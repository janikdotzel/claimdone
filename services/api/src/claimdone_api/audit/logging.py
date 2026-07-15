"""Structured logging that never renders caller or provider content."""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from enum import StrEnum

from pydantic import JsonValue

from .redaction import (
    OBSERVABILITY_REDACTION_VERSION,
    redact_observability_payload,
)


class ObservabilityLogEvent(StrEnum):
    """Closed operational event names permitted in ClaimDone logs."""

    OBSERVABILITY_METRICS_REJECTED = "observability_metrics_rejected"
    OBSERVABILITY_PAYLOAD_REJECTED = "observability_payload_rejected"
    PROVIDER_REQUEST_FAILED = "provider_request_failed"
    PROVIDER_USAGE_REJECTED = "provider_usage_rejected"
    WORKFLOW_REPLAY_REJECTED = "workflow_replay_rejected"


type RedactedLogRecord = dict[str, JsonValue]


def emit_redacted_log(
    logger: logging.Logger,
    event: ObservabilityLogEvent,
    *,
    fields: object = None,
    error: BaseException | None = None,
    level: int = logging.WARNING,
) -> RedactedLogRecord:
    """Emit one canonical JSON record while discarding exception contents.

    ``error`` is accepted so callers do not accidentally interpolate it into a
    message, but its type, message, args, traceback and remote payload are all
    intentionally ignored.  Logging is diagnostic only: handler failures are
    suppressed and can never change a gate decision or workflow response.
    """

    del error
    selected_event = (
        event
        if isinstance(event, ObservabilityLogEvent)
        else ObservabilityLogEvent.OBSERVABILITY_PAYLOAD_REJECTED
    )
    try:
        payload = redact_observability_payload({} if fields is None else fields)
        record: RedactedLogRecord = {
            "event": selected_event.value,
            **payload,
        }
        serialized = json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except Exception:
        record = {
            "event": ObservabilityLogEvent.OBSERVABILITY_PAYLOAD_REJECTED.value,
            "schemaVersion": OBSERVABILITY_REDACTION_VERSION,
            "redacted": True,
            "redactedFieldCount": 1,
            "truncated": True,
            "fields": {},
        }
        serialized = (
            '{"event":"observability_payload_rejected","fields":{},'
            '"redacted":true,"redactedFieldCount":1,"schemaVersion":1,'
            '"truncated":true}'
        )
    selected_level = (
        level
        if type(level) is int
        and level in {logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR}
        else logging.WARNING
    )
    with suppress(Exception):
        logger.log(selected_level, serialized, stacklevel=2)
    return record
