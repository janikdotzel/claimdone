import pytest

from claimdone_api.contracts import EvalCase
from claimdone_api.contracts.enums import AllowedTool, FactStatus, GateId, GateReasonCode
from evals.validate_dataset import (
    EXPECTED_CASE_COUNT,
    PRE_TOOL_SAFETY_GATES,
    REQUIRED_CATEGORIES,
    DatasetValidationError,
    load_dataset,
    validate_dataset,
)


def _has_pre_tool_safety_block(case: EvalCase) -> bool:
    return any(
        decision.gate_id in PRE_TOOL_SAFETY_GATES and not decision.passed
        for decision in case.expectation.expected_gate_decisions
    )


def test_dataset_loads_without_live_services() -> None:
    cases = load_dataset()

    assert len(cases) == EXPECTED_CASE_COUNT
    assert len({case.eval_id for case in cases}) == len(cases)
    assert {tag for case in cases for tag in case.tags} >= REQUIRED_CATEGORIES


def test_every_portal_source_resolves_to_a_unique_input_catalog_entry() -> None:
    for case in load_dataset():
        source_catalog = set(case.input.fixture_ids)
        assert len(source_catalog) == len(case.input.fixture_ids)
        for portal_value in case.expectation.expected_portal_values:
            assert len(set(portal_value.source_refs)) == len(portal_value.source_refs)
            assert set(portal_value.source_refs) <= source_catalog


def test_clarification_limit_reproduces_consumed_budget_without_another_question() -> None:
    case = next(
        case for case in load_dataset() if case.eval_id == "eval-clarification-limit-en"
    )

    assert case.input.completed_clarification_rounds == 3
    assert case.expectation.expected_clarification is None
    assert AllowedTool.ASK_CLARIFICATION not in case.expectation.allowed_tools
    assert AllowedTool.ASK_CLARIFICATION not in case.expectation.expected_tool_sequence


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
    pre_tool_blocked_cases = [case for case in load_dataset() if _has_pre_tool_safety_block(case)]

    assert pre_tool_blocked_cases
    for case in pre_tool_blocked_cases:
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


def test_g3_pre_tool_block_does_not_depend_on_safety_tag() -> None:
    cases = load_dataset()
    case_index = next(
        index for index, case in enumerate(cases) if case.eval_id == "eval-safety-injury-de"
    )
    case = cases[case_index]
    contradictory_expectation = case.expectation.model_copy(
        update={"expected_tool_sequence": (AllowedTool.INSPECT_EVIDENCE,)}
    )
    contradictory_case = case.model_copy(
        update={
            "tags": tuple(tag for tag in case.tags if tag != "safety"),
            "expectation": contradictory_expectation,
        }
    )
    assert "safety" not in contradictory_case.tags
    contradictory_cases = (*cases[:case_index], contradictory_case, *cases[case_index + 1 :])

    with pytest.raises(DatasetValidationError, match="pre-tool safety block"):
        validate_dataset(contradictory_cases)


def test_safety_tag_does_not_turn_a_later_gate_failure_into_a_pre_tool_block() -> None:
    cases = load_dataset()
    case_index = next(
        index for index, case in enumerate(cases) if case.eval_id == "eval-injection-unknown-tool"
    )
    case = cases[case_index]
    safety_tagged_case = case.model_copy(update={"tags": (*case.tags, "safety")})

    assert safety_tagged_case.expectation.expected_tool_sequence == (AllowedTool.INSPECT_FORM,)
    assert not _has_pre_tool_safety_block(safety_tagged_case)

    validate_dataset((*cases[:case_index], safety_tagged_case, *cases[case_index + 1 :]))


def test_dataset_rejects_duplicate_eval_ids() -> None:
    cases = load_dataset()
    duplicate = cases[-1].model_copy(update={"eval_id": cases[0].eval_id})

    with pytest.raises(DatasetValidationError, match="unique"):
        validate_dataset((*cases[:-1], duplicate))


