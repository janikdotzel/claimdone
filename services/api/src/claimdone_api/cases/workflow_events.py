"""Strict persisted-only SSE replay for canonical case workflow events."""

import asyncio
import math
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from claimdone_api.contracts import WorkflowEventEnvelope, validate_workflow_event_order
from claimdone_api.persistence import SequencedWorkflowEvent

from .errors import CaseNotFoundError

DisconnectCheck = Callable[[], Awaitable[bool]]
AsyncSleep = Callable[[float], Awaitable[None]]
MonotonicClock = Callable[[], float]

SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
_CURSOR_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class WorkflowCursorError(ValueError):
    """The HTTP replay cursor is ambiguous or non-canonical."""


class WorkflowDataIntegrityError(RuntimeError):
    """Persisted replay data cannot be exposed safely."""


class WorkflowEventService(Protocol):
    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class EventStreamConfig:
    page_size: int = 100
    poll_interval_seconds: float = 0.25
    heartbeat_interval_seconds: float = 15.0
    one_shot: bool = False

    def __post_init__(self) -> None:
        if type(self.page_size) is not int or not 1 <= self.page_size <= 500:
            raise ValueError("page_size must be an integer between 1 and 500")
        if (
            type(self.poll_interval_seconds) not in {int, float}
            or not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds < 0
        ):
            raise ValueError("poll_interval_seconds must be non-negative")
        if (
            type(self.heartbeat_interval_seconds) not in {int, float}
            or not math.isfinite(self.heartbeat_interval_seconds)
            or self.heartbeat_interval_seconds < 0
        ):
            raise ValueError("heartbeat_interval_seconds must be non-negative")
        if type(self.one_shot) is not bool:
            raise ValueError("one_shot must be a boolean")


@dataclass(frozen=True, slots=True)
class PreparedReplay:
    case_id: str
    after: int
    initial_events: tuple[SequencedWorkflowEvent, ...]


def parse_replay_cursor(value: str) -> int:
    if type(value) is not str or _CURSOR_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid replay cursor")
    parsed = int(value)
    if parsed > SQLITE_MAX_INTEGER:
        raise ValueError("Invalid replay cursor")
    return parsed


def encode_workflow_event(envelope: WorkflowEventEnvelope) -> bytes:
    return (
        f"id: {envelope.cursor}\n"
        "event: workflow\n"
        f"data: {envelope.model_dump_json(by_alias=True)}\n\n"
    ).encode()


class WorkflowEventStreamer:
    def __init__(
        self,
        service: WorkflowEventService,
        *,
        config: EventStreamConfig | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        self._service = service
        self._config = config or EventStreamConfig()
        self._sleep = sleep
        self._clock = clock

    def prepare(self, case_id: str, after: int) -> PreparedReplay:
        return PreparedReplay(
            case_id=case_id,
            after=after,
            initial_events=self._load_page(case_id, after),
        )

    async def stream(
        self,
        replay: PreparedReplay,
        *,
        disconnected: DisconnectCheck,
    ) -> AsyncIterator[bytes]:
        cursor = replay.after
        page = replay.initial_events
        first_page = True
        last_output_at = self._clock()
        while True:
            if await disconnected():
                return
            if not first_page:
                try:
                    page = self._load_page(replay.case_id, cursor)
                except (CaseNotFoundError, WorkflowDataIntegrityError):
                    return
            first_page = False
            for item in page:
                if await disconnected():
                    return
                cursor = item.envelope.cursor
                yield encode_workflow_event(item.envelope)
                last_output_at = self._clock()
            if self._config.one_shot:
                if (
                    not page
                    and self._clock() - last_output_at
                    >= self._config.heartbeat_interval_seconds
                ):
                    yield b": heartbeat\n\n"
                return
            if len(page) >= self._config.page_size:
                continue
            now = self._clock()
            if now - last_output_at >= self._config.heartbeat_interval_seconds:
                yield b": heartbeat\n\n"
                last_output_at = now
            if await disconnected():
                return
            await self._sleep(self._config.poll_interval_seconds)

    def _load_page(
        self,
        case_id: str,
        after: int,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        try:
            events = self._service.list_workflow_events(
                case_id,
                after=after,
                limit=self._config.page_size,
            )
            if type(events) is not tuple or len(events) > self._config.page_size:
                raise WorkflowDataIntegrityError("Persisted workflow replay is invalid")
            return _validate_replay_page(events, case_id=case_id, after=after)
        except CaseNotFoundError:
            raise
        except WorkflowDataIntegrityError:
            raise
        except Exception:
            raise WorkflowDataIntegrityError(
                "Persisted workflow replay is invalid"
            ) from None


def _validate_replay_page(
    events: Sequence[SequencedWorkflowEvent],
    *,
    case_id: str,
    after: int,
) -> tuple[SequencedWorkflowEvent, ...]:
    materialized = tuple(events)
    envelopes: list[WorkflowEventEnvelope] = []
    event_ids: set[str] = set()
    for item in materialized:
        if type(item.sequence) is not int or not isinstance(
            item.envelope,
            WorkflowEventEnvelope,
        ):
            raise WorkflowDataIntegrityError("Persisted workflow replay is invalid")
        envelope = item.envelope
        if (
            item.sequence != envelope.cursor
            or envelope.cursor <= after
            or envelope.case_id != case_id
            or envelope.event_id in event_ids
        ):
            raise WorkflowDataIntegrityError("Persisted workflow replay is invalid")
        event_ids.add(envelope.event_id)
        envelopes.append(envelope)
    try:
        validate_workflow_event_order(tuple(envelopes))
    except ValueError as error:
        raise WorkflowDataIntegrityError("Persisted workflow replay is invalid") from error
    return materialized
