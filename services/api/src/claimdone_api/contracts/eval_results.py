"""Immutable evaluation check, case, metric, and run result contracts."""

from math import isclose
from typing import Annotated, Self

from pydantic import Field, model_validator

from .base import (
    Confidence,
    ContractModel,
    ContractVersion,
    GitCommitSha,
    Identifier,
    Sha256Digest,
    StrictBoolean,
    StrictInteger,
    WireAwareDatetime,
)
from .enums import (
    EvalFailureCode,
    EvalGraderType,
    EvalMetricId,
    EvalMetricStatus,
    EvaluationMode,
    GateId,
    GateReasonCode,
)
from .workflow import ProviderFailure


class EvalCheckResult(ContractModel):
    """One closed metric check with grader failure separate from observed gates."""

    contract_version: ContractVersion
    metric_id: EvalMetricId
    grader_type: EvalGraderType
    passed: StrictBoolean
    score: Confidence | None
    failure_code: EvalFailureCode | None
    observed_gate_id: GateId | None
    observed_gate_reason_codes: tuple[GateReasonCode, ...]

    @model_validator(mode="after")
    def validate_check_result(self) -> Self:
        if self.passed is (self.failure_code is not None):
            raise ValueError("Passing checks omit failureCode; failed checks require it")
        if len(set(self.observed_gate_reason_codes)) != len(self.observed_gate_reason_codes):
            raise ValueError("Observed gate reason codes cannot contain duplicates")
        if self.observed_gate_id is None:
            if self.observed_gate_reason_codes:
                raise ValueError("Observed gate reasons require observedGateId")
        else:
            prefix = f"{self.observed_gate_id.value}_"
            if any(
                not reason.value.startswith(prefix) for reason in self.observed_gate_reason_codes
            ):
                raise ValueError("Observed gate reasons must belong to observedGateId")
        if self.grader_type is EvalGraderType.DETERMINISTIC and self.score is not None:
            raise ValueError("Deterministic checks are binary and cannot carry scores")
        if self.grader_type is not EvalGraderType.DETERMINISTIC and self.score is None:
            raise ValueError("Model and human checks require an explicit bounded score")
        return self


class EvalCaseResult(ContractModel):
    """One case result whose deterministic failures cannot be overridden."""

    contract_version: ContractVersion
    eval_id: Identifier
    evaluation_mode: EvaluationMode
    checks: Annotated[tuple[EvalCheckResult, ...], Field(min_length=1)]
    provider_failure: ProviderFailure | None
    provider_call_count: Annotated[StrictInteger, Field(ge=0)]
    deterministic_passed: StrictBoolean
    passed: StrictBoolean
    duration_ms: Annotated[StrictInteger, Field(ge=0)]

    @model_validator(mode="after")
    def derive_case_authority(self) -> Self:
        metric_ids = tuple(check.metric_id for check in self.checks)
        if len(set(metric_ids)) != len(metric_ids):
            raise ValueError("Eval case metric IDs must be unique")
        deterministic = tuple(
            check for check in self.checks if check.grader_type is EvalGraderType.DETERMINISTIC
        )
        if not deterministic:
            raise ValueError("Every eval case result requires a deterministic check")
        if self.provider_failure is not None and not self.provider_failure.terminal:
            raise ValueError("A final eval case result requires a terminal provider failure")
        if self.evaluation_mode is EvaluationMode.DETERMINISTIC:
            if len(deterministic) != len(self.checks):
                raise ValueError("Deterministic eval cases cannot contain model or human checks")
            if self.provider_call_count != 0 or self.provider_failure is not None:
                raise ValueError("Deterministic eval cases cannot call a provider")
        expected_deterministic = all(check.passed for check in deterministic)
        if self.deterministic_passed is not expected_deterministic:
            raise ValueError("deterministicPassed must be derived from deterministic checks")
        expected_passed = (
            expected_deterministic
            and self.provider_failure is None
            and all(check.passed for check in self.checks)
        )
        if self.passed is not expected_passed:
            raise ValueError("passed cannot override a check or provider failure")
        return self