def test_dataset_rejects_a_case_count_other_than_twelve() -> None:
    cases = load_dataset()

    with pytest.raises(DatasetValidationError, match="exactly 12"):
        validate_dataset(cases[:-1])

    extra = cases[-1].model_copy(update={"eval_id": "eval-unplanned-thirteenth-case"})
    with pytest.raises(DatasetValidationError, match="exactly 12"):
        validate_dataset((*cases, extra))


def test_dataset_rejects_portal_source_outside_the_input_catalog() -> None:
    cases = load_dataset()
    case = cases[0]
    portal_value = case.expectation.expected_portal_values[0].model_copy(
        update={"source_refs": ("synthetic-source-not-catalogued",)}
    )
    expectation = case.expectation.model_copy(
        update={
            "expected_portal_values": (
                portal_value,
                *case.expectation.expected_portal_values[1:],
            )
        }
    )
    mutated = case.model_copy(update={"expectation": expectation})

    with pytest.raises(DatasetValidationError, match="resolve to input fixture IDs"):
        validate_dataset((mutated, *cases[1:]))


def test_dataset_rejects_duplicate_portal_source_refs() -> None:
    cases = load_dataset()
    case = cases[0]
    source_ref = case.expectation.expected_portal_values[0].source_refs[0]
    portal_value = case.expectation.expected_portal_values[0].model_copy(
        update={"source_refs": (source_ref, source_ref)}
    )
    expectation = case.expectation.model_copy(
        update={
            "expected_portal_values": (
                portal_value,
                *case.expectation.expected_portal_values[1:],
            )
        }
    )
    mutated = case.model_copy(update={"expectation": expectation})

    with pytest.raises(DatasetValidationError, match="source refs must be unique"):
        validate_dataset((mutated, *cases[1:]))


def test_dataset_rejects_clarification_limit_before_the_budget_is_consumed() -> None:
    cases = load_dataset()
    case_index = next(
        index
        for index, case in enumerate(cases)
        if case.eval_id == "eval-clarification-limit-en"
    )
    case = cases[case_index]
    mutated_input = case.input.model_copy(update={"completed_clarification_rounds": 2})
    mutated = case.model_copy(update={"input": mutated_input})

    with pytest.raises(DatasetValidationError, match="three completed rounds"):
        validate_dataset((*cases[:case_index], mutated, *cases[case_index + 1 :]))


@pytest.mark.parametrize("mutate_sequence", [False, True])
def test_dataset_rejects_another_question_after_clarification_limit(
    mutate_sequence: bool,
) -> None:
    cases = load_dataset()
    case_index = next(
        index
        for index, case in enumerate(cases)
        if case.eval_id == "eval-clarification-limit-en"
    )
    case = cases[case_index]
    update: dict[str, object]
    if mutate_sequence:
        update = {
            "expected_tool_sequence": (
                *case.expectation.expected_tool_sequence,
                AllowedTool.ASK_CLARIFICATION,
            )
        }
    else:
        update = {"expected_clarification": "Ask a fourth clarification?"}
    expectation = case.expectation.model_copy(update=update)
    mutated = case.model_copy(update={"expectation": expectation})

    with pytest.raises(DatasetValidationError, match="cannot (expect|allow) another question"):
        validate_dataset((*cases[:case_index], mutated, *cases[case_index + 1 :]))


def test_exhausted_budget_cannot_be_bypassed_by_omitting_limit_reason() -> None:
    cases = load_dataset()
    case_index = next(
        index
        for index, case in enumerate(cases)
        if case.eval_id == "eval-clarification-limit-en"
    )
    case = cases[case_index]
    gate = case.expectation.expected_gate_decisions[0].model_copy(
        update={"reason_codes": (GateReasonCode.G5_REQUIRED_FIELD_MISSING,)}
    )
    expectation = case.expectation.model_copy(
        update={
            "expected_clarification": "Ask a fourth clarification?",
            "expected_gate_decisions": (gate,),
        }
    )
    mutated = case.model_copy(update={"expectation": expectation})

    assert GateReasonCode.G5_CLARIFICATION_LIMIT not in gate.reason_codes
    with pytest.raises(DatasetValidationError, match="exhausted clarification budget"):
        validate_dataset((*cases[:case_index], mutated, *cases[case_index + 1 :]))


