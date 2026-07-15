"""Single public human-approval route with authorization-first parsing."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from claimdone_api.contracts import SandboxReceipt

from .errors import AuthorityError, token_invalid
from .service import AuthorityService


def create_authority_router(service: AuthorityService) -> APIRouter:
    router = APIRouter(prefix="/api/sandbox/cases", tags=["sandbox-authority"])

    @router.post(
        "/{case_id}/human-approve",
        response_model=SandboxReceipt,
        responses={403: {}, 404: {}, 409: {}, 422: {}},
    )
    async def human_approve(case_id: str, request: Request) -> SandboxReceipt | JSONResponse:
        try:
            authorization = service.authorize_human_bearer(_bearer_token(request))
        except AuthorityError as error:
            return authority_error_response(error)

        if await request.body():
            return _request_error_response()
        try:
            return service.approve_authorized(
                case_id,
                authorization=authorization,
            )
        except AuthorityError as error:
            return authority_error_response(error)

    return router


def _bearer_token(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise token_invalid()
    scheme, separator, value = values[0].partition(" ")
    if scheme != "Bearer" or separator != " " or not value or " " in value:
        raise token_invalid()
    return value


def _request_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "The request does not match the closed approval contract.",
                "reasonCodes": [],
                "fieldErrors": [
                    {
                        "field": "body",
                        "reasonCode": None,
                        "message": "The approval request body must be empty.",
                    }
                ],
                "gateDecision": None,
                "currentVersion": None,
            }
        },
    )


def authority_error_response(error: AuthorityError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.safe_message,
                "reasonCodes": [reason.value for reason in error.reason_codes],
                "fieldErrors": [],
                "gateDecision": None,
                "currentVersion": error.current_version,
            }
        },
    )
