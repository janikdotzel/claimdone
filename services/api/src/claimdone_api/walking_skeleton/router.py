"""FastAPI routes for the no-live-AI walking skeleton."""

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from claimdone_api.cases import ErrorEnvelope
from claimdone_api.media import (
    AudioUpload,
    ExifDecision,
    ImageUpload,
    IntakeConsents,
    IntakeRequest,
)

from .errors import FlowError
from .models import (
    ClarificationAnswerRequest,
    DemoResetResponse,
    FlowResponse,
)
from .service import WalkingSkeletonService


def create_walking_skeleton_router(service: WalkingSkeletonService) -> APIRouter:
    router = APIRouter(tags=["walking-skeleton"])

    @router.post(
        "/api/cases/{case_id}/intake",
        response_model=FlowResponse,
        responses={
            404: {"model": ErrorEnvelope},
            409: {"model": ErrorEnvelope},
            413: {"model": ErrorEnvelope},
            422: {"model": ErrorEnvelope},
        },
    )
    async def intake(
        http_request: Request,
        case_id: str,
        expected_version: Annotated[str, Form(alias="expectedVersion")],
        sandbox_acknowledged: Annotated[str, Form(alias="sandboxAcknowledged")],
        image_rights_confirmed: Annotated[str, Form(alias="imageRightsConfirmed")],
        data_processing_approved: Annotated[str, Form(alias="dataProcessingApproved")],
        exif_decisions: Annotated[list[str], Form(alias="exifDecisions")],
        images: Annotated[list[UploadFile], File()],
        statement_text: Annotated[str | None, Form(alias="statementText")] = None,
        audio: Annotated[UploadFile | None, File()] = None,
    ) -> FlowResponse | JSONResponse:
        try:
            await _assert_closed_multipart(http_request)
            parsed_version = _positive_int(expected_version, "expectedVersion")
            service.assert_intake_precondition(case_id, parsed_version)
            decisions = _exif_decisions(exif_decisions)
            parsed_sandbox = _strict_bool(
                sandbox_acknowledged,
                "sandboxAcknowledged",
            )
            parsed_rights = _strict_bool(
                image_rights_confirmed,
                "imageRightsConfirmed",
            )
            parsed_processing = _strict_bool(
                data_processing_approved,
                "dataProcessingApproved",
            )
            image_uploads: list[ImageUpload] = []
            for image in images:
                image_uploads.append(
                    ImageUpload(
                        content=await image.read(),
                        media_type=image.content_type or "",
                    )
                )
            request = IntakeRequest(
                images=tuple(image_uploads),
                text=statement_text,
                audio=(
                    None
                    if audio is None
                    else AudioUpload(
                        content=await audio.read(),
                        media_type=audio.content_type or "",
                    )
                ),
                consents=IntakeConsents(
                    sandbox_acknowledged=parsed_sandbox,
                    image_rights_confirmed=parsed_rights,
                    data_processing_approved=parsed_processing,
                ),
            )
            return service.intake(
                case_id,
                expected_version=parsed_version,
                request=request,
                exif_decisions=decisions,
            )
        except FlowError as error:
            return flow_error_response(error)

    @router.post(
        "/api/cases/{case_id}/clarifications/{clarification_id}/answer",
        response_model=FlowResponse,
        responses={
            404: {"model": ErrorEnvelope},
            409: {"model": ErrorEnvelope},
            422: {"model": ErrorEnvelope},
            502: {"model": ErrorEnvelope},
        },
    )
    def answer(
        case_id: str,
        clarification_id: str,
        request: ClarificationAnswerRequest,
    ) -> FlowResponse | JSONResponse:
        try:
            return service.answer(
                case_id,
                clarification_id,
                expected_version=request.expected_version,
                answer=request.answer,
            )
        except FlowError as error:
            return flow_error_response(error)

    @router.post(
        "/api/dev/reset",
        response_model=DemoResetResponse,
    )
    def reset_demo() -> DemoResetResponse:
        return DemoResetResponse.model_validate({"deletedCases": service.reset_demo()})

    return router


def flow_error_response(error: FlowError) -> JSONResponse:
    decision = error.gate_decision
    field_errors: list[dict[str, object]]
    if decision is not None:
        field_errors = [
            {
                "field": _gate_reason_field(reason.value),
                "reasonCode": reason.value,
                "message": _gate_reason_message(reason.value),
            }
            for reason in decision.reason_codes
        ]
    elif error.field is not None:
        field_errors = [
            {
                "field": error.field,
                "reasonCode": None,
                "message": error.message,
            }
        ]
    else:
        field_errors = []
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "reasonCodes": (
                    [] if decision is None else [reason.value for reason in decision.reason_codes]
                ),
                "fieldErrors": field_errors,
                "gateDecision": (
                    None if decision is None else decision.model_dump(mode="json", by_alias=True)
                ),
                "currentVersion": error.current_version,
            }
        },
    )


