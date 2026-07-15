"""OBS-001 redaction, metrics, failure-log, and persisted replay tests."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from claimdone_api.audit import (
    ObservabilityLogEvent,
    emit_redacted_log,
    redact_observability_payload,
)
from claimdone_api.cases.workflow_events import (
    EventStreamConfig,
    WorkflowDataIntegrityError,
    WorkflowEventStreamer,
    encode_workflow_event,
)
from claimdone_api.contracts import (
    ActorType,
    OperationalFailureWorkflowEvent,
    ProviderCallWorkflowEvent,
    ProviderModelId,
    RetryWorkflowEvent,
    ToolCallWorkflowEvent,
    WorkflowOperation,
)
from claimdone_api.persistence import (
    PersistedDataIntegrityError,
    SqliteCaseRepository,
)
from claimdone_api.persistence import sqlite as persistence_sqlite

NOW = datetime(2026, 7, 15, 10, tzinfo=UTC)
CASE_ID = "case-observability-001"
CASE_ID_HASH = (
    "sha256:e4291b81e168968794e0bf1d0e60f9c88e7c8e92992fd647a6591ffa50c68443"
)


def _provider_event(
    *,
    call_sequence: int,
    retry_attempt: int,
    duration_ms: int,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_micros: int,
    operation: WorkflowOperation = WorkflowOperation.EXTRACTION,
) -> ProviderCallWorkflowEvent:
    model_id = (
        ProviderModelId.TRANSCRIBE
        if operation is WorkflowOperation.TRANSCRIPTION
        else ProviderModelId.SOL
    )
    return ProviderCallWorkflowEvent.model_validate(
        {
            "kind": "provider_call",
            "operation": operation,
            "modelId": model_id,
            "providerMode": "live",
            "callSequence": call_sequence,
            "retryAttempt": retry_attempt,
            "durationMs": duration_ms,
            "status": "succeeded",
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": input_tokens + output_tokens,
            },
            "cost": {
                "estimatedCostMicros": estimated_cost_micros,
                "currency": "USD",
                "pricingSnapshotId": "pricing-obs-v1",
            },
        }
    )


def _retry_event(
    *,
    call_sequence: int = 1,
    duration_ms: int = 250,
) -> RetryWorkflowEvent:
    return RetryWorkflowEvent.model_validate(
        {
            "kind": "retry",
            "operation": "extraction",
            "modelId": "gpt-5.6-sol",
            "providerMode": "live",
            "callSequence": call_sequence,
            "retryAttempt": 1,
            "durationMs": duration_ms,
            "failure": {
                "category": "invalid_response",
                "retryable": True,
                "terminal": False,
            },
        }
    )


def _failure_event(
    *,
    operation: WorkflowOperation,
    call_sequence: int,
    retry_attempt: int = 0,
    duration_ms: int = 25,
) -> OperationalFailureWorkflowEvent:
    model_id = (
        ProviderModelId.TRANSCRIBE
        if operation is WorkflowOperation.TRANSCRIPTION
        else ProviderModelId.SOL
    )
    return OperationalFailureWorkflowEvent.model_validate(
        {
            "kind": "operational_failure",
            "operation": operation,
            "modelId": model_id,
            "providerMode": "live",
            "callSequence": call_sequence,
            "retryAttempt": retry_attempt,
            "durationMs": duration_ms,
            "failure": {
                "category": "provider_unavailable",
                "retryable": False,
                "terminal": True,
            },
        }
    )


def _tool_event() -> ToolCallWorkflowEvent:
    return ToolCallWorkflowEvent.model_validate(
        {
            "kind": "tool_call",
            "invocationId": "invocation-observability-001",
            "sequence": 1,
            "tool": "fill_until_review",
            "status": "succeeded",
            "durationMs": 90,
        }
    )


type TestProviderEvent = (
    OperationalFailureWorkflowEvent
    | ProviderCallWorkflowEvent
    | RetryWorkflowEvent
)


def _repository_with_provider_events(
    tmp_path: Path,
    events: tuple[TestProviderEvent, ...],
) -> SqliteCaseRepository:
    repository = SqliteCaseRepository(tmp_path / "provider-chronology.db")
    repository.create_case(
        case_id=CASE_ID,
        redacted_metadata={},
        created_at=NOW,
    )
    for offset, event in enumerate(events, start=1):
        actor = (
            ActorType.SYSTEM
            if isinstance(event, OperationalFailureWorkflowEvent)
            else ActorType.AGENT
        )
        with repository._write_connection() as connection:
            repository._insert_redacted_workflow_event(
                connection,
                case_id=CASE_ID,
                event=event,
                actor=actor,
                occurred_at=NOW + timedelta(milliseconds=offset),
            )
    return repository


def _repository_with_metrics(
    tmp_path: Path,
) -> tuple[SqliteCaseRepository, tuple[int, ...]]:
    repository = SqliteCaseRepository(tmp_path / "observability.db")
    repository.create_case(
        case_id=CASE_ID,
        redacted_metadata={},
        created_at=NOW,
    )
    events: tuple[
        ProviderCallWorkflowEvent | RetryWorkflowEvent | ToolCallWorkflowEvent,
        ...,
    ] = (
        _provider_event(
            call_sequence=1,
            retry_attempt=0,
            duration_ms=250,
            input_tokens=10,
            output_tokens=5,
            estimated_cost_micros=42,
        ),
        _retry_event(),
        _provider_event(
            call_sequence=2,
            retry_attempt=1,
            duration_ms=300,
            input_tokens=20,
            output_tokens=7,
            estimated_cost_micros=50,
        ),
        _tool_event(),
    )
    cursors: list[int] = []
    for offset, event in enumerate(events, start=1):
        with repository._write_connection() as connection:
            envelope = repository._insert_redacted_workflow_event(
                connection,
                case_id=CASE_ID,
                event=event,
                actor=ActorType.AGENT,
                occurred_at=NOW + timedelta(milliseconds=offset),
            )
        cursors.append(envelope.cursor)
    return repository, tuple(cursors)


def test_observability_redaction_snapshot_keeps_only_closed_operational_values() -> None:
    data_url = "data:image/png;base64,c3ludGhldGljLWJpbmFyeQ=="
    secret_name = "Synthetic Full Name"

    actual = redact_observability_payload(
        {
            "caseId": CASE_ID,
            "modelId": "gpt-5.6-sol",
            "durationMs": 250,
            "retryAttempt": 0,
            "usage": {
                "inputTokens": 10,
                "outputTokens": 5,
                "totalTokens": 15,
                "prompt": secret_name,
            },
            "claimantName": secret_name,
            "image": data_url,
            "audio": b"synthetic-binary-audio",
        }
    )

    assert actual == {
        "schemaVersion": 1,
        "redacted": True,
        "redactedFieldCount": 4,
        "truncated": False,
        "fields": {
            "caseIdHash": CASE_ID_HASH,
            "modelId": "gpt-5.6-sol",
            "durationMs": 250,
            "retryAttempt": 0,
            "usage": {
                "inputTokens": 10,
                "outputTokens": 5,
                "totalTokens": 15,
            },
        },
    }
    serialized = json.dumps(actual, sort_keys=True)
    assert secret_name not in serialized
    assert data_url not in serialized
    assert "synthetic-binary-audio" not in serialized


@pytest.mark.parametrize(
    "sensitive_key",
    (
        "audio",
        "authorization",
        "binary",
        "claimantName",
        "cookie",
        "error",
        "fullName",
        "headers",
        "image",
        "insuranceNumber",
        "message",
        "policyReference",
        "prompt",
        "query",
        "response",
        "stack",
        "url",
        "vehicleRegistration",
    ),
)
def test_sensitive_keys_are_never_copied(sensitive_key: str) -> None:
    secret = "DEMO-SENSITIVE-VALUE-9f3d"
    redacted = redact_observability_payload(
        {"durationMs": 4, sensitive_key: secret}
    )
    serialized = json.dumps(redacted, sort_keys=True)

    assert redacted["fields"] == {"durationMs": 4}
    assert redacted["redactedFieldCount"] == 1
    assert sensitive_key not in serialized
    assert secret not in serialized


@pytest.mark.parametrize(
    ("field", "unsafe"),
    (
        ("modelId", "Synthetic Full Name"),
        ("operation", "data:image/png;base64,c2VjcmV0"),
        ("providerMode", "Bearer sk-proj-test-only"),
        ("route", "https://outside.invalid/path?policy=DEMO-1"),
        ("status", "DEMO-REGISTRATION-SECRET"),
        ("tool", "free-form-provider-output"),
    ),
)
def test_free_text_cannot_enter_closed_string_fields(field: str, unsafe: str) -> None:
    redacted = redact_observability_payload({field: unsafe})

    assert redacted["fields"] == {}
    assert unsafe not in json.dumps(redacted, sort_keys=True)


def test_identifiers_are_fully_hashed_and_never_render_names_or_policy_values() -> None:
    raw_values = {
        "caseId": "Synthetic Full Name",
        "requestId": "DEMO-POLICY-SECRET-001",
        "invocationId": "DEMO-REGISTRATION-SECRET",
        "pricingSnapshotId": "insurance-secret-value",
    }

    redacted = redact_observability_payload(raw_values)
    serialized = json.dumps(redacted, sort_keys=True)
    fields = cast(dict[str, JsonValue], redacted["fields"])

    assert set(fields) == {
        "caseIdHash",
        "invocationIdHash",
        "pricingSnapshotIdHash",
        "requestIdHash",
    }
    assert all(raw not in serialized for raw in raw_values.values())
    assert all(
        len(value) == len("sha256:") + 64
        for value in fields.values()
        if isinstance(value, str)
    )


@pytest.mark.parametrize(
    "unsafe_identifier",
    (
        "data:image/png;base64,c2Vuc2l0aXZl",
        "https://outside.invalid/path?token=test-only-sensitive",
        "Bearer test-only-sensitive",
        "sk-proj-test-only-sensitive",
    ),
)
def test_data_urls_network_urls_and_credentials_are_not_hashed(
    unsafe_identifier: str,
) -> None:
    redacted = redact_observability_payload({"caseId": unsafe_identifier})

    assert redacted["fields"] == {}
    assert unsafe_identifier not in json.dumps(redacted, sort_keys=True)


def test_hostile_unicode_cycles_strange_mappings_and_oversize_fail_closed() -> None:
    class StrangeMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            del key
            raise AssertionError("a strange mapping must not be traversed")

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("a strange mapping must not be traversed")

        def __len__(self) -> int:
            raise AssertionError("a strange mapping must not be traversed")

    class HostileValue:
        def __str__(self) -> str:
            raise AssertionError("str must never be called")

        def __repr__(self) -> str:
            raise AssertionError("repr must never be called")

    cyclic: dict[str, object] = {}
    cyclic["usage"] = cyclic
    huge = "X" * 5_000_000
    cases: tuple[object, ...] = (
        StrangeMapping(),
        {"caseId": "\ud800"},
        {"\ud800": "test-only-sensitive"},
        {"caseId": "😀" * 2_000},
        {"prompt": huge},
        {"binary": memoryview(b"test-only-sensitive")},
        {"error": HostileValue()},
        cyclic,
        {f"field-{index}": index for index in range(65)},
    )

    for payload in cases:
        redacted = redact_observability_payload(payload)
        serialized = json.dumps(redacted, sort_keys=True)
        assert "test-only-sensitive" not in serialized
        assert "ud800" not in serialized
        assert redacted["redacted"] is True
    assert redact_observability_payload({"prompt": huge})["truncated"] is True
    assert redact_observability_payload({"binary": b"X" * 5_000_000})[
        "truncated"
    ] is True
    assert redact_observability_payload(cyclic)["truncated"] is True


def test_error_logging_ignores_exception_text_args_objects_and_free_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "remote-provider-secret-DEMO-001"

    class HostileError(RuntimeError):
        def __str__(self) -> str:
            raise AssertionError("exception str must not be called")

        def __repr__(self) -> str:
            raise AssertionError("exception repr must not be called")

    logger = logging.getLogger("claimdone.observability.test")
    caplog.set_level(logging.WARNING, logger=logger.name)
    record = emit_redacted_log(
        logger,
        ObservabilityLogEvent.PROVIDER_REQUEST_FAILED,
        fields={
            "caseId": CASE_ID,
            "modelId": "gpt-5.6-sol",
            "durationMs": 21,
            "headers": {"Authorization": secret},
            "url": f"https://outside.invalid/?token={secret}",
            "response": secret,
        },
        error=HostileError(secret),
    )

    assert record["event"] == "provider_request_failed"
    assert record["fields"] == {
        "caseIdHash": CASE_ID_HASH,
        "modelId": "gpt-5.6-sol",
        "durationMs": 21,
    }
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None
    assert secret not in caplog.text
    assert CASE_ID not in caplog.text


def test_sse_repository_failure_log_is_redacted_and_response_error_is_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "repository-remote-secret-DEMO"

    class FailingService:
        def list_workflow_events(
            self,
            case_id: str,
            *,
            after: int = 0,
            limit: int = 100,
        ) -> tuple[()]:
            del case_id, after, limit
            raise RuntimeError(secret)

    caplog.set_level(logging.WARNING, logger="claimdone.observability")
    streamer = WorkflowEventStreamer(FailingService())

    with pytest.raises(
        WorkflowDataIntegrityError,
        match="Persisted workflow replay is invalid",
    ) as raised:
        streamer.prepare(CASE_ID, 0)

    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)
    assert secret not in caplog.text
    assert CASE_ID not in caplog.text
    log_record = json.loads(caplog.records[-1].message)
    assert log_record["event"] == "workflow_replay_rejected"
    assert log_record["fields"] == {
        "caseIdHash": CASE_ID_HASH,
        "cursor": 0,
    }


def test_persisted_metrics_are_derived_once_without_retry_duration_double_count(
    tmp_path: Path,
) -> None:
    repository, cursors = _repository_with_metrics(tmp_path)
    unchanged_case = repository.get_case(CASE_ID)

    metrics = repository.get_observability_metrics(CASE_ID)

    assert metrics.case_id == CASE_ID
    assert metrics.through_cursor == cursors[-1]
    assert metrics.provider_request_count == 2
    assert metrics.provider_request_duration_ms == 550
    assert metrics.retry_count == 1
    assert metrics.model_ids == (ProviderModelId.SOL,)
    assert metrics.usage_reported_request_count == 2
    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (
        30,
        12,
        42,
    )
    assert metrics.costed_request_count == 2
    assert metrics.estimated_cost_micros == 92
    assert metrics.currency == "USD"
    assert metrics.pricing_snapshot_ids == ("pricing-obs-v1",)
    assert metrics.tool_call_count == 1
    assert metrics.tool_duration_ms == 90
    assert repository.get_case(CASE_ID) == unchanged_case
    assert repository.list_gate_decisions(CASE_ID) == ()
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_usage_ledger WHERE case_id = ?",
            (CASE_ID,),
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_events WHERE case_id = ?",
            (CASE_ID,),
        ).fetchone() == (4,)


@pytest.mark.parametrize("mutated_field", ("event_id", "occurred_at"))
def test_metrics_reject_coherently_mutated_workflow_source_identity(
    tmp_path: Path,
    mutated_field: str,
) -> None:
    repository, cursors = _repository_with_metrics(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        if mutated_field == "event_id":
            replacement = f"event_{'a' * 32}"
            connection.execute(
                """
                UPDATE audit_events
                SET event_id = ?, event_json = json_set(event_json, '$.eventId', ?)
                WHERE sequence = ?
                """,
                (replacement, replacement, cursors[0]),
            )
        else:
            replacement = (NOW + timedelta(days=1)).isoformat()
            connection.execute(
                """
                UPDATE audit_events
                SET occurred_at = ?,
                    event_json = json_set(event_json, '$.occurredAt', ?)
                WHERE sequence = ?
                """,
                (replacement, replacement, cursors[0]),
            )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_metrics_reject_missing_non_provider_workflow_projection(
    tmp_path: Path,
) -> None:
    repository, cursors = _repository_with_metrics(tmp_path)
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "DELETE FROM workflow_events WHERE source_audit_sequence = ?",
            (cursors[-1],),
        )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_metrics_authority_validation_is_scoped_to_the_selected_case(
    tmp_path: Path,
) -> None:
    repository, cursors = _repository_with_metrics(tmp_path)
    other_case_id = "case-observability-corrupt-002"
    repository.create_case(
        case_id=other_case_id,
        redacted_metadata={},
        created_at=NOW,
    )
    with repository._write_connection() as connection:
        other_envelope = repository._insert_redacted_workflow_event(
            connection,
            case_id=other_case_id,
            event=_tool_event(),
            actor=ActorType.AGENT,
            occurred_at=NOW + timedelta(seconds=1),
        )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "DELETE FROM workflow_events WHERE source_audit_sequence = ?",
            (other_envelope.source_audit_sequence,),
        )

    metrics = repository.get_observability_metrics(CASE_ID)

    assert metrics.through_cursor == cursors[-1]
    assert metrics.tool_call_count == 1


def test_metrics_read_uses_one_wal_snapshot_then_rejects_new_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, cursors = _repository_with_metrics(tmp_path)
    original_validate = SqliteCaseRepository._validate_case_workflow_source_bindings
    did_mutate = False

    def validate_then_mutate(
        selected_repository: SqliteCaseRepository,
        connection: sqlite3.Connection,
        *,
        case_id: str,
    ) -> None:
        nonlocal did_mutate
        original_validate(selected_repository, connection, case_id=case_id)
        if did_mutate:
            return
        did_mutate = True
        with sqlite3.connect(selected_repository.database_path) as writer:
            writer.execute(
                "DELETE FROM workflow_events WHERE source_audit_sequence = ?",
                (cursors[-1],),
            )

    monkeypatch.setattr(
        SqliteCaseRepository,
        "_validate_case_workflow_source_bindings",
        validate_then_mutate,
    )

    first = repository.get_observability_metrics(CASE_ID)

    assert first.through_cursor == cursors[-1]
    assert first.tool_call_count == 1
    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_provider_metrics_reject_retry_after_terminal_attempt_zero_failure(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _failure_event(
                operation=WorkflowOperation.EXTRACTION,
                call_sequence=1,
            ),
            _retry_event(),
        ),
    )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_provider_metrics_reject_attempt_one_before_its_retry_marker(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _provider_event(
                call_sequence=1,
                retry_attempt=0,
                duration_ms=250,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _provider_event(
                call_sequence=2,
                retry_attempt=1,
                duration_ms=300,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _retry_event(),
        ),
    )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_provider_metrics_reject_a_second_extraction_retry_chain(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _provider_event(
                call_sequence=1,
                retry_attempt=0,
                duration_ms=250,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _retry_event(),
            _provider_event(
                call_sequence=2,
                retry_attempt=1,
                duration_ms=300,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _retry_event(call_sequence=2, duration_ms=300),
            _provider_event(
                call_sequence=3,
                retry_attempt=1,
                duration_ms=350,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
        ),
    )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_provider_metrics_reject_an_operation_inside_the_extraction_retry(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _provider_event(
                call_sequence=1,
                retry_attempt=0,
                duration_ms=250,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _retry_event(),
            _provider_event(
                operation=WorkflowOperation.COMPUTER_USE,
                call_sequence=1,
                retry_attempt=0,
                duration_ms=20,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _provider_event(
                call_sequence=2,
                retry_attempt=1,
                duration_ms=300,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
        ),
    )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


@pytest.mark.parametrize("call_sequences", ((1, 3), (2, 1)))
def test_provider_metrics_reject_multi_turn_gaps_and_reversals(
    tmp_path: Path,
    call_sequences: tuple[int, int],
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        tuple(
            _provider_event(
                operation=WorkflowOperation.COMPUTER_USE,
                call_sequence=call_sequence,
                retry_attempt=0,
                duration_ms=20,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            )
            for call_sequence in call_sequences
        ),
    )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_provider_metrics_reject_any_call_after_a_terminal_failure(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _provider_event(
                operation=WorkflowOperation.COMPUTER_USE,
                call_sequence=1,
                retry_attempt=0,
                duration_ms=20,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _failure_event(
                operation=WorkflowOperation.COMPUTER_USE,
                call_sequence=2,
            ),
            _provider_event(
                operation=WorkflowOperation.VERIFICATION,
                call_sequence=1,
                retry_attempt=0,
                duration_ms=30,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
        ),
    )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_provider_metrics_reject_more_than_one_transcription_call(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        tuple(
            _provider_event(
                operation=WorkflowOperation.TRANSCRIPTION,
                call_sequence=call_sequence,
                retry_attempt=0,
                duration_ms=20,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            )
            for call_sequence in (1, 2)
        ),
    )

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ):
        repository.get_observability_metrics(CASE_ID)


def test_provider_metrics_allow_exactly_one_transcription_call(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _provider_event(
                operation=WorkflowOperation.TRANSCRIPTION,
                call_sequence=1,
                retry_attempt=0,
                duration_ms=20,
                input_tokens=2,
                output_tokens=3,
                estimated_cost_micros=4,
            ),
        ),
    )

    metrics = repository.get_observability_metrics(CASE_ID)

    assert metrics.provider_request_count == 1
    assert metrics.provider_request_duration_ms == 20
    assert metrics.retry_count == 0
    assert metrics.model_ids == (ProviderModelId.TRANSCRIBE,)
    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (
        2,
        3,
        5,
    )


@pytest.mark.parametrize("source_cursors", ((7, 7), (8, 7)))
def test_provider_chronology_requires_strict_source_cursor_order_independently(
    tmp_path: Path,
    source_cursors: tuple[int, int],
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        tuple(
            _provider_event(
                operation=WorkflowOperation.COMPUTER_USE,
                call_sequence=call_sequence,
                retry_attempt=0,
                duration_ms=20,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            )
            for call_sequence in (1, 2)
        ),
    )
    persisted = repository.list_provider_usage(CASE_ID)
    mutated = tuple(
        replace(record, source_audit_sequence=source_cursor)
        for record, source_cursor in zip(
            persisted,
            source_cursors,
            strict=True,
        )
    )
    assert tuple(record.call_sequence for record in mutated) == (1, 2)

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted provider cursors are not strictly increasing",
    ):
        persistence_sqlite._validate_provider_metric_sequence(mutated)


def test_provider_metrics_allow_current_terminal_extraction_retry_shape(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _provider_event(
                call_sequence=1,
                retry_attempt=0,
                duration_ms=250,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _retry_event(),
            _failure_event(
                operation=WorkflowOperation.EXTRACTION,
                call_sequence=2,
                retry_attempt=1,
                duration_ms=300,
            ),
        ),
    )

    metrics = repository.get_observability_metrics(CASE_ID)

    assert metrics.provider_request_count == 2
    assert metrics.provider_request_duration_ms == 550
    assert metrics.retry_count == 1


def test_provider_metrics_allow_contiguous_interleaved_cu_and_verification_turns(
    tmp_path: Path,
) -> None:
    operations = (
        (WorkflowOperation.COMPUTER_USE, 1, 20),
        (WorkflowOperation.COMPUTER_USE, 2, 21),
        (WorkflowOperation.VERIFICATION, 1, 30),
        (WorkflowOperation.COMPUTER_USE, 3, 22),
        (WorkflowOperation.VERIFICATION, 2, 31),
    )
    repository = _repository_with_provider_events(
        tmp_path,
        tuple(
            _provider_event(
                operation=operation,
                call_sequence=call_sequence,
                retry_attempt=0,
                duration_ms=duration_ms,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            )
            for operation, call_sequence, duration_ms in operations
        ),
    )

    metrics = repository.get_observability_metrics(CASE_ID)

    assert metrics.provider_request_count == 5
    assert metrics.provider_request_duration_ms == 124
    assert metrics.retry_count == 0
    assert metrics.model_ids == (ProviderModelId.SOL,)


def test_provider_metrics_allow_terminal_failure_as_last_contiguous_turn(
    tmp_path: Path,
) -> None:
    repository = _repository_with_provider_events(
        tmp_path,
        (
            _provider_event(
                operation=WorkflowOperation.COMPUTER_USE,
                call_sequence=1,
                retry_attempt=0,
                duration_ms=20,
                input_tokens=1,
                output_tokens=1,
                estimated_cost_micros=1,
            ),
            _failure_event(
                operation=WorkflowOperation.COMPUTER_USE,
                call_sequence=2,
                duration_ms=25,
            ),
        ),
    )

    metrics = repository.get_observability_metrics(CASE_ID)

    assert metrics.provider_request_count == 2
    assert metrics.provider_request_duration_ms == 45
    assert metrics.retry_count == 0


def test_corrupt_observability_row_fails_closed_and_never_logs_its_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository, cursors = _repository_with_metrics(tmp_path)
    secret = "private-provider-response-DEMO"
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE workflow_events "
            "SET event_json = json_set(event_json, '$.event.providerMessage', ?) "
            "WHERE source_audit_sequence = ?",
            (secret, cursors[0]),
        )
    caplog.set_level(logging.WARNING, logger="claimdone.observability")

    with pytest.raises(
        PersistedDataIntegrityError,
        match="Persisted observability metrics are invalid",
    ) as raised:
        repository.get_observability_metrics(CASE_ID)

    assert raised.value.__cause__ is None
    assert secret not in str(raised.value)
    assert secret not in caplog.text
    assert CASE_ID not in caplog.text
    assert json.loads(caplog.records[-1].message)["event"] == (
        "observability_metrics_rejected"
    )


@pytest.mark.anyio
async def test_persisted_event_disconnect_then_cursor_reconnect_loses_no_event(
    tmp_path: Path,
) -> None:
    repository, _cursors = _repository_with_metrics(tmp_path)
    expected = repository.list_workflow_events(CASE_ID)
    streamer = WorkflowEventStreamer(
        repository,
        config=EventStreamConfig(page_size=100),
    )
    replay = streamer.prepare(CASE_ID, 0)
    disconnect_checks = 0

    async def disconnect_after_first_frame() -> bool:
        nonlocal disconnect_checks
        disconnect_checks += 1
        return disconnect_checks >= 3

    first_connection = [
        frame
        async for frame in streamer.stream(
            replay,
            disconnected=disconnect_after_first_frame,
        )
    ]
    assert first_connection == [encode_workflow_event(expected[0].envelope)]

    reconnect_streamer = WorkflowEventStreamer(
        repository,
        config=EventStreamConfig(page_size=100, one_shot=True),
    )
    reconnect = reconnect_streamer.prepare(CASE_ID, expected[0].sequence)

    async def connected() -> bool:
        return False

    second_connection = [
        frame
        async for frame in reconnect_streamer.stream(
            reconnect,
            disconnected=connected,
        )
    ]
    assert first_connection + second_connection == [
        encode_workflow_event(item.envelope) for item in expected
    ]
    assert tuple(item.sequence for item in expected) == tuple(
        sorted(item.sequence for item in expected)
    )
