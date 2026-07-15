"""Closed FastAPI mutation boundary for the deterministic INT-002 flow."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from claimdone_api.contracts import ClarificationAnswerRequest, WorkflowSnapshot
from claimdone_api.contracts.base import ExactIdentifier
from claimdone_api.media import (
    AudioUpload,
    ExifDecision,
    ImageUpload,
    IntakeConsents,
    IntakeRequest,
)

from .errors import (
    CaseNotFoundError,
    CaseSnapshotValidationError,
    CaseVersionConflictError,
    InvalidCaseStateTransitionError,
)
from .int002_errors import (
    Int002HttpError,
    composition_failed,
    intake_form_invalid,
    request_identity_mismatch,
    request_validation_failed,
    workflow_case_not_found,
    workflow_internal_error,
    workflow_state_conflict,
    workflow_version_conflict,
)
from .int002_models import Int002RunRequest
from .models import ErrorEnvelope

_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status_code: {"model": ErrorEnvelope} for status_code in (400, 404, 409, 413, 422, 500, 502)
}


class Int002MutationService(Protocol):
    """Only orchestration surface exposed to the INT-002 HTTP adapter."""

    def submit_intake(
        self,
        case_id: str,
        *,
        expected_version: int,
        request: IntakeRequest,
        exif_decisions: tuple[ExifDecision, ...],
    ) -> WorkflowSnapshot: ...

    def answer_clarification(
        self,
        case_id: str,
        clarification_id: str,
        request: ClarificationAnswerRequest,
    ) -> WorkflowSnapshot: ...

    def run_to_review(
        self,
        case_id: str,
        *,
        expected_version: int,
    ) -> WorkflowSnapshot: ...


class _ClosedContractRoute(APIRoute):
    """Keep every route-level failure value-free and frontend-parseable."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def closed_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return int002_error_response(request_validation_failed())
            except Int002HttpError as error:
                return int002_error_response(error)
            except CaseNotFoundError:
                return int002_error_response(workflow_case_not_found())
            except CaseVersionConflictError as error:
                return int002_error_response(
                    workflow_version_conflict(current_version=error.current_version)
                )
            except (CaseSnapshotValidationError, InvalidCaseStateTransitionError):
                return int002_error_response(workflow_state_conflict())
            except StarletteHTTPException as error:
                if 400 <= error.status_code < 500:
                    return int002_error_response(request_validation_failed())
                return int002_error_response(workflow_internal_error())
            except Exception as error:
                if _is_local_composition_failure(error):
                    return int002_error_response(composition_failed())
                return int002_error_response(workflow_internal_error())

        return closed_handler


def create_int002_router(service: Int002MutationService) -> APIRouter:
    """Expose the three canonical V1 mutations without owning workflow logic."""

    router = APIRouter(
        prefix="/api/cases",
        tags=["int002"],
        route_class=_ClosedContractRoute,
    )

    @router.post(
        "/{case_id}/intake",
        response_model=WorkflowSnapshot,
        responses=_ERROR_RESPONSES,
    )
    async def submit_intake(
        http_request: Request,
        case_id: ExactIdentifier,
        expected_version: Annotated[str, Form(alias="expectedVersion")],
        sandbox_acknowledged: Annotated[str, Form(alias="sandboxAcknowledged")],
        image_rights_confirmed: Annotated[str, Form(alias="imageRightsConfirmed")],
        data_processing_approved: Annotated[
            str,
            Form(alias="dataProcessingApproved"),
        ],
        exif_decisions: Annotated[list[str], Form(alias="exifDecisions")],
        images: Annotated[list[UploadFile], File()],
        statement_text: Annotated[str | None, Form(alias="statementText")] = None,
        audio: Annotated[UploadFile | None, File()] = None,
    ) -> WorkflowSnapshot:
        await _assert_closed_multipart(http_request)
        parsed_version = _positive_int(expected_version, field="expectedVersion")
        decisions = _parse_exif_decisions(exif_decisions)
        parsed_sandbox = _strict_bool(
            sandbox_acknowledged,
            field="sandboxAcknowledged",
        )
        parsed_rights = _strict_bool(
            image_rights_confirmed,
            field="imageRightsConfirmed",
        )
        parsed_processing = _strict_bool(
            data_processing_approved,
            field="dataProcessingApproved",
        )
        image_uploads: list[ImageUpload] = []
        for image in images:
            image_uploads.append(
                ImageUpload(
                    content=await image.read(),
                    media_type=image.content_type or "",
                )
            )
        audio_upload = (
            None
            if audio is None
            else AudioUpload(
                content=await audio.read(),
                media_type=audio.content_type or "",
            )
        )
        request = IntakeRequest(
            images=tuple(image_uploads),
            text=statement_text,
            audio=audio_upload,
            consents=IntakeConsents(
                sandbox_acknowledged=parsed_sandbox,
                image_rights_confirmed=parsed_rights,
                data_processing_approved=parsed_processing,
            ),
        )
        return service.submit_intake(
            case_id,
            expected_version=parsed_version,
            request=request,
            exif_decisions=decisions,
        )

    @router.post(
        "/{case_id}/clarifications/{clarification_id}/answer",
        response_model=WorkflowSnapshot,
        responses=_ERROR_RESPONSES,
    )
    def answer_clarification(
        case_id: ExactIdentifier,
        clarification_id: ExactIdentifier,
        request: ClarificationAnswerRequest,
    ) -> WorkflowSnapshot:
        if request.case_id != case_id or request.clarification_id != clarification_id:
            raise request_identity_mismatch()
        return service.answer_clarification(case_id, clarification_id, request)

    @router.post(
        "/{case_id}/run",
        response_model=WorkflowSnapshot,
        responses=_ERROR_RESPONSES,
    )
    def run_to_review(
        case_id: ExactIdentifier,
        request: Int002RunRequest,
    ) -> WorkflowSnapshot:
        return service.run_to_review(
            case_id,
            expected_version=request.expected_version,
        )

    return router


