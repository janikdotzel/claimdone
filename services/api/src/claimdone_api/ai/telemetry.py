"""Value-free provider telemetry and canonical workflow-event projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from claimdone_api.contracts import (
    OperationalFailureWorkflowEvent,
    ProviderCallWorkflowEvent,
    ProviderFailure,
    ProviderFailureCategory,
    ProviderModelId,
    ProviderUsageSnapshot,
    RetryWorkflowEvent,
    WorkflowOperation,
)
from claimdone_api.gates import OutputContractRun

from .config import ProviderMode


class ProviderCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderCallTelemetry:
    """Content-free metadata retained for success and failed-call OBS projection."""

    operation: WorkflowOperation
    model_id: ProviderModelId
    provider_mode: ProviderMode
    call_sequence: int
    retry_attempt: int
    duration_ms: int
    status: ProviderCallStatus
    usage: ProviderUsageSnapshot | None = None

    def __post_init__(self) -> None:
        if self.operation not in {
            WorkflowOperation.TRANSCRIPTION,
            WorkflowOperation.EXTRACTION,
        }:
            raise ValueError("AI telemetry supports only transcription and extraction")
        if type(self.call_sequence) is not int or not 1 <= self.call_sequence <= 40:
            raise ValueError("callSequence must be an integer from 1 through 40")
        if type(self.retry_attempt) is not int or self.retry_attempt not in {0, 1}:
            raise ValueError("retryAttempt must be zero or one")
        if self.operation is not WorkflowOperation.EXTRACTION and self.retry_attempt != 0:
            raise ValueError("Only extraction may use the app-owned retry")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ValueError("durationMs must be a non-negative integer")
        if not isinstance(self.status, ProviderCallStatus):
            raise ValueError("Provider status must be closed")
        if self.status is ProviderCallStatus.FAILED and self.usage is not None:
            raise ValueError("Failed calls cannot expose usage")
        if self.provider_mode is ProviderMode.MOCK:
            if self.model_id is not ProviderModelId.DETERMINISTIC_MOCK:
                raise ValueError("Mock telemetry requires the deterministic mock model")
        elif self.operation is WorkflowOperation.TRANSCRIPTION:
            if self.model_id is not ProviderModelId.TRANSCRIBE:
                raise ValueError("Live transcription requires gpt-4o-transcribe")
        elif self.model_id is not ProviderModelId.SOL:
            raise ValueError("Live extraction requires gpt-5.6-sol")

    def to_success_event(self) -> ProviderCallWorkflowEvent:
        """Project a successful call to the current canonical workflow contract."""

        if self.status is not ProviderCallStatus.SUCCEEDED:
            raise ValueError("Only successful telemetry has a provider-call event")
        return ProviderCallWorkflowEvent.model_validate(
            {
                "kind": "provider_call",
                "operation": self.operation.value,
                "modelId": self.model_id.value,
                "providerMode": self.provider_mode.value,
                "callSequence": self.call_sequence,
                "retryAttempt": self.retry_attempt,
                "durationMs": self.duration_ms,
                "status": "succeeded",
                "usage": self.usage,
                "cost": None,
            }
        )

    def to_failure_event(
        self,
        failure: ProviderFailure,
    ) -> OperationalFailureWorkflowEvent:
        """Project one terminal provider failure with the same call metadata."""

        if self.status is not ProviderCallStatus.FAILED:
            raise ValueError("Only failed telemetry has an operational-failure event")
        return OperationalFailureWorkflowEvent.model_validate(
            {
                "kind": "operational_failure",
                "operation": self.operation.value,
                "modelId": self.model_id.value,
                "providerMode": self.provider_mode.value,
                "callSequence": self.call_sequence,
                "retryAttempt": self.retry_attempt,
                "durationMs": self.duration_ms,
                "failure": failure,
            }
        )

    def to_retry_event(self, g2_run: OutputContractRun) -> RetryWorkflowEvent:
        """Project the one retry authorized by the deterministic first G2 result."""

        if self.operation is not WorkflowOperation.EXTRACTION:
            raise ValueError("Only extraction telemetry can project a retry event")
        if self.status is not ProviderCallStatus.SUCCEEDED:
            raise ValueError("A G2 retry follows a completed provider response")
        if self.retry_attempt != 0:
            raise ValueError("Only the initial extraction response can authorize a retry")
        if self.call_sequence >= 40:
            raise ValueError("The retry must reserve the next provider call sequence")
        if not isinstance(g2_run, OutputContractRun):
            raise ValueError("Retry authority requires the canonical G2 run")
        if not g2_run.attempts or not g2_run.attempts[0].retry_allowed:
            raise ValueError("The first deterministic G2 result did not authorize a retry")

        failure = ProviderFailure.model_validate(
            {
                "category": ProviderFailureCategory.INVALID_RESPONSE.value,
                "retryable": True,
                "terminal": False,
            }
        )
        return RetryWorkflowEvent.model_validate(
            {
                "kind": "retry",
                "operation": self.operation.value,
                "modelId": self.model_id.value,
                "providerMode": self.provider_mode.value,
                "callSequence": self.call_sequence,
                "retryAttempt": 1,
                "durationMs": self.duration_ms,
                "failure": failure,
            }
        )


def elapsed_milliseconds(start: float, end: float) -> int:
    """Convert an injected monotonic interval to a safe non-negative integer."""

    if type(start) not in {int, float} or type(end) not in {int, float}:
        return 0
    return max(0, round((float(end) - float(start)) * 1_000))


def sanitized_usage(value: object) -> ProviderUsageSnapshot | None:
    """Copy only valid integer counters; discard all other response metadata."""

    input_tokens = _member(value, "input_tokens")
    output_tokens = _member(value, "output_tokens")
    total_tokens = _member(value, "total_tokens")
    if (
        type(input_tokens) is not int
        or type(output_tokens) is not int
        or type(total_tokens) is not int
        or input_tokens < 0
        or output_tokens < 0
        or total_tokens != input_tokens + output_tokens
    ):
        return None
    return ProviderUsageSnapshot.model_validate(
        {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
        }
    )


def response_member(value: object, name: str) -> object:
    """Read a typed SDK-model or fake response member without retaining the object."""

    return _member(value, name)


def _member(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return cast(object, value.get(name))
    return cast(object, getattr(value, name, None))
