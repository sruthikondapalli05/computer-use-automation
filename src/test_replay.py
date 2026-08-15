# src/test_replay.py

"""
Replays the hand-authored Saucedemo artifact and checks it reaches the
checkpoint deterministically -- no LLM in the loop. Regenerates the
artifact JSON first via create_example_artifact.py if it isn't on disk yet.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from error_handler import HardFailureError
from replay_engine import ReplayEngine

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACT_PATH = os.path.join(REPO_ROOT, "artifacts", "saucedemo_login_and_add_to_cart_v1.json")


def ensure_artifact_exists():
    if not os.path.exists(ARTIFACT_PATH):
        script = os.path.join(REPO_ROOT, "src", "create_example_artifact.py")
        subprocess.run([sys.executable, script], check=True, cwd=os.path.dirname(script))


def test_replay_saucedemo_add_to_cart():
    ensure_artifact_exists()

    engine = ReplayEngine(ARTIFACT_PATH, headless=True)
    result = engine.run()

    print(json.dumps(result, indent=2))

    assert result["success"] is True, f"Replay failed: {result['error']}"
    assert result["checkpoint_verified"] is True
    assert result["steps_executed"] == result["total_steps"]
    assert result["outputs"]["cart_item_count"] == "1"


# --------------------------------------------------- {{key}} templating: pure logic (fast)

def _engine():
    """A ReplayEngine instance for exercising pure string logic -- constructing one doesn't
    launch a browser (that only happens inside .run()), so these stay fast."""
    ensure_artifact_exists()
    return ReplayEngine(ARTIFACT_PATH, headless=True)


def test_extract_placeholder_keys_handles_non_string_text_input():
    """A numeric text_input (e.g. a 'wait' action's duration in ms -- see
    _execute_action_once's int(text_input) usage) must not crash placeholder extraction.
    There's nothing to substitute in a non-string value, so it's just not a template."""
    engine = _engine()
    assert engine._extract_placeholder_keys(2000) == []
    resolved, template = engine._resolve_step({"step_number": 1, "text_input": 2000}, {})
    assert resolved["text_input"] == 2000
    assert template is None


def test_substitute_inputs_replaces_matching_keys():
    engine = _engine()
    result = engine._substitute_inputs("{{username}}", {"username": "alice"})
    assert result == "alice"


def test_substitute_inputs_passthrough_when_no_placeholder():
    engine = _engine()
    assert engine._substitute_inputs("plain text", {"username": "alice"}) == "plain text"


def test_substitute_inputs_raises_on_missing_key():
    engine = _engine()
    with pytest.raises(HardFailureError) as exc_info:
        engine._substitute_inputs("{{amount}}", {"amt": "500"})  # key mismatch: amt vs amount
    assert "amount" in str(exc_info.value)


def test_substitute_inputs_raises_on_explicit_none():
    """An explicit None override must not become the literal string 'None'."""
    engine = _engine()
    with pytest.raises(HardFailureError):
        engine._substitute_inputs("{{promo_code}}", {"promo_code": None})


def test_substitute_inputs_does_not_cross_contaminate_fields():
    """
    Single-pass substitution: if one input's VALUE happens to look like a {{other_key}}
    placeholder, it must not get re-substituted. Regression test for the sequential
    str.replace() bug, where replacing "note" first could produce "{{password}}", which a
    later pass for "password" would then match and leak into the note field.
    """
    engine = _engine()
    inputs = {"note": "{{password}}", "password": "hunter2"}
    result = engine._substitute_inputs("{{note}}", inputs)
    assert result == "{{password}}", f"expected the literal placeholder text, got {result!r} (password leaked in)"


def test_resolve_step_returns_same_object_when_nothing_to_substitute():
    engine = _engine()
    step = {"step_number": 1, "text_input": "no placeholders here"}
    resolved, template = engine._resolve_step(step, {})
    assert resolved is step
    assert template is None


def test_resolve_step_substitutes_and_copies():
    engine = _engine()
    step = {"step_number": 2, "text_input": "{{username}}"}
    resolved, template = engine._resolve_step(step, {"username": "alice"})
    assert resolved is not step
    assert resolved["text_input"] == "alice"
    assert template == "{{username}}"
    assert step["text_input"] == "{{username}}"  # original untouched


def test_redact_handles_int_values():
    """A sensitive value that's a non-string JSON type (e.g. an int account number) must
    still be recognized and redacted -- not silently left in plaintext in the log because
    of an int-vs-str comparison mismatch."""
    engine = _engine()
    engine._effective_inputs = {"account_number": 987654321}
    assert engine._redact("987654321") == "***REDACTED***"


def test_redact_api_key():
    """"api_key" wasn't in the old hardcoded SENSITIVE_INPUT_KEYWORDS list -- a secret
    passed under that key name (e.g. via the --inputs CLI flag) must now be redacted too,
    now that redaction shares safety.py's DEFAULT_SENSITIVE_FIELDS instead of maintaining
    a second, drifted-apart list."""
    engine = _engine()
    engine._effective_inputs = {"api_key": "sk-test-123"}
    assert engine._redact("sk-test-123") == "***REDACTED***"


def test_escaped_braces_are_literal():
    r"""\{\{name\}\} must type as the literal text {{name}} -- not be treated as an
    unresolved placeholder requiring an input (it must not even raise when no inputs are
    supplied at all, since there's nothing to resolve)."""
    engine = _engine()
    text = r"The pattern is \{\{name\}\} in legacy template"
    assert engine._extract_placeholder_keys(text) == []
    result = engine._substitute_inputs(text, {})
    assert result == "The pattern is {{name}} in legacy template"


def test_escaped_and_real_placeholders_together():
    """A real placeholder and an escaped literal in the same string must both be handled
    correctly: the real one substituted, the escaped one unescaped to plain text."""
    engine = _engine()
    text = r"{{username}} said \{\{literal\}\}"
    result = engine._substitute_inputs(text, {"username": "alice"})
    assert result == "alice said {{literal}}"


def test_hyphenated_and_dotted_placeholder_keys_are_recognized():
    engine = _engine()
    assert engine._substitute_inputs("{{api-key}}", {"api-key": "abc123"}) == "abc123"
    assert engine._substitute_inputs("{{user.email}}", {"user.email": "a@b.com"}) == "a@b.com"

    with pytest.raises(HardFailureError) as exc_info:
        engine._substitute_inputs("{{api-key}}", {})
    assert "api-key" in str(exc_info.value)


def test_literal_placeholder_string_as_input_still_logs_template():
    """If an input value happens to literally equal its own placeholder string, template
    detection (regex on the ORIGINAL text) must still correctly identify the step as
    templated -- not fall back to a value-equality check that would wrongly conclude
    'nothing changed' and silently drop text_input_template from the log."""
    engine = _engine()
    step = {"step_number": 1, "text_input": "{{key}}"}
    resolved, template = engine._resolve_step(step, {"key": "{{key}}"})
    assert template == "{{key}}"
    assert resolved["text_input"] == "{{key}}"


# ------------------------------------------------- {{key}} templating: live end-to-end

def _templated_artifact_path(tmp_path, strip_baked_in_inputs=()):
    """
    Build a templated copy of the hand-authored artifact. `strip_baked_in_inputs` removes
    keys from the artifact's OWN `inputs` dict (not just the step placeholders) -- needed
    for genuinely-missing-key tests, since ReplayEngine.run() merges a caller's `inputs`
    override OVER the artifact's baked-in ones, so omitting a key from the override alone
    isn't enough to make it truly absent if the artifact still carries a value for it.
    """
    ensure_artifact_exists()
    with open(ARTIFACT_PATH) as f:
        artifact = json.load(f)
    artifact["name"] = "templated_test_artifact"
    for step in artifact["steps"]:
        if step["text_input"] == "standard_user":
            step["text_input"] = "{{username}}"
        elif step["text_input"] == "secret_sauce":
            step["text_input"] = "{{password}}"
    for key in strip_baked_in_inputs:
        artifact["inputs"].pop(key, None)
    path = tmp_path / "templated_artifact.json"
    path.write_text(json.dumps(artifact))
    return str(path)


def test_failed_step_logs_text_input(tmp_path):
    """
    A failing step must still record what it was trying to type -- including the original
    {{key}} template, if any -- so the evidence log doesn't go silent exactly when it
    matters most: investigating a failure. Breaks the password step's locator so it fails
    every attempt, then checks the step_error log event directly.
    """
    artifact_path = _templated_artifact_path(tmp_path)
    with open(artifact_path) as f:
        artifact = json.load(f)
    for step in artifact["steps"]:
        if step.get("text_input") == "{{password}}":
            step["locator"] = {"strategy": "id", "value": "this-does-not-exist", "robustness_reason": "deliberately broken for this test"}
    with open(artifact_path, "w") as f:
        json.dump(artifact, f)

    engine = ReplayEngine(artifact_path, headless=True, log_dir=str(tmp_path), timeout_ms=3000, max_retries=0)
    result = engine.run(inputs={"username": "standard_user", "password": "secret_sauce"})

    assert result["success"] is False
    step_errors = [e for e in engine._log_events if e["event"] == "step_error"]
    assert step_errors, "expected at least one step_error event"
    password_step_errors = [e for e in step_errors if e.get("text_input_template") == "{{password}}"]
    assert password_step_errors, f"expected a step_error for the broken password step, got: {step_errors}"
    assert password_step_errors[0]["text_input"] == "***REDACTED***"  # password is a sensitive field


def test_replay_with_correct_templated_inputs_succeeds(tmp_path):
    engine = ReplayEngine(_templated_artifact_path(tmp_path), headless=True, log_dir=str(tmp_path))
    result = engine.run(inputs={"username": "standard_user", "password": "secret_sauce"})
    assert result["success"] is True, f"Replay failed: {result['error']}"
    assert result["outputs"]["cart_item_count"] == "1"


def test_placeholder_validation_before_execution(tmp_path):
    """
    A missing placeholder input anywhere in the artifact must be caught before step 1 (even
    the initial navigate) executes -- not discovered only once the loop reaches the step
    that needs it, by which point earlier steps' side effects (a click, a submit) would
    already have run. steps_executed == 0 proves nothing ran at all, not just that it
    failed early.
    """
    artifact_path = _templated_artifact_path(tmp_path, strip_baked_in_inputs=("password",))
    engine = ReplayEngine(artifact_path, headless=True, log_dir=str(tmp_path))
    result = engine.run(inputs={"username": "standard_user"})  # password deliberately omitted, nowhere
    assert result["success"] is False
    assert result["steps_executed"] == 0, "no step -- not even the initial navigate -- should have run"
    assert "password" in str(result["error"]["message"])


def test_replay_with_wrong_templated_password_fails(tmp_path):
    """Negative control: proves substitution is really happening, not silently falling
    back to some baked-in default -- a wrong password must make login (and the rest of
    the flow) fail."""
    engine = ReplayEngine(_templated_artifact_path(tmp_path), headless=True, log_dir=str(tmp_path))
    result = engine.run(inputs={"username": "standard_user", "password": "definitely_wrong"})
    assert result["success"] is False


# ---------------------------------------------- malformed-artifact crash regressions

def _minimal_artifact(**overrides):
    artifact = {
        "version": "1.0", "name": "malformed_artifact_test", "description": "test",
        "target_app": "https://www.saucedemo.com", "inputs": {},
        "steps": [{"step_number": 1, "action": "navigate", "locator": None, "text_input": None, "description": "navigate"}],
        "checkpoint": {
            "element_locator": {"strategy": "id", "value": "login-button", "robustness_reason": "always present on the login page"},
            "expected_content": None, "description": "login form visible",
        },
        "outputs": {}, "created_at": "x", "created_by": "x", "estimated_duration_seconds": 1,
    }
    artifact.update(overrides)
    return artifact


def test_replay_with_numeric_wait_text_input_does_not_crash(tmp_path):
    """
    Regression test: a 'wait' step's numeric duration text_input previously crashed
    _extract_placeholder_keys with an uncaught TypeError before the browser even launched --
    run() must always return a result dict, never raise.
    """
    artifact = _minimal_artifact()
    artifact["steps"].append({"step_number": 2, "action": "wait", "locator": None, "text_input": 200, "description": "numeric wait"})
    path = tmp_path / "numeric_wait_artifact.json"
    path.write_text(json.dumps(artifact))

    engine = ReplayEngine(str(path), headless=True, log_dir=str(tmp_path))
    result = engine.run()  # must not raise
    assert result["success"] is True, f"Replay failed: {result['error']}"


def test_replay_with_explicit_null_inputs_and_outputs_does_not_crash(tmp_path):
    """
    Regression test: an artifact JSON with an explicit "inputs": null or "outputs": null
    (valid JSON, distinct from the key being absent -- dict.get(key, default) only applies
    the default when the key is missing) previously crashed __init__/run() by unpacking
    None with **.
    """
    artifact = _minimal_artifact(inputs=None, outputs=None)
    path = tmp_path / "null_fields_artifact.json"
    path.write_text(json.dumps(artifact))

    engine = ReplayEngine(str(path), headless=True, log_dir=str(tmp_path))  # must not raise
    result = engine.run()  # must not raise
    assert result["success"] is True, f"Replay failed: {result['error']}"


if __name__ == "__main__":
    test_replay_saucedemo_add_to_cart()
    print("\nReplay succeeded: checkpoint reached, no LLM involved.")
