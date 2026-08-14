# src/error_handler.py

"""
Error taxonomy for deterministic replay.

Replay failures are not all equal. We distinguish three categories so the
engine (and any caller escalating to a human) can react appropriately:

- ExpectedOutcomeError: the run reached a valid business result that isn't
  the happy path (e.g. "member not found"). Not a bug, not a crash.
- RecoverableError: a transient condition (timeout, element not yet
  rendered, a dismissible dialog) worth retrying before giving up.
- HardFailureError: retries exhausted or the situation can't be resolved
  automatically (element never appeared, navigation failed, checkpoint not
  met). This is what triggers human escalation.
"""

from enum import Enum


class ErrorCategory(str, Enum):
    EXPECTED_OUTCOME = "expected_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class ReplayError(Exception):
    """Base class for all replay errors. Carries step context for logging."""

    category: ErrorCategory = ErrorCategory.HARD_FAILURE

    def __init__(self, message: str, step_number: int = None, details: dict = None):
        super().__init__(message)
        self.message = message
        self.step_number = step_number
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "message": self.message,
            "step_number": self.step_number,
            "details": self.details,
        }


class ExpectedOutcomeError(ReplayError):
    """A valid, named business result that isn't the happy path."""

    category = ErrorCategory.EXPECTED_OUTCOME


class RecoverableError(ReplayError):
    """A transient condition that is worth retrying."""

    category = ErrorCategory.RECOVERABLE


class HardFailureError(ReplayError):
    """Retries exhausted, or an unrecoverable condition. Escalate to a human."""

    category = ErrorCategory.HARD_FAILURE


def classify_playwright_exception(exc: Exception) -> ErrorCategory:
    """
    Map a raw Playwright exception to an error category. Timeouts on
    individual actions are treated as recoverable (worth a retry); anything
    else (bad selector syntax, closed browser/context, navigation errors)
    is treated as a hard failure.
    """
    name = type(exc).__name__
    message = str(exc).lower()

    if "timeout" in name.lower() or "timeout" in message:
        return ErrorCategory.RECOVERABLE

    return ErrorCategory.HARD_FAILURE
