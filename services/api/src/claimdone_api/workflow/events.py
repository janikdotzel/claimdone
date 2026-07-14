"""Persisted-only SSE replay for redacted workflow event projections."""

import asyncio
import math
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from claimdone_api.contracts import (
    WorkflowEventEnvelope,
    validate_workflow_event_order,
)
from claimdone_api.persistence import SequencedWorkflowEvent

from .errors import WorkflowCaseNotFoundError, WorkflowDataIntegrityError
from .ports import WorkflowReadRepository

DisconnectCheck = Callable[[], Awaitable[bool]]
AsyncSleep = Callable[[float], Awaitable[None]]
MonotonicClock = Callable[[], float]

SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
_CURSOR_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")


@dataclass(frozen=True, slots=True)
class EventStreamConfig:
    """Bounded polling controls with a deterministic one-shot test mode."""

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
    """First persisted page validated before HTTP response headers are sent."""

    case_id: str
    after: int
    initial_events: tuple[SequencedWorkflowEvent, ...]


def parse_replay_cursor(value: str) -> int:
    """Parse one canonical non-negative decimal within SQLite integer bounds."""

    if type(value) is not str or _CURSOR_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid replay cursor")
    parsed = int(value)
    if parsed > SQLITE_MAX_INTEGER:
        raise ValueError("Invalid replay cursor")
    return parsed


def encode_workflow_event(envelope: WorkflowEventEnvelope) -> bytes:
    """Encode one exact canonical envelope as one SSE workflow event."""

    payload = envelope.model_dump_json(by_alias=True)
    return (
        f"id: {envelope.cursor}\n"
        "event: workflow\n"
        f"data: {payload}\n\n"
    ).encode()


class WorkflowEventStreamer:
    """Poll only persisted projections and stream database-owned cursors."""

    def __init__(
        self,
        repository: WorkflowReadRepository,
        *,
        config: EventStreamConfig | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        self._repository = repository
        self._config = config or EventStreamConfig()
        self._sleep = sleep
        self._clock = clock

    def prepare(self, case_id: str, after: int) -> PreparedReplay:
        """Validate existence and the first persisted page before streaming."""

        record = self._repository.get_case(case_id)
        if record is None:
            raise WorkflowCaseNotFoundError("The workflow case does not exist.")
        if record.case_id != case_id:
            raise WorkflowDataIntegrityError("Persisted workflow case data is invalid.")
        page = self._load_page(case_id, after)
        return PreparedReplay(case_id=case_id, after=after, initial_events=page)

    async def stream(
        self,
        replay: PreparedReplay,
        *,
        disconnected: DisconnectCheck,
    ) -> AsyncIterator[bytes]:
        """Yield replay frames, then poll persistence until disconnect."""

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
                except WorkflowDataIntegrityError:
                    # HTTP status and headers are already committed. Closing the
                    # stream is the only fail-closed response that cannot leak a
                    # corrupt projection or a repository exception value.
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
            events = self._repository.list_workflow_events(
                case_id,
                after=after,
                limit=self._config.page_size,
            )
            if len(events) > self._config.page_size:
                raise WorkflowDataIntegrityError(
                    "Persisted workflow replay data is invalid."
                )
            return _validate_replay_page(events, case_id=case_id, after=after)
        except WorkflowDataIntegrityError:
            raise
        except Exception:
            raise WorkflowDataIntegrityError(
                "Persisted workflow replay data is invalid."
            ) from None


def _validate_replay_page(
    events: Sequence[SequencedWorkflowEvent],
    *,
    case_id: str,
    after: int,
) -> tuple[SequencedWorkflowEvent, ...]:
    if len(events) > 500:
        raise WorkflowDataIntegrityError("Persisted workflow replay data is invalid.")
    materialized = tuple(events)
    envelopes: list[WorkflowEventEnvelope] = []
    event_ids: set[str] = set()
    for item in materialized:
        if type(item.sequence) is not int or not isinstance(
            item.envelope, WorkflowEventEnvelope
        ):
            raise WorkflowDataIntegrityError("Persisted workflow replay data is invalid.")
        envelope = item.envelope
        if (
            item.sequence != envelope.cursor
            or envelope.cursor <= after
            or envelope.case_id != case_id
            or envelope.event_id in event_ids
        ):
            raise WorkflowDataIntegrityError("Persisted workflow replay data is invalid.")
        event_ids.add(envelope.event_id)
        envelopes.append(envelope)
    try:
        validate_workflow_event_order(tuple(envelopes))
    except ValueError as error:
        raise WorkflowDataIntegrityError(
            "Persisted workflow replay data is invalid."
        ) from error
    return materialized


__all__ = [
    "SQLITE_MAX_INTEGER",
    "EventStreamConfig",
    "PreparedReplay",
    "WorkflowEventStreamer",
    "encode_workflow_event",
    "parse_replay_cursor",
]
