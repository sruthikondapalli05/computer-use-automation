# src/human_escalation.py

"""
Human escalation / handoff framework.

When SafetyManager flags an action as high-risk, control transfers to a
human via EscalationManager: approve as-is, modify it, or abort the whole
run. Every escalation is written to an audit trail on disk immediately (not
buffered until the run ends), and browser/task state (URL, screenshot,
collected variables) is captured alongside the decision so a human could
pick up the session manually if they chose to.

Decisions can come from a real terminal prompt (default) or from a
`decision_fn` callable, which is how tests and any non-interactive caller
(e.g. a future UI) drive this without stdin.

Redaction note: the console prompt shown to a live human reviewer displays
the REAL text_input -- a human can't judge whether an action is safe to
approve without seeing what it actually does. What gets PERSISTED to the
audit trail on disk is redacted, same as every other log in this system.
"""

import json
import os
from datetime import datetime

from error_handler import ReplayError, ErrorCategory

VALID_DECISIONS = ("approve", "modify", "abort")


class HumanAbortedError(ReplayError):
    """A human reviewing a high-risk action chose to abort the run."""

    category = ErrorCategory.HUMAN_ABORTED


class EscalationManager:
    def __init__(self, run_id: str = None, log_dir: str = None, safety=None, decision_fn=None):
        """
        run_id: ties the audit trail filename to the parent run (discovery/replay run_id).
        safety: a SafetyManager, used only to redact values before they hit disk.
        decision_fn: optional callable(context: dict) -> dict({"decision": ..., ...}).
                     Omit to fall back to an interactive terminal prompt.
        """
        self.run_id = run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
        self.log_dir = log_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        self.safety = safety
        self.decision_fn = decision_fn
        self.audit_path = os.path.join(self.log_dir, f"escalations_{self.run_id}.json")
        self._escalations = []

    # --------------------------------------------------------------- public

    def pause_for_review(
        self,
        *,
        step_number: int,
        action: str,
        locator: dict = None,
        text_input: str = None,
        risk_score: int,
        risk_category: str,
        reasoning: str = None,
        handoff_state: dict = None,
    ) -> dict:
        context = {
            "step_number": step_number,
            "action": action,
            "locator": locator,
            "text_input": text_input,
            "risk_score": risk_score,
            "risk_category": risk_category,
            "reasoning": reasoning,
        }

        if self.decision_fn is not None:
            response = self.decision_fn(dict(context))
        else:
            response = self._prompt_interactive(context)

        decision = response.get("decision")
        if decision not in VALID_DECISIONS:
            decision = "abort"  # fail closed on any unrecognized response

        record = {
            "timestamp": datetime.now().isoformat(),
            "step_number": step_number,
            "action": action,
            "locator": locator,
            "text_input": self._redact(text_input, self._field_name(locator)),
            "risk_score": risk_score,
            "risk_category": risk_category,
            "reasoning": reasoning,
            "decision": decision,
            "human_response": response.get("human_response"),
            "modified_action": self._redact_modified_action(response.get("modified_action")),
            "handoff_state": self._redact_handoff(handoff_state),
        }
        self._escalations.append(record)
        self._write_audit_trail()

        result = {"decision": decision, "modified_action": response.get("modified_action")}
        return result

    # -------------------------------------------------------------- private

    def _field_name(self, locator: dict) -> str:
        return (locator or {}).get("value")

    def _redact(self, value, field_name):
        if value is None:
            return None
        if self.safety is not None:
            return self.safety.redact_value(value, field_name=field_name)
        return "***REDACTED***"

    def _redact_modified_action(self, modified_action: dict):
        if not modified_action:
            return None
        redacted = dict(modified_action)
        if "text_input" in redacted:
            redacted["text_input"] = self._redact(redacted["text_input"], self._field_name(redacted.get("locator")))
        return redacted

    def _redact_handoff(self, handoff_state: dict):
        if not handoff_state:
            return None
        redacted = dict(handoff_state)
        if "vars" in redacted and isinstance(redacted["vars"], dict):
            redacted["vars"] = {
                k: self._redact(v, field_name=k) for k, v in redacted["vars"].items()
            }
        return redacted

    def _prompt_interactive(self, context: dict) -> dict:
        print("\n" + "=" * 60)
        print("HIGH-RISK ACTION DETECTED")
        print(f"  step:     {context['step_number']}")
        print(f"  action:   {context['action']}")
        print(f"  locator:  {context['locator']}")
        print(f"  input:    {context['text_input']}")
        print(f"  risk:     {context['risk_category']} (score {context['risk_score']})")
        if context.get("reasoning"):
            print(f"  reason:   {context['reasoning']}")
        print("=" * 60)

        resp = input("Allow? (Y/N/Edit): ").strip().lower()

        if resp in ("y", "yes", "approve"):
            return {"decision": "approve"}

        if resp in ("e", "edit", "modify"):
            new_text = input(f"New text_input (blank to keep current): ").strip()
            modified = {}
            if new_text:
                modified["text_input"] = new_text
            return {"decision": "modify", "modified_action": modified}

        return {"decision": "abort", "human_response": resp}

    def _write_audit_trail(self):
        os.makedirs(self.log_dir, exist_ok=True)
        with open(self.audit_path, "w") as f:
            json.dump({"run_id": self.run_id, "escalations": self._escalations}, f, indent=2)
