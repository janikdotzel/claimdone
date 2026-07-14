"""Unwired HTTP router factory for canonical workflow snapshots and SSE replay."""

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from claimdone_api.cases.models import ErrorEnvelope, error_envelope
from claimdone_api.contracts import WorkflowSnapshot

from .errors import (
    WorkflowCaseNotFoundError,
    WorkflowCursorError,
    WorkflowVersionChurnError,
)
from .events import WorkflowEventStreamer, parse_replay_cursor
from .snapshots import SnapshotAssembler

DEFAULT_WORKFLOW_PREFIX = "/api/workflow/cases"


def workflow_error_response(error: Exception) -> JSONResponse:
    """Map read failures to stable envelopes without exposing exception values."""

    current_version: int | None = None
    if isinstance(error, WorkflowCaseNotFoundError):
        response_status = status.HTTP_404_NOT_FOUND
        code = "WORKFLOW_CASE_NOT_FOUND"
        message = "The workflow case does not exist."
    elif isinstance(error, WorkflowVersionChurnError):
        response_status = status.HTTP_409_CONFLICT
        code = "WORKFLOW_VERSION_CONFLICT"
        message = "The workflow case changed while it was loaded."
        current_version = error.current_version
    elif isinstance(error, WorkflowCursorError):
        response_status = status.HTTP_400_BAD_REQUEST
        code = "WORKFLOW_CURSOR_INVALID"
        message = "The workflow replay cursor is invalid."
    else:
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        code = "WORKFLOW_DATA_INVALID"
        message = "The workflow data could not be read safely."
    envelope = error_envelope(
        code=code,
        message=message,
        current_version=current_version,
    )
    return JSONResponse(
        status_code=response_status,
        content=envelope.model_dump(mode="json", by_alias=True),
    )


def resolve_replay_cursor(request: Request) -> int:
    """Resolve query/header cursors, rejecting duplicates and ambiguity."""

    query_values = request.query_params.getlist("after")
    header_values = request.headers.getlist("last-event-id")
    if len(query_values) > 1 or len(header_values) > 1:
        raise WorkflowCursorError("The workflow replay cursor is invalid.")
    try:
        query_cursor = (
            None if not query_values else parse_replay_cursor(query_values[0])
        )
        header_cursor = (
            None if not header_values else parse_replay_cursor(header_values[0])
        )
    except ValueError as error:
        raise WorkflowCursorError("The workflow replay cursor is invalid.") from error
    if (
        query_cursor is not None
        and header_cursor is not None
        and query_cursor != header_cursor
    ):
        raise WorkflowCursorError("The workflow replay cursor is invalid.")
    if query_cursor is not None:
        return query_cursor
    if header_cursor is not None:
        return header_cursor
    return 0


def create_workflow_router(
    assembler: SnapshotAssembler,
    event_streamer: WorkflowEventStreamer,
    *,
    prefix: str = DEFAULT_WORKFLOW_PREFIX,
) -> APIRouter:
    """Build an isolated router; integration will mount it at ``/api/cases``.

    The temporary default avoids colliding with the legacy CaseView route. During
    INT002 the canonical mount replaces that legacy GET surface with these paths:
    ``GET /api/cases/{case_id}`` and ``GET /api/cases/{case_id}/events``.
    """

    router = APIRouter(prefix=prefix, tags=["workflow"])

    @router.get(
        "/{case_id}",
        response_model=WorkflowSnapshot,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope},
            status.HTTP_409_CONFLICT: {"model": ErrorEnvelope},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorEnvelope},
        },
    )
    def get_workflow_snapshot(case_id: str) -> WorkflowSnapshot | JSONResponse:
        try:
            return assembler.assemble(case_id)
        except Exception as error:
            return workflow_error_response(error)

    @router.get(
        "/{case_id}/events",
        response_model=None,
        responses={
            status.HTTP_400_BAD_REQUEST: {"model": ErrorEnvelope},
            status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorEnvelope},
        },
    )
    async def get_workflow_events(
        case_id: str,
        request: Request,
    ) -> StreamingResponse | JSONResponse:
        try:
            after = resolve_replay_cursor(request)
            replay = event_streamer.prepare(case_id, after)
        except Exception as error:
            return workflow_error_response(error)
        return StreamingResponse(
            event_streamer.stream(replay, disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = [
    "DEFAULT_WORKFLOW_PREFIX",
    "create_workflow_router",
    "resolve_replay_cursor",
    "workflow_error_response",
]
