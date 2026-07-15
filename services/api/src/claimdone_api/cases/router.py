"""Unwired FastAPI router factory for the Case API."""

from collections.abc import Mapping
from typing import Annotated, Protocol

from fastapi import APIRouter, Body, Response, status
from fastapi.responses import JSONResponse
from pydantic import JsonValue

from claimdone_api.persistence import CaseRecord

from .errors import CaseNotFoundError, CaseVersionConflictError
from .models import CaseView, CreateCaseRequest, ErrorEnvelope, error_envelope


class CaseCrudService(Protocol):
    def create_case(self, metadata: Mapping[str, JsonValue] | None = None) -> CaseRecord: ...

    def get_case(self, case_id: str) -> CaseRecord: ...

    def delete_case(self, case_id: str) -> None: ...


def case_error_response(error: CaseNotFoundError | CaseVersionConflictError) -> JSONResponse:
    """Map service failures to the stable top-level error envelope."""

    if isinstance(error, CaseNotFoundError):
        response_status = status.HTTP_404_NOT_FOUND
        envelope = error_envelope(code="CASE_NOT_FOUND", message="The case does not exist.")
    else:
        response_status = status.HTTP_409_CONFLICT
        envelope = error_envelope(
            code="CASE_VERSION_CONFLICT",
            message="The case changed since it was loaded.",
            current_version=error.current_version,
        )
    return JSONResponse(
        status_code=response_status,
        content=envelope.model_dump(mode="json", by_alias=True),
    )


def create_case_router(service: CaseCrudService) -> APIRouter:
    """Build a router without choosing a database path or touching the main app."""

    router = APIRouter(prefix="/api/cases", tags=["cases"])

    @router.post(
        "",
        response_model=CaseView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_case(
        request: Annotated[CreateCaseRequest | None, Body()] = None,
    ) -> CaseView:
        metadata = None if request is None else request.metadata
        return CaseView.from_record(service.create_case(metadata))

    @router.get(
        "/{case_id}",
        response_model=CaseView,
        responses={status.HTTP_404_NOT_FOUND: {"model": ErrorEnvelope}},
    )
    def get_case(case_id: str) -> CaseView | JSONResponse:
        try:
            return CaseView.from_record(service.get_case(case_id))
        except CaseNotFoundError as error:
            return case_error_response(error)

    @router.delete(
        "/{case_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_case(case_id: str) -> Response:
        service.delete_case(case_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
