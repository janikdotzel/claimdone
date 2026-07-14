import pytest

from evals.validate_dataset import (
    REQUIRED_CATEGORIES,
    DatasetValidationError,
    load_dataset,
    validate_dataset,
)


def test_dataset_loads_without_live_services() -> None:
    cases = load_dataset()

    assert len(cases) == 12
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


def test_dataset_rejects_duplicate_eval_ids() -> None:
    cases = load_dataset()
    duplicate = cases[-1].model_copy(update={"eval_id": cases[0].eval_id})

    with pytest.raises(DatasetValidationError, match="unique"):
        validate_dataset((*cases[:-1], duplicate))
