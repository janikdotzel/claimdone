from itertools import pairwise

import pytest

from claimdone_api.contracts import (
    CASE_TRANSITIONS,
    TERMINAL_CASE_STATES,
    CaseState,
    InvalidCaseTransition,
    is_case_transition_allowed,
    validate_case_transition,
)

NORMAL_PATH = (
    CaseState.CREATED,
    CaseState.DISCLOSED,
    CaseState.ANALYZING,
    CaseState.AWAITING_CLARIFICATION,
    CaseState.READY_TO_FILL,
    CaseState.FILLING,
    CaseState.VERIFYING,
    CaseState.REVIEW,
    CaseState.HUMAN_APPROVED,
    CaseState.RECEIPT,
)


def test_normal_workflow_path_is_explicitly_allowed() -> None:
    for current, target in pairwise(NORMAL_PATH):
        validate_case_transition(current, target)
        assert is_case_transition_allowed(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (CaseState.CREATED, CaseState.ANALYZING),
        (CaseState.ANALYZING, CaseState.READY_TO_FILL),
        (CaseState.VERIFYING, CaseState.HUMAN_APPROVED),
        (CaseState.BLOCKED, CaseState.HUMAN_APPROVED),
        (CaseState.REVIEW, CaseState.RECEIPT),
        (CaseState.RECEIPT, CaseState.CREATED),
    ],
)
def test_skipped_or_unsafe_transitions_are_rejected(current: CaseState, target: CaseState) -> None:
    assert not is_case_transition_allowed(current, target)
    with pytest.raises(InvalidCaseTransition):
        validate_case_transition(current, target)


def test_transition_table_covers_every_state_once() -> None:
    assert set(CASE_TRANSITIONS) == set(CaseState)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert {
        CaseState.RECEIPT,
        CaseState.BLOCKED,
        CaseState.EMERGENCY_STOPPED,
        CaseState.ABANDONED,
        CaseState.FAILED,
    } == TERMINAL_CASE_STATES
    assert all(not CASE_TRANSITIONS[state] for state in TERMINAL_CASE_STATES)
