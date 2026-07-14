"""Single authoritative ClaimDone case-state transition table."""

from types import MappingProxyType

from .enums import CaseState

_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.CREATED: frozenset({CaseState.DISCLOSED, CaseState.ABANDONED, CaseState.FAILED}),
    CaseState.DISCLOSED: frozenset(
        {
            CaseState.ANALYZING,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
            CaseState.ABANDONED,
            CaseState.FAILED,
        }
    ),
    CaseState.ANALYZING: frozenset(
        {
            CaseState.AWAITING_CLARIFICATION,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
            CaseState.ABANDONED,
            CaseState.FAILED,
        }
    ),
    CaseState.AWAITING_CLARIFICATION: frozenset(
        {
            CaseState.READY_TO_FILL,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
            CaseState.ABANDONED,
            CaseState.FAILED,
        }
    ),
    CaseState.READY_TO_FILL: frozenset(
        {
            CaseState.FILLING,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
            CaseState.ABANDONED,
            CaseState.FAILED,
        }
    ),
    CaseState.FILLING: frozenset(
        {
            CaseState.VERIFYING,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
            CaseState.ABANDONED,
            CaseState.FAILED,
        }
    ),
    CaseState.VERIFYING: frozenset(
        {
            CaseState.REVIEW,
            CaseState.BLOCKED,
            CaseState.EMERGENCY_STOPPED,
            CaseState.ABANDONED,
            CaseState.FAILED,
        }
    ),
    CaseState.REVIEW: frozenset({CaseState.HUMAN_APPROVED, CaseState.ABANDONED, CaseState.FAILED}),
    CaseState.HUMAN_APPROVED: frozenset({CaseState.RECEIPT, CaseState.FAILED}),
    CaseState.RECEIPT: frozenset(),
    CaseState.BLOCKED: frozenset(),
    CaseState.EMERGENCY_STOPPED: frozenset(),
    CaseState.ABANDONED: frozenset(),
    CaseState.FAILED: frozenset(),
}

CASE_TRANSITIONS = MappingProxyType(_TRANSITIONS)
TERMINAL_CASE_STATES = frozenset(
    {
        CaseState.RECEIPT,
        CaseState.BLOCKED,
        CaseState.EMERGENCY_STOPPED,
        CaseState.ABANDONED,
        CaseState.FAILED,
    }
)


class InvalidCaseTransition(ValueError):
    """Raised when a caller attempts a transition outside the canonical table."""


def is_case_transition_allowed(current: CaseState, target: CaseState) -> bool:
    """Return whether ``current -> target`` is an explicitly allowed transition."""

    return target in CASE_TRANSITIONS[current]


def validate_case_transition(current: CaseState, target: CaseState) -> None:
    """Reject transitions not present in the canonical table."""

    if not is_case_transition_allowed(current, target):
        raise InvalidCaseTransition(f"Invalid case transition: {current.value} -> {target.value}")
