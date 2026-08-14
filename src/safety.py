# src/safety.py

"""
Safety layer: allowlist enforcement, PII/secret redaction, and risk scoring.

Two different kinds of "no" live here, and they are not interchangeable:

- Allowlist violations (SafetyViolation) are a hard, fail-closed reject.
  There is no human override path -- if a step tries to navigate or lands
  on a domain that isn't allowlisted, or matches a blocked pattern, the run
  stops. This is what "no security bypass paths" means in practice: risk
  scoring and human escalation never get a chance to approve their way
  around the allowlist.
- Risk scoring (categorize_risk / score_action) is a soft gate for actions
  that ARE permitted but touch something sensitive (a password field, a
  credit-card-shaped value, a delete/transfer action). Those go to a human
  for a yes/no/edit decision via EscalationManager, not an automatic reject.

Redaction is intentionally applied to LOGS ONLY. The artifact itself must
keep real values (a replay that fills in "***REDACTED***" as a password is
useless), so redaction lives entirely on the logging path, never on
anything written into an Artifact.
"""

import re
from urllib.parse import urlparse

from error_handler import ReplayError, ErrorCategory

DEFAULT_RISK_THRESHOLD = 70

DEFAULT_SENSITIVE_FIELDS = (
    "password", "credit_card", "card_number", "cvv", "ssn", "social_security",
    "token", "api_key", "secret", "pin", "account_number", "routing_number",
)

# (name, compiled pattern) -- order matters only for readability of redacted output.
PII_PATTERNS = (
    ("credit_card", re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk|xox[baprs])-[A-Za-z0-9_-]{8,}\b")),
)

RISKY_ACTION_KEYWORDS = ("delete", "remove", "transfer", "withdraw", "wire", "pay", "send money", "close account")


class SafetyViolation(ReplayError):
    """Allowlist / blocked-pattern rejection. Fail-closed, never retried, never overridable."""

    category = ErrorCategory.SAFETY_VIOLATION


class SafetyManager:
    def __init__(self, config: dict = None):
        config = config or {}
        self.allowed_domains = [d.lower().lstrip(".") for d in config.get("allowed_domains", [])]
        self.blocked_patterns = [re.compile(p, re.IGNORECASE) for p in config.get("blocked_patterns", [])]
        self.sensitive_fields = tuple(f.lower() for f in config.get("sensitive_fields", DEFAULT_SENSITIVE_FIELDS))
        self.risk_threshold = config.get("risk_threshold", DEFAULT_RISK_THRESHOLD)

    # ------------------------------------------------------------ allowlist

    def is_url_allowed(self, url: str) -> bool:
        """Fail-closed: an empty allowlist permits nothing. Blocked patterns override the allowlist."""
        if not url:
            return False

        for pattern in self.blocked_patterns:
            if pattern.search(url):
                return False

        if not self.allowed_domains:
            return False

        parsed = urlparse(url if "://" in url else f"//{url}", scheme="")
        netloc = (parsed.netloc or parsed.path).lower().split(":")[0]
        netloc = netloc[4:] if netloc.startswith("www.") else netloc

        return any(netloc == d or netloc.endswith(f".{d}") for d in self.allowed_domains)

    def enforce_url_allowed(self, url: str, step_number: int = None):
        if not self.is_url_allowed(url):
            raise SafetyViolation(
                f"URL '{url}' is outside the allowlist or matches a blocked pattern",
                step_number=step_number,
                details={"url": url, "allowed_domains": self.allowed_domains},
            )

    # ------------------------------------------------------------ redaction

    def _pii_matches(self, text: str) -> list:
        if not text:
            return []
        return [name for name, pattern in PII_PATTERNS if pattern.search(text)]

    def contains_sensitive_data(self, text: str) -> bool:
        return bool(self._pii_matches(text))

    def redact_text(self, text: str) -> str:
        """Pattern-based redaction: masks recognizable PII/secret shapes wherever they appear."""
        if not text:
            return text
        redacted = text
        for name, pattern in PII_PATTERNS:
            redacted = pattern.sub(f"***REDACTED_{name.upper()}***", redacted)
        return redacted

    def redact_value(self, value: str, field_name: str = None) -> str:
        """
        Full redaction for known-sensitive fields (passwords, tokens -- values
        with no distinctive shape a regex could catch), falling back to
        pattern-based partial redaction for everything else.
        """
        if value is None:
            return None
        if field_name and any(f in field_name.lower() for f in self.sensitive_fields):
            return "***REDACTED***"
        return self.redact_text(value)

    # --------------------------------------------------------- risk scoring

    def _get_locator_value(self, locator) -> str:
        """Extract value from locator regardless of type (string, dict, or object)."""
        if not locator:
            return ""
        if isinstance(locator, str):
            return locator
        if isinstance(locator, dict):
            return locator.get("value", "")
        return getattr(locator, "value", "")

    def _field_is_sensitive(self, locator) -> bool:
        if not locator:
            return False
        value = self._get_locator_value(locator).lower()
        return any(f in value for f in self.sensitive_fields)

    def _has_risky_keyword(self, *texts: str) -> bool:
        joined = " ".join(t for t in texts if t).lower()
        return any(k in joined for k in RISKY_ACTION_KEYWORDS)

    def categorize_risk(self, action: str, locator = None, text_input: str = None) -> str:
        sensitive_text = self.contains_sensitive_data(text_input)
        sensitive_field = self._field_is_sensitive(locator)

        if action == "navigate":
            target = text_input or self._get_locator_value(locator) or ""
            if target and not self.is_url_allowed(target) and sensitive_text:
                return "HIGH"

        if sensitive_field or sensitive_text:
            return "MEDIUM"

        locator_value = self._get_locator_value(locator)
        locator_reason = (locator or {}).get("robustness_reason", "") if isinstance(locator, dict) else ""
        if self._has_risky_keyword(locator_value, locator_reason):
            return "MEDIUM"

        return "LOW"

    def score_action(self, action: str, locator = None, text_input: str = None) -> int:
        category = self.categorize_risk(action, locator, text_input)
        # MEDIUM is set so a bare sensitive-field-name match (e.g. a routine password
        # login field) stays under the review threshold, but any actual PII-shaped
        # value (credit card, SSN, ...) pushes it over -- routine logins shouldn't
        # need a human every run; moving real financial/PII data should.
        score = {"LOW": 15, "MEDIUM": 65, "HIGH": 90}[category]

        score += 10 * len(self._pii_matches(text_input))

        if action == "navigate":
            target = text_input or self._get_locator_value(locator) or ""
            if target and not self.is_url_allowed(target):
                score += 30

        locator_value = self._get_locator_value(locator)
        locator_reason = (locator or {}).get("robustness_reason", "") if isinstance(locator, dict) else ""
        if self._has_risky_keyword(locator_value, locator_reason):
            score += 20

        return min(score, 100)

    def needs_human_review(self, score: int) -> bool:
        return score > self.risk_threshold
