import pytest

from claimdone_api.contracts.enums import AllowedTool
from evals.validate_dataset import (
    MINIMUM_CASE_COUNT,
    REQUIRED_CATEGORIES,
    DatasetValidationError,
    load_dataset,
    validate_dataset,
)


def test_dataset_loads_without_live_services() -> None:
    cases = load_dataset()

    assert len(cases) >= MINIMUM_CASE_COUNT
    assert len({case.eval_id for case in cases}) == len(cases)
    assert {tag for case in cases for tag in case.tags} >= REQUIRED_CATEGORIES


def test_every_safety_case_has_an_explicit_block_reason() -> None:
    safety_cases = [case for case in load_dataset() if "safety" in case.tags]

    assert safety_cases
    for case in safety_cases:
        reasons = [
            reason
            for gate in case.expectation.expected_gate_decisions
            if not gate.passed
            for reason in gate.reason_codes
        ]
        assert reasons


def test_pre_tool_safety_blocks_execute_no_tools() -> None:
    safety_cases = [case for case in load_dataset() if "safety" in case.tags]

    assert safety_cases
    for case in safety_cases:
        assert case.expectation.expected_tool_sequence == ()


@pytest.mark.parametrize(
    "eval_id",
    [
        "eval-safety-injury-de",
        "eval-safety-real-portal-en",
        "eval-safety-liability-payment-de",
    ],
)
def test_dataset_rejects_tools_after_pre_tool_safety_block(eval_id: str) -> None:
    cases = load_dataset()
    case_index = next(index for index, case in enumerate(cases) if case.eval_id == eval_id)
    case = cases[case_index]
    contradictory_expectation = case.expectation.model_copy(
        update={"expected_tool_sequence": (AllowedTool.INSPECT_EVIDENCE,)}
    )
    contradictory_case = case.model_copy(update={"expectation": contradictory_expectation})
    contradictory_cases = (*cases[:case_index], contradictory_case, *cases[case_index + 1 :])

    with pytest.raises(DatasetValidationError, match="pre-tool safety block"):
        validate_dataset(contradictory_cases)


def test_dataset_rejects_duplicate_eval_ids() -> None:
    cases = load_dataset()
    duplicate = cases[-1].model_copy(update={"eval_id": cases[0].eval_id})

    with pytest.raises(DatasetValidationError, match="unique"):
        validate_dataset((*cases[:-1], duplicate))
