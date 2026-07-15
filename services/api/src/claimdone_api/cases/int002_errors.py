"""Value-free HTTP failures for the canonical INT-002 mutation boundary."""

import re
from dataclasses import dataclass

from claimdone_api.contracts import GateDecision

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_UNSAFE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


@dataclass(frozen=True, slots=True)
class Int002HttpError(RuntimeError):
    """One pre-redacted failure safe to serialize at the public API boundary.

    ``safe_message`` and ``field`` must never be constructed from request content.
    Callers should use the fixed constructors below for expected failures.
    """

    code: str
    safe_message: str
    status_code: int
    current_version: int | None = None
    gate_decision: GateDecision | None = None
    field: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not str or _ERROR_CODE.fullmatch(self.code) is None:
            raise ValueError("INT-002 error code is not safe")
        if not _safe_text(self.safe_message, maximum=512):
            raise ValueError("INT-002 error message is not safe")
        if type(self.status_code) is not int or self.status_code < 400 or self.status_code > 599:
            raise ValueError("INT-002 status code is invalid")
        if self.current_version is not None and (
            type(self.current_version) is not int
            or self.current_version < 1
            or self.current_version > _SQLITE_MAX_INTEGER
        ):
            raise ValueError("INT-002 current version is invalid")
        if self.gate_decision is not None and (
            not isinstance(self.gate_decision, GateDecision) or self.gate_decision.passed
        ):
            raise ValueError("INT-002 error gate decision must be a canonical failure")
        if self.field is not None and not _safe_text(self.field, maximum=256):
            raise ValueError("INT-002 error field is not safe")

    def __str__(self) -> str:
        return self.safe_message


def request_validation_failed() -> Int002HttpError:
    return Int002HttpError(
        code="REQUEST_VALIDATION_FAILED",
        safe_message="The request does not match the closed API contract.",
        status_code=422,
        field="request",
    )


def intake_form_invalid(*, safe_message: str, field: str) -> Int002HttpError:
    """Create a form error only from fixed, developer-owned text."""

    return Int002HttpError(
        code="INTAKE_FORM_INVALID",
        safe_message=safe_message,
        status_code=422,
        field=field,
    )


def request_identity_mismatch() -> Int002HttpError:
    return Int002HttpError(
        code="REQUEST_IDENTITY_MISMATCH",
        safe_message="The request identity does not match the selected workflow resource.",
        status_code=422,
        field="request",
    )


def workflow_case_not_found() -> Int002HttpError:
    return Int002HttpError(
        code="WORKFLOW_CASE_NOT_FOUND",
        safe_message="The workflow case does not exist.",
        status_code=404,
    )


def workflow_version_conflict(*, current_version: int) -> Int002HttpError:
    return Int002HttpError(
        code="WORKFLOW_VERSION_CONFLICT",
        safe_message="The workflow case changed before this request was applied.",
        status_code=409,
        current_version=current_version,
    )


def workflow_state_conflict(*, current_version: int | None = None) -> Int002HttpError:
    return Int002HttpError(
        code="WORKFLOW_STATE_CONFLICT",
        safe_message="The workflow case is not at the required mutation boundary.",
        status_code=409,
        current_version=current_version,
    )


def workflow_gate_blocked(
    decision: GateDecision,
    *,
    status_code: int = 422,
) -> Int002HttpError:
    return Int002HttpError(
        code="WORKFLOW_GATE_BLOCKED",
        safe_message="A deterministic workflow gate blocked this request.",
        status_code=status_code,
        gate_decision=decision,
    )


def fixture_rejected() -> Int002HttpError:
    return Int002HttpError(
        code="INT002_FIXTURE_REJECTED",
        safe_message="The staged input is not the approved deterministic demo fixture.",
        status_code=422,
    )


def composition_conflict(*, current_version: int | None = None) -> Int002HttpError:
    return Int002HttpError(
        code="INT002_COMPOSITION_CONFLICT",
        safe_message="The persisted workflow authority cannot continue this request.",
        status_code=409,
        current_version=current_version,
    )


def composition_failed() -> Int002HttpError:
    return Int002HttpError(
        code="INT002_COMPOSITION_FAILED",
        safe_message="The local sandbox workflow could not complete the request.",
        status_code=502,
    )


def workflow_input_rejected() -> Int002HttpError:
    """Compatibility name for callers that classify by workflow boundary."""

    return fixture_rejected()


def workflow_upstream_unavailable() -> Int002HttpError:
    """Compatibility name for callers that classify by upstream availability."""

    return composition_failed()


def workflow_internal_error() -> Int002HttpError:
    return Int002HttpError(
        code="WORKFLOW_INTERNAL_ERROR",
        safe_message="The workflow request could not be completed safely.",
        status_code=500,
    )


def _safe_text(value: object, *, maximum: int) -> bool:
    return (
        type(value) is str and 1 <= len(value) <= maximum and _UNSAFE_CONTROL.search(value) is None
    )
