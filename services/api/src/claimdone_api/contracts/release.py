"""Deterministic release-decision contract."""

from types import MappingProxyType
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, model_validator

from .base import (
    AlwaysTrue,
    Confidence,
    ContractModel,
    ContractVersion,
    Identifier,
    ShortText,
    StrictBoolean,
)
from .enums import (
    CheckpointStatus,
    GateReasonCode,
    HumanCheckpointId,
    ReleaseCheckId,
)

RELEASE_CHECK_FAILURE_REASONS = MappingProxyType(
    {
        ReleaseCheckId.DETERMINISTIC_TESTS: GateReasonCode.G11_DETERMINISTIC_TESTS_FAILED,
        ReleaseCheckId.SAFETY_EVALS: GateReasonCode.G11_SAFETY_EVAL_FAILED,
        ReleaseCheckId.EVAL_THRESHOLDS: GateReasonCode.G11_THRESHOLD_FAILED,
        ReleaseCheckId.PORTAL_SUCCESS_RATE: GateReasonCode.G11_PORTAL_SUCCESS_FAILED,
        ReleaseCheckId.APPROVAL_ATTACKS: GateReasonCode.G11_APPROVAL_ATTACK_FAILED,
        ReleaseCheckId.CLEAN_CHECKOUT: GateReasonCode.G11_CLEAN_CHECKOUT_FAILED,
        ReleaseCheckId.README: GateReasonCode.G11_DOCUMENTATION_MISSING,
        ReleaseCheckId.LICENSE: GateReasonCode.G11_LICENSE_MISSING,
        ReleaseCheckId.FIXTURES: GateReasonCode.G11_FIXTURES_MISSING,
        ReleaseCheckId.TEST_REPORT: GateReasonCode.G11_TEST_REPORT_MISSING,
    }
)


class ReleaseCheck(ContractModel):
    """One authoritative deterministic G11 prerequisite."""

    check_id: ReleaseCheckId
    deterministic: AlwaysTrue
    passed: StrictBoolean
    reason_code: GateReasonCode | None

    @model_validator(mode="after")
    def validate_deterministic_result(self) -> Self:
        if self.deterministic is not True:
            raise ValueError("Release checks must be deterministic")
        if self.passed and self.reason_code is not None:
            raise ValueError("A passing release check cannot have a blocking reason")
        if not self.passed and self.reason_code is not RELEASE_CHECK_FAILURE_REASONS[self.check_id]:
            raise ValueError("A failing release check requires its check-specific reason code")
        return self


class ModelGradeResult(ContractModel):
    """Supplementary quality result with no deterministic-gate authority."""

    grader_id: Identifier
    score: Confidence
    threshold: Confidence
    passed: StrictBoolean

    @model_validator(mode="after")
    def validate_score_result(self) -> Self:
        if self.passed is not (self.score >= self.threshold):
            raise ValueError("Model grade passed must be derived from score and threshold")
        return self


class HumanCheckpoint(ContractModel):
    """Explicit human-owned release prerequisite."""

    checkpoint_id: HumanCheckpointId
    status: CheckpointStatus
    confirmed_by: ShortText | None
    confirmed_at: AwareDatetime | None

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        has_identity = self.confirmed_by is not None
        has_timestamp = self.confirmed_at is not None
        if has_identity is not has_timestamp:
            raise ValueError(
                "Human checkpoint confirmation identity and timestamp must appear together"
            )
        if self.status is CheckpointStatus.PASSED and not has_identity:
            raise ValueError("A passed human checkpoint requires confirmation")
        if self.status is not CheckpointStatus.PASSED and has_identity:
            raise ValueError("Only a passed human checkpoint may contain confirmation")
        return self


class ReleaseDecision(ContractModel):
    """Final release result derived from separated deterministic, model, and human inputs."""

    contract_version: ContractVersion
    release_id: Identifier
    commit_sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    evaluated_at: AwareDatetime
    deterministic_checks: Annotated[tuple[ReleaseCheck, ...], Field(min_length=1)]
    model_grades: Annotated[tuple[ModelGradeResult, ...], Field(min_length=1)]
    human_checkpoints: Annotated[tuple[HumanCheckpoint, ...], Field(min_length=1)]
    deterministic_passed: StrictBoolean
    model_quality_passed: StrictBoolean
    human_checkpoints_passed: StrictBoolean
    passed: StrictBoolean
    reason_codes: tuple[GateReasonCode, ...]

    @model_validator(mode="after")
    def prevent_release_override(self) -> Self:
        check_ids = [check.check_id for check in self.deterministic_checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("Release check IDs must be unique")
        if set(check_ids) != set(ReleaseCheckId):
            raise ValueError("ReleaseDecision requires every deterministic release check")
        checkpoint_ids = [checkpoint.checkpoint_id for checkpoint in self.human_checkpoints]
        if len(set(checkpoint_ids)) != len(checkpoint_ids):
            raise ValueError("Human checkpoint IDs must be unique")
        if set(checkpoint_ids) != set(HumanCheckpointId):
            raise ValueError("ReleaseDecision requires every human checkpoint")
        grader_ids = [grade.grader_id for grade in self.model_grades]
        if len(set(grader_ids)) != len(grader_ids):
            raise ValueError("Model grader IDs must be unique")

        expected_deterministic = all(check.passed for check in self.deterministic_checks)
        expected_model_quality = all(grade.passed for grade in self.model_grades)
        expected_human = all(
            checkpoint.status is CheckpointStatus.PASSED for checkpoint in self.human_checkpoints
        )
        if self.deterministic_passed is not expected_deterministic:
            raise ValueError("deterministicPassed must be derived from deterministicChecks")
        if self.model_quality_passed is not expected_model_quality:
            raise ValueError("modelQualityPassed must be derived from modelGrades")
        if self.human_checkpoints_passed is not expected_human:
            raise ValueError("humanCheckpointsPassed must be derived from humanCheckpoints")

        expected_passed = expected_deterministic and expected_model_quality and expected_human
        if self.passed is not expected_passed:
            raise ValueError(
                "Release passed cannot override deterministic, model, or human failure"
            )
        derived_reasons = [
            check.reason_code
            for check in self.deterministic_checks
            if not check.passed and check.reason_code is not None
        ]
        if not expected_model_quality:
            derived_reasons.append(GateReasonCode.G11_THRESHOLD_FAILED)
        if not expected_human:
            derived_reasons.append(GateReasonCode.G11_HUMAN_CHECKPOINT_MISSING)
        expected_reasons = tuple(dict.fromkeys(derived_reasons))
        if self.reason_codes != expected_reasons:
            raise ValueError("Release reasonCodes must exactly match all derived failures")
        return self
