# src/test_safety.py

"""
Unit tests for the safety layer (SafetyManager, EscalationManager) plus one
integration test proving discovery_agent actually pauses for human review
on a high-risk action mid-run. No network/API key required -- the
integration test drives DiscoveryAgent with a scripted fake Claude client,
the same technique used to validate the loop mechanics without spending API
credits.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from safety import SafetyManager, SafetyViolation
from human_escalation import EscalationManager

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------- allowlist

def test_allowlist_permits_configured_domain():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"]})
    assert safety.is_url_allowed("https://www.saucedemo.com/inventory.html") is True
    assert safety.is_url_allowed("https://saucedemo.com/") is True


def test_allowlist_blocks_external_domain():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"]})
    assert safety.is_url_allowed("https://evil-phishing-site.com") is False
    with pytest.raises(SafetyViolation):
        safety.enforce_url_allowed("https://evil-phishing-site.com")


def test_allowlist_fails_closed_when_empty():
    safety = SafetyManager({"allowed_domains": []})
    assert safety.is_url_allowed("https://saucedemo.com") is False


def test_blocked_patterns_override_an_allowed_domain():
    safety = SafetyManager({"allowed_domains": ["example.com"], "blocked_patterns": [r".*admin.*", r".*api.*"]})
    assert safety.is_url_allowed("https://example.com/dashboard") is True
    assert safety.is_url_allowed("https://example.com/admin/panel") is False
    assert safety.is_url_allowed("https://example.com/api/v1/users") is False


# ---------------------------------------------------------------- redaction

def test_redact_text_masks_credit_card():
    safety = SafetyManager()
    out = safety.redact_text("Card on file: 4111 1111 1111 1111")
    assert "4111 1111 1111 1111" not in out
    assert "REDACTED" in out


def test_redact_text_masks_email():
    safety = SafetyManager()
    out = safety.redact_text("Contact john.doe@example.com for details")
    assert "john.doe@example.com" not in out


def test_redact_text_masks_ssn():
    safety = SafetyManager()
    out = safety.redact_text("SSN on record: 123-45-6789")
    assert "123-45-6789" not in out


def test_redact_text_masks_phone():
    safety = SafetyManager()
    out = safety.redact_text("Call me at 415-555-1234")
    assert "415-555-1234" not in out


def test_redact_text_masks_api_key():
    safety = SafetyManager()
    out = safety.redact_text("Use key sk-abcdef1234567890 to authenticate")
    assert "sk-abcdef1234567890" not in out


def test_redact_value_by_field_name_fully_masks_non_pattern_secrets():
    # A password like "secret_sauce" has no distinctive regex shape --
    # only field-name-based redaction can catch it.
    safety = SafetyManager({"sensitive_fields": ["password"]})
    assert safety.redact_value("secret_sauce", field_name="password") == "***REDACTED***"


def test_redact_value_leaves_non_sensitive_plain_text_untouched():
    safety = SafetyManager()
    assert safety.redact_value("Fleece Jacket", field_name="product_name") == "Fleece Jacket"


# ----------------------------------------------------------- risk scoring

def test_categorize_risk_low_for_ordinary_click():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"]})
    assert safety.categorize_risk("click", {"strategy": "id", "value": "login-button", "robustness_reason": "id"}, None) == "LOW"


def test_categorize_risk_medium_for_sensitive_field():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"]})
    category = safety.categorize_risk("type", {"strategy": "id", "value": "password", "robustness_reason": "id"}, "hunter2")
    assert category == "MEDIUM"


def test_categorize_risk_medium_for_pii_shaped_input():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"]})
    category = safety.categorize_risk("type", {"strategy": "id", "value": "card-number", "robustness_reason": "id"}, "4111 1111 1111 1111")
    assert category == "MEDIUM"


def test_categorize_risk_high_for_external_navigate_with_sensitive_data():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"]})
    category = safety.categorize_risk("navigate", None, "send card 4111 1111 1111 1111 to evil.com")
    assert category == "HIGH"


def test_score_action_exceeds_threshold_for_high_risk():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"], "risk_threshold": 70})
    score = safety.score_action("navigate", None, "send card 4111 1111 1111 1111 to evil.com")
    assert score > 70
    assert safety.needs_human_review(score) is True


def test_score_action_low_risk_does_not_need_review():
    safety = SafetyManager({"allowed_domains": ["saucedemo.com"]})
    score = safety.score_action("click", {"strategy": "id", "value": "login-button", "robustness_reason": "id"}, None)
    assert score <= 70
    assert safety.needs_human_review(score) is False


def test_score_action_flags_risky_keyword():
    safety = SafetyManager({"allowed_domains": ["bank.com"]})
    score = safety.score_action("click", {"strategy": "text", "value": "Delete Account", "robustness_reason": "button text"}, None)
    assert score >= 50


# ------------------------------------------------------------- escalation

def test_escalation_approve_returns_approve_and_writes_audit_trail(tmp_path):
    escalation = EscalationManager(run_id="test_approve", log_dir=str(tmp_path), decision_fn=lambda ctx: {"decision": "approve"})
    result = escalation.pause_for_review(
        step_number=5, action="type", locator={"strategy": "id", "value": "password", "robustness_reason": "id"},
        text_input="hunter2", risk_score=90, risk_category="HIGH",
    )
    assert result["decision"] == "approve"

    audit_path = os.path.join(str(tmp_path), "escalations_test_approve.json")
    assert os.path.exists(audit_path)
    with open(audit_path) as f:
        audit = json.load(f)
    assert len(audit["escalations"]) == 1
    assert audit["escalations"][0]["decision"] == "approve"
    assert audit["escalations"][0]["risk_score"] == 90


def test_escalation_abort_returns_abort():
    escalation = EscalationManager(run_id="test_abort", log_dir="/tmp", decision_fn=lambda ctx: {"decision": "abort"})
    result = escalation.pause_for_review(
        step_number=3, action="navigate", locator=None, text_input=None, risk_score=95, risk_category="HIGH",
    )
    assert result["decision"] == "abort"


def test_escalation_modify_returns_modified_action():
    escalation = EscalationManager(
        run_id="test_modify", log_dir="/tmp",
        decision_fn=lambda ctx: {"decision": "modify", "modified_action": {"text_input": "corrected_value"}},
    )
    result = escalation.pause_for_review(
        step_number=2, action="type", locator={"strategy": "id", "value": "amount", "robustness_reason": "id"},
        text_input="9999999", risk_score=80, risk_category="HIGH",
    )
    assert result["decision"] == "modify"
    assert result["modified_action"]["text_input"] == "corrected_value"


def test_escalation_unrecognized_response_fails_closed_to_abort():
    escalation = EscalationManager(run_id="test_fail_closed", log_dir="/tmp", decision_fn=lambda ctx: {"decision": "banana"})
    result = escalation.pause_for_review(step_number=1, action="click", risk_score=99, risk_category="HIGH")
    assert result["decision"] == "abort"


def test_escalation_audit_trail_redacts_sensitive_text_input(tmp_path):
    safety = SafetyManager({"sensitive_fields": ["password"]})
    escalation = EscalationManager(run_id="test_redact", log_dir=str(tmp_path), safety=safety, decision_fn=lambda ctx: {"decision": "approve"})
    escalation.pause_for_review(
        step_number=1, action="type", locator={"strategy": "id", "value": "password", "robustness_reason": "id"},
        text_input="hunter2", risk_score=90, risk_category="HIGH",
    )
    with open(os.path.join(str(tmp_path), "escalations_test_redact.json")) as f:
        audit = json.load(f)
    assert audit["escalations"][0]["text_input"] == "***REDACTED***"


def test_escalation_audit_trail_accumulates_multiple_entries(tmp_path):
    decisions = iter([{"decision": "approve"}, {"decision": "abort"}])
    escalation = EscalationManager(run_id="test_multi", log_dir=str(tmp_path), decision_fn=lambda ctx: next(decisions))
    escalation.pause_for_review(step_number=1, action="click", risk_score=75, risk_category="MEDIUM")
    escalation.pause_for_review(step_number=2, action="click", risk_score=95, risk_category="HIGH")

    with open(os.path.join(str(tmp_path), "escalations_test_multi.json")) as f:
        audit = json.load(f)
    assert len(audit["escalations"]) == 2
    assert [e["decision"] for e in audit["escalations"]] == ["approve", "abort"]


# ------------------------------------------------------- discovery integration

def test_discovery_agent_escalates_on_high_risk_type_action():
    """
    A scripted (mocked-Claude) discovery run where one step types a
    credit-card-shaped value into a field. Confirms the risk score crosses
    the threshold, escalation actually pauses the run, the (mocked) human
    approves, and the run log records both the risk assessment and the
    escalation decision.
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
    from discovery_agent import DiscoveryAgent

    script = [
        {"is_complete": False, "reasoning": "click card field", "action": "click", "step_description": "Click card number field",
         "locator": {"strategy": "id", "value": "card-number", "robustness_reason": "id"}},
        {"is_complete": False, "reasoning": "type card number", "action": "type", "step_description": "Type card number",
         "text_input": "4111 1111 1111 1111", "input_key": "card_number", "is_sensitive": True,
         "locator": {"strategy": "id", "value": "card-number", "robustness_reason": "id"}},
        {"is_complete": True, "reasoning": "done",
         "checkpoint": {"locator": {"strategy": "id", "value": "footer", "robustness_reason": "footer always present"},
                         "expected_content": None, "description": "Reached end of form"},
         "outputs": {}},
    ]

    class FakeToolUse:
        def __init__(self, input_dict, idx):
            self.type = "tool_use"
            self.id = f"toolu_{idx}"
            self.input = input_dict

    class FakeMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            decision = script[self.calls]
            resp = type("Resp", (), {"content": [FakeToolUse(decision, self.calls)]})()
            self.calls += 1
            return resp

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    escalation_calls = []

    def decision_fn(ctx):
        escalation_calls.append(ctx)
        return {"decision": "approve"}

    def _snapshot(dir_path):
        return set(os.listdir(dir_path)) if os.path.isdir(dir_path) else set()

    artifacts_dir = os.path.join(REPO_ROOT, "artifacts")
    logs_dir = os.path.join(REPO_ROOT, "logs")
    screenshots_dir = os.path.join(logs_dir, "screenshots")
    before = {
        artifacts_dir: _snapshot(artifacts_dir),
        logs_dir: _snapshot(logs_dir),
        screenshots_dir: _snapshot(screenshots_dir),
    }

    agent = DiscoveryAgent(
        api_key="unused", target_url="https://www.saucedemo.com",
        goal="Type a card number into a field", max_steps=20, headed=False,
        escalation_decision_fn=decision_fn,
    )
    agent.client = FakeClient()

    try:
        result = agent.discover()

        assert len(escalation_calls) == 1, "the credit-card type action should have triggered exactly one escalation"
        assert escalation_calls[0]["risk_category"] in ("MEDIUM", "HIGH")
        assert result["artifact_path"] is not None

        with open(os.path.join(REPO_ROOT, "logs", f"discovery_{agent.run_id}.json")) as f:
            log = json.load(f)
        event_types = {e["event"] for e in log["events"]}
        assert "risk_assessment" in event_types
        assert "escalation_decision" in event_types
    finally:
        # This is a scripted smoke run, not a real discovery -- don't leave it in
        # artifacts/logs regardless of whether the assertions above passed. Diffing
        # directory contents (rather than guessing filenames) survives internal
        # naming/timestamp schemes in replay_engine/human_escalation changing later.
        import shutil

        for dir_path, before_entries in before.items():
            for name in _snapshot(dir_path) - before_entries:
                path = os.path.join(dir_path, name)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