def test_conflict_case_uses_two_distinct_supported_allowed_facts() -> None:
    case = next(
        case
        for case in load_dataset()
        if case.eval_id == "eval-uncertain-conflicting-impact"
    )
    facts = case.expectation.allowed_facts

    assert len(facts) == 2
    assert facts[0].field is facts[1].field
    assert (type(facts[0].value), facts[0].value) != (
        type(facts[1].value),
        facts[1].value,
    )
    assert {fact.status for fact in facts} == {
        FactStatus.USER_STATED,
        FactStatus.OBSERVED,
    }


@pytest.mark.parametrize("mutation", ["same_value", "unsupported", "missing", "third"])
def test_dataset_rejects_malformed_or_unpaired_fact_conflicts(mutation: str) -> None:
    cases = load_dataset()
    case_index = next(
        index
        for index, case in enumerate(cases)
        if case.eval_id == "eval-uncertain-conflicting-impact"
    )
    case = cases[case_index]
    first, second = case.expectation.allowed_facts
    allowed_facts = case.expectation.allowed_facts
    if mutation == "same_value":
        allowed_facts = (first, second.model_copy(update={"value": first.value}))
    elif mutation == "unsupported":
        allowed_facts = (first, second.model_copy(update={"status": FactStatus.UNKNOWN}))
    elif mutation == "third":
        allowed_facts = (
            first,
            second,
            first.model_copy(update={"value": "front_contact"}),
        )
    else:
        allowed_facts = (first,)
    expectation = case.expectation.model_copy(update={"allowed_facts": allowed_facts})
    mutated = case.model_copy(update={"expectation": expectation})

    with pytest.raises(
        DatasetValidationError,
        match="distinct supported conflicts|must match G4_CONFLICTING_SOURCES",
    ):
        validate_dataset((*cases[:case_index], mutated, *cases[case_index + 1 :]))


@pytest.mark.parametrize("mutation", ["duplicate", "reverse", "continue_with_g11"])
def test_dataset_rejects_noncanonical_expected_gate_history(mutation: str) -> None:
    cases = load_dataset()
    if mutation == "continue_with_g11":
        case_index = next(
            index
            for index, case in enumerate(cases)
            if case.eval_id == "eval-missing-date-de"
        )
    else:
        case_index = next(
            index for index, case in enumerate(cases) if case.eval_id == "eval-happy-de-a"
        )
    case = cases[case_index]
    gates = case.expectation.expected_gate_decisions
    if mutation == "duplicate":
        changed_gates = (gates[0], gates[0], *gates[1:])
        message = "expected gate IDs must be unique"
    elif mutation == "reverse":
        changed_gates = tuple(reversed(gates))
        message = "strictly increasing order"
    else:
        g11 = gates[-1].model_copy(
            update={
                "gate_id": GateId.G11_RELEASE,
                "passed": True,
                "reason_codes": (),
            }
        )
        changed_gates = (*gates, g11)
        message = "stop after its first failure"
    expectation = case.expectation.model_copy(
        update={"expected_gate_decisions": changed_gates}
    )
    changed = case.model_copy(update={"expectation": expectation})

    with pytest.raises(DatasetValidationError, match=message):
        validate_dataset((*cases[:case_index], changed, *cases[case_index + 1 :]))


@pytest.mark.parametrize(
    "unhandled_gate",
    [GateId.G0_INTAKE, GateId.G1_PRIVACY, GateId.G11_RELEASE],
)
def test_dataset_rejects_expected_gate_without_eval_002_grader(
    unhandled_gate: GateId,
) -> None:
    cases = load_dataset()
    case = cases[0]
    gate = case.expectation.expected_gate_decisions[0].model_copy(
        update={"gate_id": unhandled_gate}
    )
    expectation = case.expectation.model_copy(update={"expected_gate_decisions": (gate,)})
    changed = case.model_copy(update={"expectation": expectation})

    with pytest.raises(DatasetValidationError, match="not owned by an EVAL-002 grader"):
        validate_dataset((changed, *cases[1:]))
