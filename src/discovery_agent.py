# src/discovery_agent.py

"""
LLM-driven discovery agent.

Given a goal and a target URL, this loop observes the live page (screenshot),
asks Claude to decide the next UI action (vision + tool use), executes that
action with Playwright, and repeats until Claude reports the goal reached,
max_steps is hit, or an unrecoverable error occurs. The resulting step
history is written out as an Artifact -- the same schema the replay engine
consumes -- so the LLM never has to be called again for this task.

The agent immediately auto-replays what it just recorded (via ReplayEngine)
before declaring success: a discovery run that can't reproduce itself
deterministically hasn't actually produced a usable artifact.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

from anthropic import Anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from artifact_schema import Artifact, Step, Locator, Checkpoint
from error_handler import ErrorCategory, HardFailureError, classify_playwright_exception
from replay_engine import ReplayEngine
from safety import SafetyManager, SafetyViolation
from human_escalation import EscalationManager

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG_DIR = os.path.join(REPO_ROOT, "logs")
DEFAULT_ARTIFACTS_DIR = os.path.join(REPO_ROOT, "artifacts")
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

SENSITIVE_INPUT_KEYWORDS = ("password", "token", "secret", "pin", "ssn", "account_number")
MAX_ACTION_RETRIES = 2          # same-locator retries on a transient (recoverable) failure
MAX_CONSECUTIVE_FAILURES = 3    # distinct Claude decisions that fail before giving up entirely

DECIDE_TOOL = {
    "name": "decide_next_action",
    "description": (
        "Decide the single next UI action needed to make progress toward the goal, "
        "given the current screenshot and history, or declare the goal complete."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_complete": {
                "type": "boolean",
                "description": "True only if the goal has been fully and verifiably achieved on the current screen.",
            },
            "reasoning": {
                "type": "string",
                "description": "One or two sentences on why this action (or completion) is correct.",
            },
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "read", "wait"],
                "description": "Required when is_complete is false.",
            },
            "step_description": {
                "type": "string",
                "description": "Short human-readable description of this step, e.g. 'Click login button'.",
            },
            "locator": {
                "type": "object",
                "description": "Required for click/type/read. Prefer id, then css, then xpath, then text -- pick whatever will still be stable on a future replay.",
                "properties": {
                    "strategy": {"type": "string", "enum": ["id", "css", "xpath", "text", "class"]},
                    "value": {"type": "string"},
                    "robustness_reason": {"type": "string", "description": "Why this locator will stay stable across replays."},
                },
                "required": ["strategy", "value", "robustness_reason"],
            },
            "text_input": {
                "type": "string",
                "description": "Text to type. Required when action is 'type'.",
            },
            "input_key": {
                "type": "string",
                "description": "If text_input is a named input value (e.g. a username or password), give it a short snake_case name here so it's recorded in the artifact's inputs.",
            },
            "is_sensitive": {
                "type": "boolean",
                "description": "True if text_input is a credential/secret that must be redacted from logs (it is still stored in the artifact, since replay needs it).",
            },
            "checkpoint": {
                "type": "object",
                "description": "Required when is_complete is true: how a future deterministic replay can verify success on this page.",
                "properties": {
                    "locator": {
                        "type": "object",
                        "properties": {
                            "strategy": {"type": "string", "enum": ["id", "css", "xpath", "text", "class"]},
                            "value": {"type": "string"},
                            "robustness_reason": {"type": "string"},
                        },
                        "required": ["strategy", "value", "robustness_reason"],
                    },
                    "expected_content": {
                        "type": ["string", "null"],
                        "description": "Text the checkpoint element must contain, or null to just check it's visible.",
                    },
                    "description": {"type": "string"},
                },
                "required": ["locator", "description"],
            },
            "outputs": {
                "type": "object",
                "description": "Required when is_complete is true: key/value facts extracted from the final screen (e.g. item_name, item_price).",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["is_complete", "reasoning"],
    },
}

SYSTEM_PROMPT = """You are a UI automation discovery agent. You are shown a screenshot of a web \
page and a goal. Your job is to decide ONE next action at a time to move toward the goal, by \
calling the decide_next_action tool. You will be shown the result of each action (and a fresh \
screenshot) before deciding the next one.

