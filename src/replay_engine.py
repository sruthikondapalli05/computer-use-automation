# src/replay_engine.py

"""
Deterministic replay engine.

Loads an Artifact (JSON recipe card produced by discovery or hand-authored),
executes its steps against a real browser via Playwright, and verifies the
checkpoint -- with no LLM anywhere in the decision loop. Every step is
logged; failures are classified using the error taxonomy in error_handler.py
so callers can tell a valid business outcome apart from a transient hiccup
apart from a hard failure that needs a human.
"""

import json
import os
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from error_handler import (
    ErrorCategory,
    ReplayError,
    HardFailureError,
    classify_playwright_exception,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG_DIR = os.path.join(REPO_ROOT, "logs")

# Values pulled from `inputs` that land in text_input are redacted in logs
# if their input key looks sensitive. Not a full secrets scanner -- just
# enough to keep passwords/tokens out of the evidence trail.
SENSITIVE_INPUT_KEYWORDS = ("password", "token", "secret", "pin", "ssn", "account_number")


class ReplayEngine:
    def __init__(
        self,
        artifact_path: str,
        headless: bool = True,
        timeout_ms: int = 10_000,
        max_retries: int = 2,
        log_dir: str = None,
    ):
        self.artifact_path = artifact_path
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.log_dir = log_dir or DEFAULT_LOG_DIR

        self.artifact = self._load_artifact(artifact_path)
        self._log_events = []
        self._read_outputs = {}

    def _load_artifact(self, path: str) -> dict:
        with open(path) as f:
            return json.load(f)

    def _log(self, event: str, **fields):
        self._log_events.append({
            "timestamp": datetime.now().isoformat(),
            "event": event,
            **fields,
        })

    def _redact(self, text_input):
        if text_input is None:
            return None
        for key, value in self.artifact.get("inputs", {}).items():
            if value == text_input and any(word in key.lower() for word in SENSITIVE_INPUT_KEYWORDS):
                return "***REDACTED***"
        return text_input

    def _resolve_locator(self, page, locator: dict):
        strategy = locator["strategy"]
        value = locator["value"]

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

        # .first: several of these strategies (text, class) can match more
        # than one element on a real page -- we want the first stable match,
        # not a strict-mode crash.
        return page.locator(selector).first

    def _execute_action_once(self, page, step: dict):
        action = step["action"]
        locator = step.get("locator")
        text_input = step.get("text_input")

        if action == "navigate":
            page.goto(self.artifact["target_app"], timeout=self.timeout_ms)
            return None

        if action == "wait":
            if locator:
                self._resolve_locator(page, locator).wait_for(state="visible", timeout=self.timeout_ms)
            else:
                page.wait_for_timeout(int(text_input) if text_input else 1000)
            return None

        if locator is None:
            raise HardFailureError(f"Step {step['step_number']} ({action}) has no locator", step_number=step["step_number"])

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

        raise HardFailureError(f"Unknown action type: {action}", step_number=step["step_number"])

    def _execute_step(self, page, step: dict):
        step_number = step["step_number"]
        attempts = self.max_retries + 1

        for attempt in range(1, attempts + 1):
            try:
                result = self._execute_action_once(page, step)
                self._log(
                    "step_success",
                    step_number=step_number,
                    action=step["action"],
                    description=step["description"],
                    attempt=attempt,
                    text_input=self._redact(step.get("text_input")),
                )
                if step["action"] == "read" and result is not None:
                    self._read_outputs[f"step_{step_number}_read"] = result
                return

            except ReplayError:
                raise
            except Exception as exc:
                category = classify_playwright_exception(exc)
                self._log(
                    "step_error",
                    step_number=step_number,
                    action=step["action"],
                    attempt=attempt,
                    category=category.value,
                    error=str(exc),
                )
                if category == ErrorCategory.RECOVERABLE and attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise HardFailureError(
                    f"Step {step_number} ({step['action']}) failed after {attempt} attempt(s): {exc}",
                    step_number=step_number,
                    details={"action": step["action"], "description": step["description"]},
                ) from exc

    def _verify_checkpoint(self, page) -> str:
        checkpoint = self.artifact["checkpoint"]
        expected = checkpoint.get("expected_content")

        try:
            el = self._resolve_locator(page, checkpoint["element_locator"])
            el.wait_for(state="visible", timeout=self.timeout_ms)
            actual = el.text_content(timeout=self.timeout_ms)
        except Exception as exc:
            raise HardFailureError(
                f"Checkpoint not reached: {checkpoint['description']} ({exc})",
                details={"checkpoint": checkpoint["description"]},
            ) from exc

        if expected is not None and (actual is None or expected.strip() not in actual.strip()):
            raise HardFailureError(
                f"Checkpoint content mismatch: expected '{expected}', got '{actual}'",
                details={"checkpoint": checkpoint["description"], "expected": expected, "actual": actual},
            )

        return actual

    def run(self) -> dict:
        start = time.time()
        self._log_events = []
        self._read_outputs = {}
        steps_executed = 0
        error_info = None
        checkpoint_verified = False

        self._log("run_start", artifact_name=self.artifact["name"], target_app=self.artifact["target_app"])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            try:
                for step in self.artifact["steps"]:
                    self._execute_step(page, step)
                    steps_executed += 1

                checkpoint_actual = self._verify_checkpoint(page)
                checkpoint_verified = True
                self._log(
                    "checkpoint_verified",
                    description=self.artifact["checkpoint"]["description"],
                    actual=checkpoint_actual,
                )

            except ReplayError as e:
                error_info = e.to_dict()
                self._log("run_failed", **error_info)
            finally:
                browser.close()

        duration = round(time.time() - start, 2)
        success = checkpoint_verified and error_info is None

        result = {
            "success": success,
            "artifact_name": self.artifact["name"],
            "steps_executed": steps_executed,
            "total_steps": len(self.artifact["steps"]),
            "checkpoint_verified": checkpoint_verified,
            "outputs": {**self.artifact.get("outputs", {}), **self._read_outputs} if success else {},
            "error": error_info,
            "duration_seconds": duration,
        }

        self._log("run_end", success=success, duration_seconds=duration)
        self._write_log(result)
        return result

    def _write_log(self, result: dict):
        os.makedirs(self.log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        log_path = os.path.join(self.log_dir, f"replay_{self.artifact['name']}_{timestamp}.json")
        with open(log_path, "w") as f:
            json.dump({"result": result, "events": self._log_events}, f, indent=2)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Replay an Artifact deterministically, no LLM involved.")
    parser.add_argument("artifact_path", help="Path to the artifact JSON file")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    args = parser.parse_args()

    engine = ReplayEngine(args.artifact_path, headless=not args.headed)
    outcome = engine.run()
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["success"] else 1)
