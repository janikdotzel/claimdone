"""Strict reconnectable SSE tests for the canonical workflow router."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claimdone_api.cases.errors import CaseNotFoundError
from claimdone_api.cases.workflow_events import (
    EventStreamConfig,
    WorkflowEventStreamer,
    encode_workflow_event,
)
from claimdone_api.cases.workflow_router import create_workflow_router
from claimdone_api.contracts import CONTRACT_VERSION, WorkflowEventEnvelope, WorkflowSnapshot
from claimdone_api.persistence import CaseRecord, SequencedWorkflowEvent

CASE_ID = "case-sse-001"
OCCURRED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)


def _event(cursor: int, *, case_id: str = CASE_ID) -> SequencedWorkflowEvent:
    envelope = WorkflowEventEnvelope.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "eventId": f"projection-{cursor}",
            "caseId": case_id,
            "sourceAuditEventId": f"audit-{cursor}",
            "sourceAuditEventType": "clarification",
            "sourceAuditSequence": cursor,
            "cursor": cursor,
            "occurredAt": OCCURRED_AT,
            "event": {
                "kind": "clarification",
                "round": 1,
                "field": "incident_date",
                "status": "requested",
            },
        }
    )
    return SequencedWorkflowEvent(sequence=cursor, envelope=envelope)


@dataclass
class FakeWorkflowService:
    events: tuple[SequencedWorkflowEvent, ...] = ()
    missing: bool = False
    failure: Exception | None = None
    event_calls: list[tuple[str, int, int]] = field(default_factory=list)

    def create_case(self, metadata: object | None = None) -> CaseRecord:
        del metadata
        raise AssertionError("create_case is outside this SSE test")

    def get_workflow_snapshot(
        self,
        case_id: str,
        *,
        request_id: str,
    ) -> WorkflowSnapshot:
        del case_id, request_id
        raise AssertionError("snapshot is outside this SSE test")

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        self.event_calls.append((case_id, after, limit))
        if self.missing:
            raise CaseNotFoundError(case_id)
        if self.failure is not None:
            raise self.failure
        return tuple(item for item in self.events if item.sequence > after)[:limit]

    def delete_case(self, case_id: str) -> None:
        del case_id
        raise AssertionError("delete_case is outside this SSE test")


def _client(service: FakeWorkflowService) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_workflow_router(
            service,
            event_stream_config=EventStreamConfig(one_shot=True),
        )
    )
    return TestClient(app)


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "reasonCodes": [],
            "fieldErrors": [],
            "gateDecision": None,
            "currentVersion": None,
        }
    }


def test_sse_replays_exact_envelopes_with_required_headers() -> None:
    events = (_event(2), _event(5))
    service = FakeWorkflowService(events=events)

    response = _client(service).get(f"/api/cases/{CASE_ID}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text == "".join(
        f"id: {item.sequence}\nevent: workflow\n"
        f"data: {item.envelope.model_dump_json(by_alias=True)}\n\n"
        for item in events
    )
    assert service.event_calls == [(CASE_ID, 0, 100)]


@pytest.mark.parametrize(
    ("query", "headers", "expected"),
    [
        ("?after=2", {}, (5, 9)),
        ("", {"Last-Event-ID": "5"}, (9,)),
        ("?after=5", {"Last-Event-ID": "5"}, (9,)),
    ],
)
def test_sse_reconnect_uses_query_or_last_event_id(
    query: str,
    headers: dict[str, str],
    expected: tuple[int, ...],
) -> None:
    service = FakeWorkflowService(events=(_event(2), _event(5), _event(9)))

    response = _client(service).get(
        f"/api/cases/{CASE_ID}/events{query}",
        headers=headers,
    )

    assert response.status_code == 200
    assert tuple(
        int(line.removeprefix("id: "))
        for line in response.text.splitlines()
        if line.startswith("id: ")
    ) == expected


@pytest.mark.parametrize(
    "suffix",
    (
        "?after=true",
        "?after=-1",
        "?after=+1",
        "?after=01",
        "?after=9223372036854775808",
        "?after=1&after=1",
    ),
)
def test_sse_rejects_noncanonical_duplicate_and_overflow_cursors(suffix: str) -> None:
    response = _client(FakeWorkflowService()).get(
        f"/api/cases/{CASE_ID}/events{suffix}"
    )

    assert response.status_code == 400
    assert response.json() == _error(
        "WORKFLOW_CURSOR_INVALID",
        "The workflow replay cursor is invalid.",
    )


@pytest.mark.parametrize(
    ("query", "header"),
    (("?after=2", "3"), ("", "true")),
)
def test_sse_rejects_mismatched_or_invalid_last_event_id(
    query: str,
    header: str,
) -> None:
    response = _client(FakeWorkflowService()).get(
        f"/api/cases/{CASE_ID}/events{query}",
        headers={"Last-Event-ID": header},
    )

    assert response.status_code == 400
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_sse_rejects_duplicate_last_event_id_headers() -> None:
    response = _client(FakeWorkflowService()).get(
        f"/api/cases/{CASE_ID}/events",
        headers=[("Last-Event-ID", "2"), ("Last-Event-ID", "2")],
    )

    assert response.status_code == 400
    assert response.json() == _error(
        "WORKFLOW_CURSOR_INVALID",
        "The workflow replay cursor is invalid.",
    )


@pytest.mark.parametrize("unsafe", ("duplicate", "cross_case"))
def test_sse_rejects_unsafe_persisted_page_before_stream_headers(unsafe: str) -> None:
    event = _event(2)
    events = (
        (event, event)
        if unsafe == "duplicate"
        else (_event(2, case_id="case-other"),)
    )

    response = _client(FakeWorkflowService(events=events)).get(
        f"/api/cases/{CASE_ID}/events"
    )

    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json() == _error(
        "WORKFLOW_DATA_INVALID",
        "The workflow data could not be read safely.",
    )


@pytest.mark.parametrize(
    "unsafe",
    (
        "out_of_order",
        "event_id_reused",
        "source_id_reused",
        "sequence_mismatch",
    ),
)
def test_sse_rejects_order_and_identity_corruption_before_headers(
    unsafe: str,
) -> None:
    first = _event(2)
    second = _event(5)
    events: tuple[SequencedWorkflowEvent, ...]
    if unsafe == "out_of_order":
        events = (second, first)
    elif unsafe == "event_id_reused":
        events = (
            first,
            replace(
                second,
                envelope=second.envelope.model_copy(
                    update={"event_id": first.envelope.event_id}
                ),
            ),
        )
    elif unsafe == "source_id_reused":
        events = (
            first,
            replace(
                second,
                envelope=second.envelope.model_copy(
                    update={
                        "source_audit_event_id": (
                            first.envelope.source_audit_event_id
                        )
                    }
                ),
            ),
        )
    else:
        events = (replace(first, sequence=3),)

    response = _client(FakeWorkflowService(events=events)).get(
        f"/api/cases/{CASE_ID}/events"
    )

    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json() == _error(
        "WORKFLOW_DATA_INVALID",
        "The workflow data could not be read safely.",
    )


def test_sse_missing_and_repository_failure_are_closed_envelopes() -> None:
    missing = _client(FakeWorkflowService(missing=True)).get(
        f"/api/cases/{CASE_ID}/events"
    )
    failed = _client(FakeWorkflowService(failure=RuntimeError("private data"))).get(
        f"/api/cases/{CASE_ID}/events"
    )

    assert missing.status_code == 404
    assert missing.json() == _error(
        "WORKFLOW_CASE_NOT_FOUND",
        "The workflow case does not exist.",
    )
    assert failed.status_code == 500
    assert "private data" not in failed.text


@pytest.mark.anyio
async def test_stream_stops_before_output_when_client_is_disconnected() -> None:
    service = FakeWorkflowService(events=(_event(2),))
    streamer = WorkflowEventStreamer(
        service,
        config=EventStreamConfig(one_shot=False),
    )
    replay = streamer.prepare(CASE_ID, 0)

    async def disconnected() -> bool:
        return True

    frames = [frame async for frame in streamer.stream(replay, disconnected=disconnected)]

    assert frames == []


@pytest.mark.parametrize(
    "later_failure",
    ("event_id_reused", "source_id_reused", "repository_error"),
)
@pytest.mark.anyio
async def test_stream_closes_before_emitting_a_corrupt_later_page(
    later_failure: str,
) -> None:
    first = _event(2)
    second = _event(5)

    @dataclass
    class PagedService:
        calls: list[tuple[str, int, int]] = field(default_factory=list)

        def list_workflow_events(
            self,
            case_id: str,
            *,
            after: int = 0,
            limit: int = 100,
        ) -> tuple[SequencedWorkflowEvent, ...]:
            self.calls.append((case_id, after, limit))
            if after == 0:
                return (first,)
            if later_failure == "repository_error":
                raise RuntimeError("private later failure")
            duplicate = replace(
                second,
                envelope=second.envelope.model_copy(
                    update={
                        (
                            "event_id"
                            if later_failure == "event_id_reused"
                            else "source_audit_event_id"
                        ): (
                            first.envelope.event_id
                            if later_failure == "event_id_reused"
                            else first.envelope.source_audit_event_id
                        )
                    }
                ),
            )
            return (duplicate,)

    service = PagedService()
    streamer = WorkflowEventStreamer(
        service,
        config=EventStreamConfig(page_size=1),
    )
    replay = streamer.prepare(CASE_ID, 0)

    async def connected() -> bool:
        return False

    frames = [frame async for frame in streamer.stream(replay, disconnected=connected)]

    assert frames == [encode_workflow_event(first.envelope)]
    assert service.calls == [(CASE_ID, 0, 1), (CASE_ID, 2, 1)]