def int002_error_response(error: Int002HttpError) -> JSONResponse:
    decision = error.gate_decision
    reason_codes = () if decision is None else decision.reason_codes
    field_errors: tuple[dict[str, object], ...]
    if decision is not None:
        field_errors = tuple(
            {
                "field": "workflow",
                "reasonCode": reason,
                "message": "A deterministic gate blocked this request.",
            }
            for reason in reason_codes
        )
    elif error.field is not None:
        field_errors = (
            {
                "field": error.field,
                "reasonCode": None,
                "message": error.safe_message,
            },
        )
    else:
        field_errors = ()
    envelope = ErrorEnvelope.model_validate(
        {
            "error": {
                "code": error.code,
                "message": error.safe_message,
                "reasonCodes": reason_codes,
                "fieldErrors": field_errors,
                "gateDecision": decision,
                "currentVersion": error.current_version,
            }
        }
    )
    return JSONResponse(
        status_code=error.status_code,
        content=envelope.model_dump(mode="json", by_alias=True),
    )


def _positive_int(value: str, *, field: str) -> int:
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise intake_form_invalid(
            safe_message=f"{field} must be a positive SQLite integer.",
            field=field,
        )
    parsed = int(value)
    if parsed < 1 or parsed > _SQLITE_MAX_INTEGER or value != str(parsed):
        raise intake_form_invalid(
            safe_message=f"{field} must be a positive SQLite integer.",
            field=field,
        )
    return parsed


def _strict_bool(value: str, *, field: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise intake_form_invalid(
        safe_message=f"{field} must be exactly true or false.",
        field=field,
    )


def _parse_exif_decisions(values: list[str]) -> tuple[ExifDecision, ...]:
    if len(values) != 3:
        raise intake_form_invalid(
            safe_message="exifDecisions must contain exactly three position-bound values.",
            field="exifDecisions",
        )
    try:
        return tuple(ExifDecision(value) for value in values)
    except ValueError as error:
        raise intake_form_invalid(
            safe_message="Every exifDecisions value must be strip or retain.",
            field="exifDecisions",
        ) from error


async def _assert_closed_multipart(request: Request) -> None:
    form = await request.form()
    items = tuple(form.multi_items())
    allowed = {
        "audio",
        "dataProcessingApproved",
        "exifDecisions",
        "expectedVersion",
        "imageRightsConfirmed",
        "images",
        "sandboxAcknowledged",
        "statementText",
    }
    if {key for key, _value in items} - allowed:
        raise intake_form_invalid(
            safe_message="Multipart form contains an unsupported field.",
            field="request",
        )

    counts: dict[str, int] = {}
    for key, value in items:
        counts[key] = counts.get(key, 0) + 1
        expected_upload = key in {"audio", "images"}
        if expected_upload != isinstance(value, StarletteUploadFile):
            raise intake_form_invalid(
                safe_message="Multipart form contains a field with an invalid part type.",
                field="request",
            )

    required_once = {
        "dataProcessingApproved",
        "expectedVersion",
        "imageRightsConfirmed",
        "sandboxAcknowledged",
    }
    if any(counts.get(field, 0) != 1 for field in required_once):
        raise intake_form_invalid(
            safe_message="Required multipart fields must occur exactly once.",
            field="request",
        )
    if any(counts.get(field, 0) > 1 for field in ("audio", "statementText")):
        raise intake_form_invalid(
            safe_message="Statement-mode multipart fields may occur at most once.",
            field="statement",
        )
    if (counts.get("audio", 0) == 1) == (counts.get("statementText", 0) == 1):
        raise intake_form_invalid(
            safe_message="Provide exactly one statementText or audio part.",
            field="statement",
        )
    if counts.get("images", 0) != 3:
        raise intake_form_invalid(
            safe_message="The images field must occur exactly three times.",
            field="images",
        )
    if counts.get("exifDecisions", 0) != 3:
        raise intake_form_invalid(
            safe_message="The exifDecisions field must occur exactly three times.",
            field="exifDecisions",
        )


def _is_local_composition_failure(error: Exception) -> bool:
    """Import heavy browser adapters only after a request actually fails."""

    try:
        from claimdone_api.computer_use.portal import PortalGatewayError
        from claimdone_api.computer_use.ports import BrowserOperationError
    except Exception:
        return False

    return isinstance(error, BrowserOperationError | PortalGatewayError)
