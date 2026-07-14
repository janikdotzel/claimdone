"""Ground-truth evaluation contract definitions."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import (
    AlwaysTrue,
    Confidence,
    ContractModel,
    ContractVersion,
    Identifier,
    JsonScalar,
    NonEmptyText,
    ShortText,
    StrictBoolean,
    StrictInteger,
)
from .enums import (
    AllowedTool,
    CaseState,
    EvalPriority,
    EvaluationMode,
    EvidenceField,
    FactStatus,
    GateId,
    GateReasonCode,
    RequiredClaimField,
    VerificationState,
)


class EvalInput(ContractModel):
    """Non-sensitive fixture references and natural-language input."""

    fixture_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    statement: NonEmptyText | None
    transcript: NonEmptyText | None
    completed_clarification_rounds: Annotated[StrictInteger, Field(ge=0, le=3)]
    language: Literal["de", "en"]
    portal_variant: Literal["A", "B"]

    @model_validator(mode="after")
    def require_one_text_input(self) -> Self:
        if (self.statement is None) is (self.transcript is None):
            raise ValueError("Eval input requires exactly one of statement or transcript")
        if len(set(self.fixture_ids)) != len(self.fixture_ids):
            raise ValueError("Eval input fixture IDs must be unique")
        return self


class FactExpectation(ContractModel):
    """Ground truth for one allowed evidence fact."""

    field: EvidenceField
    status: FactStatus
    value: JsonScalar
    minimum_confidence: Confidence | None

    @model_validator(mode="after")
    def validate_expected_value(self) -> Self:
        if self.status in {FactStatus.UNKNOWN, FactStatus.NOT_SUPPORTED}:
            if self.value is not None or self.minimum_confidence is not None:
                raise ValueError(
                    "Unsupported fact expectations cannot require a value or confidence"
                )
            return self
        if self.value is None:
            raise ValueError("Supported fact expectations require a value")
        if self.status is FactStatus.OBSERVED and self.minimum_confidence is None:
            raise ValueError("Observed fact expectations require minimumConfidence")
        if self.status is not FactStatus.OBSERVED and self.minimum_confidence is not None:
            raise ValueError("Only observed fact expectations may set minimumConfidence")
        return self


class ExpectedGateDecision(ContractModel):
    """Expected deterministic outcome for one gate in an eval case."""

    gate_id: GateId
    deterministic: AlwaysTrue
    passed: StrictBoolean
    reason_codes: tuple[GateReasonCode, ...]

    @model_validator(mode="after")
    def validate_reason_codes(self) -> Self:
        if self.passed and self.reason_codes:
            raise ValueError("A passing expected gate cannot contain blocking reasons")
        if not self.passed and not self.reason_codes:
            raise ValueError("A failing expected gate requires a reason code")
        prefix = f"{self.gate_id.value}_"
        if any(not reason.value.startswith(prefix) for reason in self.reason_codes):
            raise ValueError("Expected gate reason code belongs to a different gate")
        return self


class PortalValueExpectation(ContractModel):
    """Exact portal value and its expected source pointers."""

    field: RequiredClaimField
    value: JsonScalar
    source_refs: Annotated[tuple[Identifier, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_unique_sources(self) -> Self:
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Expected portal source refs must be unique")
        return self


class DeterministicEvalExpectation(ContractModel):
    """A binary or structured check that never delegates to a model grader."""

    check_id: Identifier
    deterministic: AlwaysTrue
    expected_passed: StrictBoolean
    expected_reason_codes: tuple[GateReasonCode, ...]


class ModelGraderExpectation(ContractModel):
    """Quality-only threshold; it has no authority over deterministic gates."""

    grader_id: Identifier
    minimum_score: Confidence
    hard_floor: Confidence

    @model_validator(mode="after")
    def validate_floor(self) -> Self:
        if self.hard_floor > self.minimum_score:
            raise ValueError("A model grader hard floor cannot exceed its minimum score")
        return self


class EvalExpectation(ContractModel):
    """Complete, machine-readable ground truth for an eval case."""

    allowed_facts: tuple[FactExpectation, ...]
    forbidden_fact_fields: tuple[EvidenceField, ...]
    expected_missing_fields: tuple[RequiredClaimField, ...]
    expected_clarification: ShortText | None
    allowed_tools: tuple[AllowedTool, ...]
    expected_tool_sequence: tuple[AllowedTool, ...]
    expected_gate_decisions: Annotated[tuple[ExpectedGateDecision, ...], Field(min_length=1)]
    expected_portal_values: tuple[PortalValueExpectation, ...]
    expected_verification_state: VerificationState
    expected_final_state: CaseState
    deterministic_checks: Annotated[tuple[DeterministicEvalExpectation, ...], Field(min_length=1)]
    model_grader_thresholds: tuple[ModelGraderExpectation, ...]

    @model_validator(mode="after")
    def validate_unique_expectations(self) -> Self:
        gate_ids = [decision.gate_id for decision in self.expected_gate_decisions]
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError("Expected gate IDs must be unique")
        check_ids = [check.check_id for check in self.deterministic_checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("Deterministic eval check IDs must be unique")
        if any(tool not in self.allowed_tools for tool in self.expected_tool_sequence):
            raise ValueError("Expected tools must be contained in allowedTools")
        return self


class EvalCase(ContractModel):
    """Versioned eval input and ground truth, usable without a live model."""

    contract_version: ContractVersion
    eval_id: Identifier
    title: ShortText
    priority: EvalPriority
    evaluation_mode: EvaluationMode
    release_blocking: StrictBoolean
    tags: tuple[Identifier, ...]
    input: EvalInput
    expectation: EvalExpectation

    @model_validator(mode="after")
    def validate_grader_mode(self) -> Self:
        if (
            self.evaluation_mode is EvaluationMode.DETERMINISTIC
            and self.expectation.model_grader_thresholds
        ):
            raise ValueError("Deterministic eval cases cannot require model graders")

        source_catalog = set(self.input.fixture_ids)
        unresolved_sources = {
            source_ref
            for portal_value in self.expectation.expected_portal_values
            for source_ref in portal_value.source_refs
            if source_ref not in source_catalog
        }
        if unresolved_sources:
            raise ValueError("Expected portal source refs must resolve to input fixture IDs")

        clarification_limit_expected = any(
            decision.gate_id is GateId.G5_COMPLETENESS
            and GateReasonCode.G5_CLARIFICATION_LIMIT in decision.reason_codes
            for decision in self.expectation.expected_gate_decisions
        )
        if (
            clarification_limit_expected
            and self.input.completed_clarification_rounds != 3
        ):
            raise ValueError("G5 clarification limit requires three completed rounds")
        if self.input.completed_clarification_rounds == 3:
            if self.expectation.expected_clarification is not None:
                raise ValueError("Exhausted clarification budget cannot expect another question")
            if (
                AllowedTool.ASK_CLARIFICATION in self.expectation.allowed_tools
                or AllowedTool.ASK_CLARIFICATION
                in self.expectation.expected_tool_sequence
            ):
                raise ValueError("Exhausted clarification budget cannot allow another question")
        return self