Rules:
- Always respond by calling decide_next_action. Never respond with plain text.
- Prefer the most stable locator available: id > css > text > class > xpath. Explain why in \
robustness_reason. Avoid locators tied to layout/position or generated/volatile values.
- Only set is_complete=true once the goal is verifiably achieved on the current screen. When you \
do, you MUST also provide a checkpoint (a locator + description a future replay can check, with \
no LLM involved) and outputs (facts extracted from the page relevant to the goal).
- If credentials or example values are visible on the page itself (common on demo/test sites), \
use them.
- One action per turn. Do not try to skip steps.
- If an action just failed, pick a different locator or approach -- do not repeat the exact same \
failed action.
"""


class DiscoveryAgent:
    def __init__(
        self,
        api_key: str,
        target_url: str,
        goal: str,
        max_steps: int = 20,
        headed: bool = False,
        timeout_ms: int = 10_000,
        model: str = DEFAULT_MODEL,
        log_dir: str = None,
        artifacts_dir: str = None,
        safety_config: dict = None,
        escalation_decision_fn=None,
    ):
        self.client = Anthropic(api_key=api_key)
        self.target_url = target_url
        self.goal = goal
        self.max_steps = max_steps
        self.headed = headed
        self.timeout_ms = timeout_ms
        self.model = model
        self.log_dir = log_dir or DEFAULT_LOG_DIR
        self.artifacts_dir = artifacts_dir or DEFAULT_ARTIFACTS_DIR

        self.run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.screenshot_dir = os.path.join(self.log_dir, "screenshots", self.run_id)

        # Defaults to allowing only the target app's own domain -- discovery can't
        # wander to arbitrary sites unless a caller explicitly widens the allowlist.
        self.safety = SafetyManager(safety_config or self._default_safety_config())
        self.escalation = EscalationManager(
            run_id=self.run_id, log_dir=self.log_dir, safety=self.safety, decision_fn=escalation_decision_fn
        )

        self._log_events = []
        self._steps = []       # list[Step]
        self._inputs = {}      # collected named input values

    def _default_safety_config(self) -> dict:
        netloc = urlparse(self.target_url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return {"allowed_domains": [netloc], "blocked_patterns": [], "risk_threshold": 70}

    # ---------------------------------------------------------------- utils

    def _log(self, event: str, **fields):
        self._log_events.append({"timestamp": datetime.now().isoformat(), "event": event, **fields})

    def _redact(self, text_input, is_sensitive: bool):
        """Full redaction if Claude flagged it sensitive; pattern-based (PII/secret-shaped) redaction otherwise."""
        if text_input is None:
            return None
        if is_sensitive:
            return "***REDACTED***"
        return self.safety.redact_text(text_input)

    def _resolve_locator(self, page, locator: dict):
        strategy, value = locator["strategy"], locator["value"]
        if strategy == "id":
            selector = f"#{value}"
        elif strategy == "css":
            selector = value
        elif strategy == "xpath":
            selector = f"xpath={value}"
        elif strategy == "text":
            selector = f"text={value}"
        elif strategy == "class":
            selector = f".{value}"
        else:
            raise HardFailureError(f"Unknown locator strategy: {strategy}")
        return page.locator(selector).first

    def _capture(self, page, step_number: int, suffix: str = "") -> str:
        os.makedirs(self.screenshot_dir, exist_ok=True)
        path = os.path.join(self.screenshot_dir, f"step_{step_number:02d}{suffix}.png")
        page.screenshot(path=path)
        return path

    def _screenshot(self, page, step_number: int) -> bytes:
        path = self._capture(page, step_number)
        with open(path, "rb") as f:
            return f.read()

    def _image_block(self, png_bytes: bytes) -> dict:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png_bytes).decode()},
        }

    # ------------------------------------------------------------ execution

    def _execute_action_once(self, page, action: str, locator: dict, text_input: str):
        if action == "navigate":
            page.goto(self.target_url, timeout=self.timeout_ms)
            return None
        if action == "wait":
            if locator:
                self._resolve_locator(page, locator).wait_for(state="visible", timeout=self.timeout_ms)
            else:
                page.wait_for_timeout(int(text_input) if text_input else 1000)
            return None

        if locator is None:
            raise HardFailureError(f"Action '{action}' requires a locator but none was given")

        el = self._resolve_locator(page, locator)
        el.wait_for(state="visible", timeout=self.timeout_ms)

        if action == "click":
            el.click(timeout=self.timeout_ms)
            return None
        if action == "type":
            el.fill(text_input or "", timeout=self.timeout_ms)
            return None
        if action == "read":
            return el.text_content(timeout=self.timeout_ms)

        raise HardFailureError(f"Unknown action type: {action}")

    def _execute_with_retry(self, page, action: str, locator: dict, text_input: str):
        """Retries the SAME decision on transient failures. Returns (result, error_dict_or_None)."""
        attempts = MAX_ACTION_RETRIES + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._execute_action_once(page, action, locator, text_input), None
            except Exception as exc:
                category = classify_playwright_exception(exc)
                if category == ErrorCategory.RECOVERABLE and attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                return None, {"category": category.value, "message": str(exc)}

    # ------------------------------------------------------------- LLM turn

    def _ask_claude(self, messages: list) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            temperature=0,
            system=SYSTEM_PROMPT,
            tools=[DECIDE_TOOL],
            tool_choice={"type": "tool", "name": "decide_next_action"},
            messages=messages,
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})
        return tool_use.id, tool_use.input

    def _validate_decision(self, decision: dict) -> str:
        """Returns an error string if the decision is malformed, else None."""
        if decision.get("is_complete"):
            if not decision.get("checkpoint") or not decision["checkpoint"].get("locator"):
                return "is_complete=true but no checkpoint.locator was provided"
            return None

        action = decision.get("action")
        if action not in ("navigate", "click", "type", "read", "wait"):
            return f"invalid or missing action: {action!r}"
        if action in ("click", "type", "read") and not decision.get("locator"):
            return f"action '{action}' requires a locator"
        if action == "type" and not decision.get("text_input"):
            return "action 'type' requires text_input"
        return None

    # ------------------------------------------------------------------ run

    def discover(self) -> dict:
        start = time.time()
        self._log_events, self._steps, self._inputs = [], [], {}
        self._log("run_start", goal=self.goal, target_url=self.target_url, model=self.model)

        error_info = None
        checkpoint = None
        outputs = {}
        consecutive_failures = 0

        with self._sync_playwright_context() as (browser, page):
            try:
                self.safety.enforce_url_allowed(self.target_url, step_number=1)
            except SafetyViolation as exc:
                error_info = exc.to_dict()
                self._log("run_failed", **error_info)
                return self._finalize(start, error_info, checkpoint, outputs)

            # Step 1 is always a deterministic navigate -- no need to spend an LLM call on it.
            step_number = 1
            page.goto(self.target_url, timeout=self.timeout_ms)
            self._steps.append(Step(step_number, "navigate", None, None, f"Navigate to {self.target_url}"))
            self._log("step_success", step_number=step_number, action="navigate", description=self._steps[-1].description)

            screenshot = self._screenshot(page, step_number)
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Goal: {self.goal}\nTarget URL: {self.target_url}\n\n"
                                              f"Here is the current screenshot (after navigating to the page). "
                                              f"Decide the next action."},
                    self._image_block(screenshot),
                ],
            }]

            iterations = 0
            while iterations < self.max_steps:
                iterations += 1
                try:
                    tool_use_id, decision = self._ask_claude(messages)
                except Exception as exc:
                    error_info = {"category": ErrorCategory.HARD_FAILURE.value, "message": f"Claude API call failed: {exc}"}
                    self._log("run_failed", **error_info)
                    break

                validation_error = self._validate_decision(decision)
                if validation_error:
                    self._log("decision_invalid", decision=decision, error=validation_error)
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        error_info = {"category": ErrorCategory.HARD_FAILURE.value, "message": f"Too many invalid decisions: {validation_error}"}
                        break
                    messages.append({"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                        "content": [{"type": "text", "text": f"Invalid decision: {validation_error}. Try again."}],
                    }]})
                    continue

                if decision.get("is_complete"):
                    checkpoint = decision["checkpoint"]
                    outputs = decision.get("outputs", {}) or {}
                    self._log("goal_reported_complete", reasoning=decision.get("reasoning"))
                    break

                step_number += 1
                action = decision["action"]
                locator = decision.get("locator")
                text_input = decision.get("text_input")
                is_sensitive = bool(decision.get("is_sensitive"))
                input_key = decision.get("input_key")
                description = decision.get("step_description") or f"{action} step {step_number}"

                if input_key and text_input is not None:
                    self._inputs[input_key] = text_input

                risk_category = self.safety.categorize_risk(action, locator, text_input)
                risk_score = self.safety.score_action(action, locator, text_input)
                self._log("risk_assessment", step_number=step_number, action=action, risk_category=risk_category, risk_score=risk_score)

                if self.safety.needs_human_review(risk_score):
                    handoff_state = {
                        "url": page.url,
                        "screenshot_path": self._capture(page, step_number, suffix="_review"),
                        "vars": dict(self._inputs),
                    }
                    escalation_result = self.escalation.pause_for_review(
                        step_number=step_number, action=action, locator=locator, text_input=text_input,
                        risk_score=risk_score, risk_category=risk_category, reasoning=decision.get("reasoning"),
                        handoff_state=handoff_state,
                    )
                    self._log("escalation_decision", step_number=step_number, decision=escalation_result["decision"])

                    if escalation_result["decision"] == "abort":
                        error_info = {
                            "category": ErrorCategory.HUMAN_ABORTED.value,
                            "message": f"Human aborted at step {step_number} ({description})",
                        }
                        step_number -= 1  # never executed
                        break
                    if escalation_result["decision"] == "modify":
                        modified = escalation_result.get("modified_action") or {}
                        action = modified.get("action", action)
                        locator = modified.get("locator", locator)
                        text_input = modified.get("text_input", text_input)
                        description = f"{description} (human-modified)"
                    # "approve" falls through and executes the original decision unchanged.

                result, exec_error = self._execute_with_retry(page, action, locator, text_input)

                if exec_error is None:
                    try:
                        self.safety.enforce_url_allowed(page.url, step_number=step_number)
                    except SafetyViolation as exc:
                        error_info = exc.to_dict()
                        self._log("run_failed", **error_info)
                        step_number -= 1
                        break

                    self._steps.append(Step(step_number, action, Locator(**locator) if locator else None, text_input, description))
                    self._log(
                        "step_success", step_number=step_number, action=action, description=description,
                        text_input=self._redact(text_input, is_sensitive),
                    )
                    consecutive_failures = 0
                    screenshot = self._screenshot(page, step_number)
                    outcome_text = f"Action succeeded: {description}."
                    if action == "read" and result:
                        outcome_text += f" Read content: {result!r}"
                    messages.append({"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": tool_use_id,
                        "content": [{"type": "text", "text": outcome_text}, self._image_block(screenshot)],
                    }]})
                else:
                    consecutive_failures += 1
                    self._log("step_error", step_number=step_number, action=action, description=description, **exec_error)
                    step_number -= 1  # this step never actually happened
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        error_info = {
                            "category": ErrorCategory.HARD_FAILURE.value,
                            "message": f"{exec_error['message']} (after {consecutive_failures} consecutive failed decisions)",
                        }
                        break
                    screenshot = self._screenshot(page, step_number)
                    messages.append({"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                        "content": [
                            {"type": "text", "text": f"Action failed ({exec_error['category']}): {exec_error['message']}. "
                                                      f"Try a different locator or approach."},
                            self._image_block(screenshot),
                        ],
                    }]})

            else:
                error_info = {"category": ErrorCategory.HARD_FAILURE.value, "message": f"Reached max_steps ({self.max_steps} decisions) without completion"}
                self._log("run_failed", **error_info)

        return self._finalize(start, error_info, checkpoint, outputs)

    def _finalize(self, start: float, error_info: dict, checkpoint: dict, outputs: dict) -> dict:
        duration = round(time.time() - start, 2)
        success = error_info is None and checkpoint is not None
        result = {
            "success": success,
            "goal": self.goal,
            "target_url": self.target_url,
            "steps_taken": len(self._steps),
            "error": error_info,
            "duration_seconds": duration,
            "screenshot_dir": self.screenshot_dir,
            "artifact_path": None,
            "replay_verified": False,
            "replay_result": None,
        }

        if success:
            artifact_path = self._save_artifact(checkpoint, outputs, duration)
            result["artifact_path"] = artifact_path
            self._log("artifact_saved", path=artifact_path)

            replay_result = ReplayEngine(artifact_path, headless=True, timeout_ms=self.timeout_ms).run()
            result["replay_result"] = replay_result
            result["replay_verified"] = replay_result["success"]
            self._log("replay_verification", verified=replay_result["success"])
            # A discovery run that can't replay deterministically hasn't produced a usable artifact.
            result["success"] = replay_result["success"]

        self._log("run_end", success=result["success"], duration_seconds=duration)
        self._write_log(result)
        return result

    def _sync_playwright_context(self):
        from playwright.sync_api import sync_playwright
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not self.headed)
                page = browser.new_page()
                try:
                    yield browser, page
                finally:
                    browser.close()

        return ctx()

    # ------------------------------------------------------------- artifact

    def _slugify(self, text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return slug[:60] or "discovered_task"

    def _save_artifact(self, checkpoint: dict, outputs: dict, duration: float) -> str:
        artifact = Artifact(
            version="1.0",
            name=self._slugify(self.goal),
            description=self.goal,
            target_app=self.target_url,
            inputs=self._inputs,
            steps=self._steps,
            checkpoint=Checkpoint(
                Locator(**checkpoint["locator"]),
                checkpoint.get("expected_content"),
                checkpoint["description"],
            ),
            outputs=outputs,
            created_at=datetime.now().isoformat(),
            created_by="ai_discovery_run",
            estimated_duration_seconds=int(duration),
        )
        path = os.path.join(self.artifacts_dir, f"discovery_{self.run_id}.json")
        artifact.save_to_file(path)
        return path

    def _write_log(self, result: dict):
        os.makedirs(self.log_dir, exist_ok=True)
        path = os.path.join(self.log_dir, f"discovery_{self.run_id}.json")
        with open(path, "w") as f:
            json.dump({"result": result, "events": self._log_events}, f, indent=2)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Discover a UI task via LLM observation and save it as a replayable artifact.")
    parser.add_argument("target_url", help="URL of the app to learn a task on")
    parser.add_argument("goal", help="Natural-language description of the task to accomplish")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if not args.api_key:
        print("Error: no Anthropic API key. Set ANTHROPIC_API_KEY (in .env or the environment) or pass --api-key.", file=sys.stderr)
        sys.exit(2)

    agent = DiscoveryAgent(
        api_key=args.api_key,
        target_url=args.target_url,
        goal=args.goal,
        max_steps=args.max_steps,
        headed=args.headed,
        model=args.model,
    )
    outcome = agent.discover()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["success"] else 1)
