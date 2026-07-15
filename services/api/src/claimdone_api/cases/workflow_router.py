"""Closed canonical HTTP projections for case workflow state and events."""

from collections.abc import Callable, Mapping
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import APIRouter, Body, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import JsonValue

from claimdone_api.contracts import WorkflowSnapshot
from claimdone_api.persistence import CaseRecord, SequencedWorkflowEvent

from .errors import CaseNotFoundError, CaseVersionConflictError
from .models import CreateCaseRequest, ErrorEnvelope, error_envelope
from .workflow_events import (
    EventStreamConfig,
    WorkflowCursorError,
    WorkflowEventStreamer,
    parse_replay_cursor,
)


def _new_request_id() -> str:
    return f"request-{uuid4().hex}"


def _workflow_read_error_response(error: Exception) -> JSONResponse:
    if isinstance(error, CaseNotFoundError):
        response_status = status.HTTP_404_NOT_FOUND
        code = "WORKFLOW_CASE_NOT_FOUND"
        message = "The workflow case does not exist."
        current_version = None
    elif isinstance(error, CaseVersionConflictError):
        response_status = status.HTTP_409_CONFLICT
        code = "WORKFLOW_VERSION_CONFLICT"
        message = "The workflow case changed while it was loaded."
        current_version = error.current_version
    elif isinstance(error, WorkflowCursorError):
        response_status = status.HTTP_400_BAD_REQUEST
        code = "WORKFLOW_CURSOR_INVALID"
        message = "The workflow replay cursor is invalid."
        current_version = None
    else:
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        code = "WORKFLOW_DATA_INVALID"
        message = "The workflow data could not be read safely."
        current_version = None
    envelope = error_envelope(
        code=code,
        message=message,
        current_version=current_version,
    )
    return JSONResponse(
        status_code=response_status,
        content=envelope.model_dump(mode="json", by_alias=True),
    )


def _resolve_replay_cursor(request: Request) -> int:
    query_values = request.query_params.getlist("after")
    header_values = request.headers.getlist("last-event-id")
    if len(query_values) > 1 or len(header_values) > 1:
        raise WorkflowCursorError("Invalid replay cursor")
    try:
        query_cursor = (
            None if not query_values else parse_replay_cursor(query_values[0])
        )
        header_cursor = (
            None if not header_values else parse_replay_cursor(header_values[0])
        )
    except ValueError as error:
        raise WorkflowCursorError("Invalid replay cursor") from error
    if (
        query_cursor is not None
        and header_cursor is not None
        and query_cursor != header_cursor
    ):
        raise WorkflowCursorError("Invalid replay cursor")
    return query_cursor if query_cursor is not None else (header_cursor or 0)


class CanonicalWorkflowService(Protocol):
    def create_case(self, metadata: Mapping[str, JsonValue] | None = None) -> CaseRecord: ...

    def get_workflow_snapshot(
        self,
        case_id: str,
        *,
        request_id: str,
    ) -> WorkflowSnapshot: ...

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]: ...

    def delete_case(self, case_id: str) -> None: ...


def create_workflow_router(
    service: CanonicalWorkflowService,
    *,
    request_id_factory: Callable[[], str] = _new_request_id,
    event_stream_config: EventStreamConfig | None = None,
) -> APIRouter:
    """Expose only closed workflow contracts from the canonical authority."""

    router = APIRouter(prefix="/api/cases", tags=["cases"])
    event_streamer = WorkflowEventStreamer(service, config=event_stream_config)

    @router.post(
        "",
        response_model=WorkflowSnapshot,
        status_code=status.HTTP_201_CREATED,
    )
    def create_case(
        request: Annotated[CreateCaseRequest | None, Body()] = None,
    ) -> WorkflowSnapshot:
        metadata = None if request is None else request.metadata
        created = service.create_case(metadata)
        return service.get_workflow_snapshot(
            created.case_id,
            request_id=request_id_factory(),
        )

    @router.get(
        "/{case_id}",
        response_model=WorkflowSnapshot,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope},
            status.HTTP_409_CONFLICT: {"model": ErrorEnvelope},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorEnvelope},
        },
    )
    def get_case(case_id: str) -> WorkflowSnapshot | JSONResponse:
        try:
            return service.get_workflow_snapshot(
                case_id,
                request_id=request_id_factory(),
            )
        except CaseNotFoundError as error:
            return _workflow_read_error_response(error)
        except Exception as error:
            return _workflow_read_error_response(error)

    @router.delete(
        "/{case_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_case(case_id: str) -> Response:
        service.delete_case(case_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/{case_id}/events",
        response_model=None,
        responses={
            status.HTTP_200_OK: {"content": {"text/event-stream": {}}},
            status.HTTP_400_BAD_REQUEST: {"model": ErrorEnvelope},
            status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope},
            status.HTTP_409_CONFLICT: {"model": ErrorEnvelope},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorEnvelope},
        },
    )
    async def stream_events(
        case_id: str,
        request: Request,
    ) -> StreamingResponse | JSONResponse:
        try:
            after = _resolve_replay_cursor(request)
            replay = event_streamer.prepare(case_id, after)
        except Exception as error:
            return _workflow_read_error_response(error)
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