class EvalMetricAggregate(ContractModel):
    """One required closed metric aggregate with explicit denominator semantics."""

    contract_version: ContractVersion
    metric_id: EvalMetricId
    status: EvalMetricStatus
    numerator: Annotated[StrictInteger, Field(ge=0)]
    denominator: Annotated[StrictInteger, Field(ge=0)]
    score: Confidence | None

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        if self.status is EvalMetricStatus.NOT_APPLICABLE:
            if self.numerator != 0 or self.denominator != 0 or self.score is not None:
                raise ValueError("not_applicable metrics require 0/0 and no score")
            return self
        if self.denominator == 0 or self.numerator > self.denominator:
            raise ValueError("Applicable metrics require 0 <= numerator <= denominator")
        expected_score = self.numerator / self.denominator
        if self.score is None or not isclose(self.score, expected_score, abs_tol=1e-12):
            raise ValueError("Metric score must equal numerator / denominator")
        if (self.status is EvalMetricStatus.PASSED) is not (self.numerator == self.denominator):
            raise ValueError("Metric status must reflect whether every observation passed")
        return self


class EvalRunSummary(ContractModel):
    """Closed aggregate bound to a dataset digest and source commit."""

    contract_version: ContractVersion
    run_id: Identifier
    dataset_version: Identifier
    dataset_sha256: Sha256Digest
    commit_sha: GitCommitSha
    evaluation_mode: EvaluationMode
    started_at: WireAwareDatetime
    completed_at: WireAwareDatetime
    case_results: Annotated[tuple[EvalCaseResult, ...], Field(min_length=1)]
    metrics: Annotated[tuple[EvalMetricAggregate, ...], Field(min_length=10, max_length=10)]
    provider_call_count: Annotated[StrictInteger, Field(ge=0)]
    total_cases: Annotated[StrictInteger, Field(ge=1)]
    passed_cases: Annotated[StrictInteger, Field(ge=0)]
    failed_cases: Annotated[StrictInteger, Field(ge=0)]
    deterministic_passed: StrictBoolean
    passed: StrictBoolean

    @model_validator(mode="after")
    def derive_run_summary(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completedAt cannot precede startedAt")
        eval_ids = tuple(result.eval_id for result in self.case_results)
        if len(set(eval_ids)) != len(eval_ids):
            raise ValueError("Eval run case IDs must be unique")
        if any(result.evaluation_mode is not self.evaluation_mode for result in self.case_results):
            raise ValueError("Case evaluationMode must match the run")
        expected_metric_order = tuple(EvalMetricId)
        if tuple(metric.metric_id for metric in self.metrics) != expected_metric_order:
            raise ValueError("Eval run must contain every metric exactly once in canonical order")
        all_checks = tuple(check for result in self.case_results for check in result.checks)
        for metric in self.metrics:
            metric_checks = tuple(
                check for check in all_checks if check.metric_id is metric.metric_id
            )
            expected_denominator = len(metric_checks)
            expected_numerator = sum(check.passed for check in metric_checks)
            if metric.denominator != expected_denominator or metric.numerator != expected_numerator:
                raise ValueError("Metric aggregates must be derived from caseResults.checks")
        provider_calls = sum(result.provider_call_count for result in self.case_results)
        if self.provider_call_count != provider_calls:
            raise ValueError("providerCallCount must be derived from caseResults")
        if self.evaluation_mode is EvaluationMode.DETERMINISTIC and provider_calls != 0:
            raise ValueError("Deterministic eval runs cannot call a provider")
        total = len(self.case_results)
        passed = sum(result.passed for result in self.case_results)
        if self.total_cases != total:
            raise ValueError("totalCases must equal the number of caseResults")
        if self.passed_cases != passed or self.failed_cases != total - passed:
            raise ValueError("Eval run pass/fail counts must be derived from caseResults")
        deterministic_passed = all(result.deterministic_passed for result in self.case_results)
        if self.deterministic_passed is not deterministic_passed:
            raise ValueError("deterministicPassed must be derived from caseResults")
        metrics_passed = all(
            metric.status is not EvalMetricStatus.FAILED for metric in self.metrics
        )
        expected_passed = deterministic_passed and passed == total and metrics_passed
        if self.passed is not expected_passed:
            raise ValueError("passed cannot override a failed case, metric, or deterministic check")
        return self
