"""ClaimDone API composition root."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from claimdone_api.cases import CaseService, create_case_router
from claimdone_api.media import (
    CaseMediaStore,
    PersistentCaseMediaCleaner,
)
from claimdone_api.persistence import SqliteCaseRepository
from claimdone_api.walking_skeleton import (
    HttpPortalPort,
    PortalPort,
    WalkingSkeletonService,
    create_walking_skeleton_router,
)
from claimdone_api.walking_skeleton.body_limit import RequestBodyLimitMiddleware

DEFAULT_GLOBAL_BODY_LIMIT = 1024 * 1024
DEFAULT_INTAKE_BODY_LIMIT = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ApiSettings:
    data_dir: Path = Path(".local/claimdone")
    web_origin: str = "http://127.0.0.1:3000"
    portal_origin: str = "http://127.0.0.1:3000"
    global_body_limit: int = DEFAULT_GLOBAL_BODY_LIMIT
    intake_body_limit: int = DEFAULT_INTAKE_BODY_LIMIT

    def __post_init__(self) -> None:
        _validate_local_origin(self.web_origin, "web_origin")
        _validate_local_origin(self.portal_origin, "portal_origin")
        if self.global_body_limit < 1 or self.intake_body_limit < self.global_body_limit:
            raise ValueError("Body limits must be positive and intake >= global")

    @classmethod
    def from_environment(cls) -> "ApiSettings":
        return cls(
            data_dir=Path(os.environ.get("CLAIMDONE_DATA_DIR", ".local/claimdone")),
            web_origin=os.environ.get(
                "CLAIMDONE_WEB_ORIGIN",
                "http://127.0.0.1:3000",
            ),
            portal_origin=os.environ.get(
                "CLAIMDONE_PORTAL_ORIGIN",
                "http://127.0.0.1:3000",
            ),
        )


class HealthResponse(BaseModel):
    service: Literal["api"] = "api"
    status: Literal["ok"] = "ok"


def create_app(
    settings: ApiSettings | None = None,
    *,
    portal_port: PortalPort | None = None,
) -> FastAPI:
    selected = settings or ApiSettings.from_environment()
    repository = SqliteCaseRepository(selected.data_dir / "cases.db")
    media_store = CaseMediaStore(selected.data_dir / "media")
    cleaner = PersistentCaseMediaCleaner(repository, media_store)
    case_service = CaseService(repository, resource_cleaner=cleaner)
    portal = portal_port or HttpPortalPort(selected.portal_origin)
    walking_service = WalkingSkeletonService(
        cases=case_service,
        repository=repository,
        media_store=media_store,
        portal=portal,
    )

    application = FastAPI(title="ClaimDone API", version="0.0.0")
    # Added before CORS so Starlette's reverse middleware build leaves the
    # narrow CORS wrapper outermost, including for early 400/413 responses.
    application.add_middleware(
        RequestBodyLimitMiddleware,
        global_limit=selected.global_body_limit,
        intake_limit=selected.intake_body_limit,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[selected.web_origin],
        allow_credentials=False,
        allow_methods=["DELETE", "GET", "OPTIONS", "POST", "PUT"],
        allow_headers=["Content-Type"],
    )
    application.include_router(create_case_router(case_service))
    application.include_router(create_walking_skeleton_router(walking_service))

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: object,
        error: RequestValidationError,
    ) -> JSONResponse:
        field_errors = []
        for issue in error.errors():
            location = issue.get("loc", ())
            field = ".".join(str(item) for item in location if item not in {"body"})
            field_errors.append(
                {
                    "field": field or "request",
                    "reasonCode": None,
                    "message": "The request field is missing or invalid.",
                }
            )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_FAILED",
                    "message": "The request does not match the closed API contract.",
                    "reasonCodes": [],
                    "fieldErrors": field_errors,
                    "gateDecision": None,
                    "currentVersion": None,
                }
            },
        )

    @application.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse()

    application.state.settings = selected
    application.state.case_repository = repository
    application.state.case_service = case_service
    application.state.media_store = media_store
    application.state.walking_skeleton_service = walking_service
    return application


def _validate_local_origin(value: str, field: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field} must be an explicit local http origin")


app = create_app()