def _positive_int(value: str, field: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise FlowError(
            "INTAKE_FORM_INVALID",
            f"{field} must be a positive integer.",
            422,
            field=field,
        )
    parsed = int(value)
    if parsed < 1:
        raise FlowError(
            "INTAKE_FORM_INVALID",
            f"{field} must be a positive integer.",
            422,
            field=field,
        )
    return parsed


def _strict_bool(value: str, field: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise FlowError(
        "INTAKE_FORM_INVALID",
        f"{field} must be exactly true or false.",
        422,
        field=field,
    )


def _exif_decisions(values: list[str]) -> tuple[ExifDecision, ...]:
    if len(values) != 3:
        raise FlowError(
            "INTAKE_FORM_INVALID",
            "exifDecisions must contain exactly three position-bound values.",
            422,
            field="exifDecisions",
        )
    try:
        return tuple(ExifDecision(value) for value in values)
    except ValueError as error:
        raise FlowError(
            "INTAKE_FORM_INVALID",
            "Every exifDecisions value must be strip or retain.",
            422,
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
    unexpected = sorted({key for key, _value in items} - allowed)
    if unexpected:
        raise FlowError(
            "INTAKE_FORM_INVALID",
            "Multipart form contains an unsupported field.",
            422,
            field=unexpected[0],
        )
    counts: dict[str, int] = {}
    for key, _value in items:
        counts[key] = counts.get(key, 0) + 1
    exact_once = {
        "dataProcessingApproved",
        "expectedVersion",
        "imageRightsConfirmed",
        "sandboxAcknowledged",
    }
    invalid_required = sorted(field for field in exact_once if counts.get(field, 0) != 1)
    if invalid_required:
        raise FlowError(
            "INTAKE_FORM_INVALID",
            "Required multipart field must occur exactly once.",
            422,
            field=invalid_required[0],
        )
    for field in ("audio", "statementText"):
        if counts.get(field, 0) > 1:
            raise FlowError(
                "INTAKE_FORM_INVALID",
                "Statement-mode multipart field must occur at most once.",
                422,
                field=field,
            )
    if (counts.get("audio", 0) == 1) == (counts.get("statementText", 0) == 1):
        raise FlowError(
            "INTAKE_FORM_INVALID",
            "Provide exactly one statementText or audio part.",
            422,
            field="statement",
        )
    for field, expected_count in (("images", 3), ("exifDecisions", 3)):
        if counts.get(field, 0) != expected_count:
            raise FlowError(
                "INTAKE_FORM_INVALID",
                f"Multipart field must occur exactly {expected_count} times.",
                422,
                field=field,
            )


def _gate_reason_field(reason: str) -> str:
    if reason.startswith(("G0_IMAGE_COUNT", "G0_IMAGE_TYPE", "G0_IMAGE_TOO")):
        return "images"
    if reason in {"G0_INPUT_MODE_INVALID", "G0_AUDIO_TOO_LONG"}:
        return "statement"
    if reason == "G0_CONSENT_MISSING":
        return "consents"
    if reason == "G1_EXIF_UNREVIEWED":
        return "privacy.exifDecisions"
    if reason == "G1_MODEL_COPY_NOT_APPROVED":
        return "consents.dataProcessingApproved"
    if reason == "G1_SENSITIVE_LOG_DATA":
        return "privacy"
    return "flow"


def _gate_reason_message(reason: str) -> str:
    messages = {
        "G0_IMAGE_COUNT_INVALID": "Exactly three images are required.",
        "G0_IMAGE_TYPE_INVALID": "Every image must be a valid JPG or PNG.",
        "G0_IMAGE_TOO_LARGE": (
            "Each image must be at most 10 MB and within safe dimensions."
        ),
        "G0_INPUT_MODE_INVALID": (
            "Provide exactly one bounded text statement or one PCM WAV statement."
        ),
        "G0_AUDIO_TOO_LONG": "The WAV statement must be at most 60 seconds.",
        "G0_CONSENT_MISSING": "All three intake consents are required.",
        "G1_EXIF_UNREVIEWED": "Choose strip or retain for each image.",
        "G1_MODEL_COPY_NOT_APPROVED": "Processing approval is required.",
        "G1_SENSITIVE_LOG_DATA": "Sensitive audit fields are forbidden.",
    }
    return messages.get(reason, "A deterministic gate blocked this field.")
