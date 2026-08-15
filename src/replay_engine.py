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
import re
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
from safety import DEFAULT_SENSITIVE_FIELDS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG_DIR = os.path.join(REPO_ROOT, "logs")

# Placeholder key names: word chars, hyphens, dots. A `{{...}}` preceded by a backslash is
# an escaped literal, not a placeholder -- lets recorded text that happens to contain
# mustache-shaped syntax (e.g. raw unrendered template text on a legacy app) opt out.
PLACEHOLDER_PATTERN = re.compile(r"(?<!\\)\{\{([\w\-\.]+)\}\}")


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
        # `or {}`, not `.get("inputs", {})`: the .get() default only applies when the key is
        # ABSENT. An artifact JSON with an explicit "inputs": null still has the key present,
        # so .get() would return None there -- and {**None, ...} elsewhere raises TypeError.
        self._effective_inputs = self.artifact.get("inputs") or {}

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
        # Checked against the resolved (post-substitution) inputs, not the artifact's own
        # `inputs` dict alone -- a caller-supplied override for a sensitive field (e.g. a
        # different tenant's password) must be recognized as sensitive too.
        # str(value): inputs come straight from parsed JSON and may be non-string (an int
        # account number, say), while text_input -- always typed into a form as text -- is
        # always a str. Comparing without normalizing types would silently never match.
        for key, value in self._effective_inputs.items():
            if str(value) == str(text_input) and any(word in key.lower() for word in DEFAULT_SENSITIVE_FIELDS):
                return "***REDACTED***"
        return text_input

    def _extract_placeholder_keys(self, text) -> list:
        """Extract every real (non-escaped) {{key}} placeholder name from text. Single source
        of truth for "what placeholders does this text reference" -- both validation and
        substitution key off this, so they can't drift out of sync with each other.

        text_input isn't always a string: the "wait" action's text_input is a numeric
        duration in ms (see _execute_action_once's `int(text_input)`). A non-string value
        can't contain a placeholder, so it's treated as "no placeholders" rather than passed
        to a regex function that would raise TypeError on it.
        """
        if not text or not isinstance(text, str):
            return []
        return PLACEHOLDER_PATTERN.findall(text)

    def _missing_keys(self, keys: list, inputs: dict) -> list:
        """Which of `keys` have no usable value in `inputs` (absent, or explicitly None)."""
        return [k for k in keys if k not in inputs or inputs[k] is None]

    def _substitute_inputs(self, text_input: str, inputs: dict, step_number: int = None) -> str:
        """
        Replace {{key}} placeholders in text_input with values from inputs, in a single pass
        over the original string, then unescape any \\{{...\\}} literal sequences back to
        plain {{...}} text.

        A single pass matters: substituting one key at a time with repeated str.replace() calls
        means a later key's replacement could match text introduced by an earlier one -- e.g. if
        inputs["note"] itself contains the literal text "{{password}}", replacing "note" first
        would produce a string that the subsequent "password" pass then matches and replaces,
        leaking the password into a field that was only supposed to hold the note. re.sub with a
        callback scans the ORIGINAL string once; replacement text is never re-scanned.

        A placeholder whose key is missing from `inputs` (or explicitly None) is a hard failure,
        not a silent no-op -- typing the literal "{{key}}" into a form and hoping the checkpoint
        happens to notice is exactly the kind of quiet data corruption this exists to prevent.
        """
        # isinstance check: text_input isn't always a string (e.g. a numeric "wait" duration);
        # a non-string value has nothing to substitute and "{" not in text_input would itself
        # raise TypeError on it. "{" (not "{{"): an escaped placeholder like "\{\{key\}\}" has
        # a backslash sitting between the two braces, so it never contains the literal
        # two-char substring "{{" -- checking for "{{" here would skip straight past
        # escaped-only text without ever unescaping it.
        if not isinstance(text_input, str) or not text_input or "{" not in text_input:
            return text_input

        inputs = inputs or {}
        keys = self._extract_placeholder_keys(text_input)
        missing = self._missing_keys(keys, inputs)
        if missing:
            raise HardFailureError(
                f"No input value provided for placeholder(s) {sorted(set(missing))} in {text_input!r}",
                step_number=step_number,
                details={"text_input": text_input, "missing_keys": sorted(set(missing))},
            )

        result = PLACEHOLDER_PATTERN.sub(lambda m: str(inputs[m.group(1)]), text_input)
        # \{{key\}} -> {{key}}: an escaped placeholder was never touched by the substitution
        # above (the pattern's negative lookbehind skips it); unescape it back to plain text
        # now that we're past the point where it could be mistaken for a real placeholder.
        result = result.replace(r"\{\{", "{{").replace(r"\}\}", "}}")
        return result

    def _resolve_step(self, step: dict, inputs: dict) -> tuple:
        """
        Return (resolved_step, template). `template` is the step's original text_input if it
        contained a real (non-escaped) {{key}} placeholder, else None.

        Whether a step "was templated" is decided once, up front, by checking the ORIGINAL
        text for a real {{...}} pattern -- not by comparing the resolved value back against
        the original. Comparing values would wrongly conclude "not templated" in the edge
        case where an input value happens to equal its own placeholder string (e.g. a caller
        passing {"key": "{{key}}"}), silently dropping text_input_template from the log for a
        step that really was substituted.

        Text containing ONLY escaped braces (no real placeholder) still routes through
        _substitute_inputs -- that's where the unescaping happens -- but reports template=None
        since nothing was actually templated.
        """
        text_input = step.get("text_input")
        if not isinstance(text_input, str) or not text_input or "{" not in text_input:  # see _substitute_inputs
            return step, None

        has_real_placeholder = bool(self._extract_placeholder_keys(text_input))
        resolved = dict(step)
        resolved["text_input"] = self._substitute_inputs(text_input, inputs, step_number=step.get("step_number"))
        return resolved, (text_input if has_real_placeholder else None)

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

    def _execute_step(self, page, step: dict, template: str = None):
        step_number = step["step_number"]
        attempts = self.max_retries + 1

        # Redaction runs on the resolved (real) value -- a placeholder like "{{password}}"
        # isn't sensitive itself, but what it resolved to is. Computed once and attached to
        # BOTH success and failure log events -- a step that fails is exactly when an
        # auditor most needs to know what it was trying to type.
        text_input_fields = {"text_input": self._redact(step.get("text_input"))}
        if template is not None:
            text_input_fields["text_input_template"] = template

        for attempt in range(1, attempts + 1):
            try:
                result = self._execute_action_once(page, step)
                self._log(
                    "step_success",
                    step_number=step_number,
                    action=step["action"],
                    description=step["description"],
                    attempt=attempt,
                    **text_input_fields,
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
                    **text_input_fields,
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

    def _validate_placeholders(self, inputs: dict):
        """
        Scan every step's text_input for {{key}} placeholders and confirm each key is
        present (and not None) in `inputs`, before any step executes.

        This runs up front rather than lazily as the loop reaches each step because steps
        can have real side effects -- a click that submits a purchase, say. Discovering a
        missing input only when the loop later reaches the step that needs it means earlier
        steps' side effects have already happened by the time the run aborts: a caller who
        treats a clean hard-failure result as "nothing happened, safe to retry" would then
        double-submit. Failing before anything runs keeps the "safe to retry" property true.
        """
        missing_by_step = {}
        for step in self.artifact["steps"]:
            keys = self._extract_placeholder_keys(step.get("text_input"))
            missing = self._missing_keys(keys, inputs)
            if missing:
                missing_by_step[step["step_number"]] = missing

        if missing_by_step:
            raise HardFailureError(
                f"Missing input value(s) for placeholder(s), by step: {missing_by_step}",
                details={"missing_by_step": missing_by_step},
            )

    def run(self, inputs: dict = None) -> dict:
        """
        Replay the artifact. `inputs` overrides/extends the artifact's own `inputs` dict and
        is what {{key}} placeholders in step text_input are resolved against -- this is what
        lets one recorded artifact run for different tenants/users without re-discovering it
        (e.g. `engine.run(inputs={"username": "bob", "password": "..."})`).
        """
        start = time.time()
        self._log_events = []
        self._read_outputs = {}
        self._effective_inputs = {**(self.artifact.get("inputs") or {}), **(inputs or {})}
        steps_executed = 0
        error_info = None
        checkpoint_verified = False

        self._log("run_start", artifact_name=self.artifact["name"], target_app=self.artifact["target_app"])

        try:
            self._validate_placeholders(self._effective_inputs)
        except ReplayError as e:
            error_info = e.to_dict()
            self._log("run_failed", **error_info)

        if error_info is None:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                page = browser.new_page()
                try:
                    for step in self.artifact["steps"]:
                        resolved_step, template = self._resolve_step(step, self._effective_inputs)
                        self._execute_step(page, resolved_step, template=template)
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
            "outputs": {**(self.artifact.get("outputs") or {}), **self._read_outputs} if success else {},
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
    parser.add_argument(
        "--inputs", default=None,
        help='JSON object overriding/extending the artifact\'s inputs, e.g. \'{"username": "bob"}\'. '
             'Resolves any {{key}} placeholders in the artifact\'s steps.',
    )
    args = parser.parse_args()

    engine = ReplayEngine(args.artifact_path, headless=not args.headed)
    outcome = engine.run(inputs=json.loads(args.inputs) if args.inputs else None)
    print(json.dumps(outcome, indent=2))
    sys.exit(0 if outcome["success"] else 1)
