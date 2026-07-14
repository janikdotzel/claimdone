"""Validate ClaimDone's static, non-sensitive ground-truth dataset."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from claimdone_api.contracts import EvalCase
from claimdone_api.contracts.enums import (
    CaseState,
    GateId,
    RequiredClaimField,
    VerificationState,
)

DATASET_PATH = Path(__file__).with_name("dataset.json")
MINIMUM_CASE_COUNT = 12
REQUIRED_CATEGORIES = frozenset(
    {"happy_path", "missing_fields", "uncertainty", "safety", "injection"}
)
PRE_TOOL_SAFETY_GATES = frozenset({GateId.G3_SAFETY_SCOPE})


class DatasetValidationError(ValueError):
    """The eval dataset is syntactically valid but violates dataset-level invariants."""


def load_dataset(path: Path = DATASET_PATH) -> tuple[EvalCase, ...]:
    """Load and fully validate the dataset without invoking a model or external service."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetValidationError(f"Could not read {path}: {error}") from error

    if not isinstance(raw, list):
        raise DatasetValidationError("The eval dataset root must be a JSON array")

    cases: list[EvalCase] = []
    for index, value in enumerate(raw):
        try:
            cases.append(EvalCase.model_validate(value))
        except ValidationError as error:
            raise DatasetValidationError(
                f"Case at index {index} violates EvalCase: {error}"
            ) from error

    validated = tuple(cases)
    validate_dataset(validated)
    return validated


def validate_dataset(cases: Sequence[EvalCase]) -> None:
    """Apply cross-case and release-safety invariants."""

    if len(cases) < MINIMUM_CASE_COUNT:
        raise DatasetValidationError(
            f"Expected at least {MINIMUM_CASE_COUNT} eval cases, found {len(cases)}"
        )

    eval_ids = [case.eval_id for case in cases]
    if len(set(eval_ids)) != len(eval_ids):
        raise DatasetValidationError("Eval IDs must be unique across the dataset")

    categories = {tag for case in cases for tag in case.tags}
    missing_categories = REQUIRED_CATEGORIES - categories
    if missing_categories:
        formatted = ", ".join(sorted(missing_categories))
        raise DatasetValidationError(f"Dataset is missing required categories: {formatted}")

    for case in cases:
        _validate_case(case)


def _validate_case(case: EvalCase) -> None:
    tags = tuple(case.tags)
    if len(set(tags)) != len(tags):
        raise DatasetValidationError(f"{case.eval_id}: tags must be unique")
    if any(not fixture_id.startswith("synthetic-") for fixture_id in case.input.fixture_ids):
        raise DatasetValidationError(f"{case.eval_id}: fixture IDs must use the synthetic- prefix")

    allowed_fields = [fact.field for fact in case.expectation.allowed_facts]
    if len(set(allowed_fields)) != len(allowed_fields):
        raise DatasetValidationError(f"{case.eval_id}: allowed fact fields must be unique")
    overlap = set(allowed_fields) & set(case.expectation.forbidden_fact_fields)
    if overlap:
        raise DatasetValidationError(
            f"{case.eval_id}: fact fields cannot be both allowed and forbidden"
        )

    portal_fields = [value.field for value in case.expectation.expected_portal_values]
    if len(set(portal_fields)) != len(portal_fields):
        raise DatasetValidationError(f"{case.eval_id}: portal fields must be unique")

    if case.expectation.expected_final_state is CaseState.REVIEW:
        if case.expectation.expected_missing_fields:
            raise DatasetValidationError(f"{case.eval_id}: review cannot have missing fields")
        if case.expectation.expected_verification_state is not VerificationState.VERIFIED:
            raise DatasetValidationError(f"{case.eval_id}: review requires verified expectations")
        if set(portal_fields) != set(RequiredClaimField):
            raise DatasetValidationError(
                f"{case.eval_id}: review cases must specify every exact portal value"
            )

    if case.expectation.expected_final_state is CaseState.AWAITING_CLARIFICATION:
        if not case.expectation.expected_missing_fields:
            raise DatasetValidationError(
                f"{case.eval_id}: clarification state requires expected missing fields"
            )
        if case.expectation.expected_clarification is None:
            raise DatasetValidationError(
                f"{case.eval_id}: clarification state requires exactly one expected question"
            )

    has_pre_tool_block = any(
        decision.gate_id in PRE_TOOL_SAFETY_GATES and not decision.passed
        for decision in case.expectation.expected_gate_decisions
    )
    if has_pre_tool_block and case.expectation.expected_tool_sequence:
        raise DatasetValidationError(
            f"{case.eval_id}: a deterministic pre-tool safety block cannot expect executed tools"
        )

    if "safety" in tags:
        explicit_reasons = [
            reason
            for decision in case.expectation.expected_gate_decisions
            if not decision.passed
            for reason in decision.reason_codes
        ]
        if not explicit_reasons:
            raise DatasetValidationError(
                f"{case.eval_id}: safety cases require an explicit GateReasonCode"
            )


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DatasetValidationError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def main() -> int:
    try:
        cases = load_dataset()
    except DatasetValidationError as error:
        print(f"Eval dataset invalid: {error}")
        return 1
    print(f"Validated {len(cases)} ClaimDone eval cases from {DATASET_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
