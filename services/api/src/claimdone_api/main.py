"""ClaimDone API composition root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp

from claimdone_api.authority import AuthorityService, create_authority_router
from claimdone_api.cases import CaseService, create_case_router, create_workflow_router
from claimdone_api.cases.router import CaseCrudService
from claimdone_api.media import PersistentCaseMediaCleaner
from claimdone_api.persistence import SqliteCaseRepository
from claimdone_api.walking_skeleton.body_limit import RequestBodyLimitMiddleware

if TYPE_CHECKING:
    from claimdone_api.walking_skeleton.legacy_boundary import (
        LegacyWalkingRepository,
    )
    from claimdone_api.walking_skeleton.portal import PortalPort

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
    def from_environment(cls) -> ApiSettings:
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


class _OuterCorsFastAPI(FastAPI):
    """Keep narrow CORS outside FastAPI's server-error middleware."""

    def __init__(self, *, web_origin: str, title: str, version: str) -> None:
        _validate_local_origin(web_origin, "web_origin")
        self.__web_origin = web_origin
        super().__init__(title=title, version=version)

    def build_middleware_stack(self) -> ASGIApp:
        return CORSMiddleware(
            super().build_middleware_stack(),
            allow_origins=[self.__web_origin],
            allow_credentials=False,
            allow_methods=["DELETE", "GET", "OPTIONS", "POST", "PUT"],
            allow_headers=["Content-Type", "Last-Event-ID"],
        )


def create_app(
    settings: ApiSettings | None = None,
    *,
    portal_port: PortalPort | None = None,
    enable_legacy_walking_skeleton_for_dev: bool = False,
) -> FastAPI:
    if type(enable_legacy_walking_skeleton_for_dev) is not bool:
        raise TypeError("enable_legacy_walking_skeleton_for_dev must be a bool")
    if portal_port is not None and not enable_legacy_walking_skeleton_for_dev:
        raise ValueError(
            "portal_port is only valid with the dev-only walking skeleton enabled"
        )
    selected = settings or ApiSettings.from_environment()
    repository: SqliteCaseRepository | LegacyWalkingRepository
    case_service: CaseCrudService
    canonical_repository: SqliteCaseRepository | None = None
    authority_service: AuthorityService | None = None
    if enable_legacy_walking_skeleton_for_dev:
        from claimdone_api.walking_skeleton.legacy_boundary import (
            LegacyWalkingCaseBoundary,
            LegacyWalkingRepository,
        )
        from claimdone_api.walking_skeleton.portal import HttpPortalPort
        from claimdone_api.walking_skeleton.router import create_walking_skeleton_router
        from claimdone_api.walking_skeleton.service import WalkingSkeletonService

        legacy_repository = LegacyWalkingRepository(
            selected.data_dir / "cases.db",
            media_root=selected.data_dir / "media",
        )
        repository = legacy_repository
    else:
        canonical_repository = SqliteCaseRepository(
            selected.data_dir / "cases.db",
            media_root=selected.data_dir / "media",
        )
        repository = canonical_repository
    media_store = repository.media_store
    cleaner = PersistentCaseMediaCleaner(repository, media_store)
    if enable_legacy_walking_skeleton_for_dev:
        case_service = LegacyWalkingCaseBoundary(
            legacy_repository,
            resource_cleaner=cleaner,
        )
        portal = portal_port or HttpPortalPort(selected.portal_origin)
        walking_service = WalkingSkeletonService(
            cases=case_service,
            repository=legacy_repository,
            media_store=media_store,
            portal=portal,
        )
    else:
        assert canonical_repository is not None
        case_service = CaseService(canonical_repository, resource_cleaner=cleaner)
        authority_service = AuthorityService(canonical_repository)
        walking_service = None

    application = _OuterCorsFastAPI(
        web_origin=selected.web_origin,
        title="ClaimDone API",
        version="0.0.0",
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        global_limit=selected.global_body_limit,
        intake_limit=selected.intake_body_limit,
    )
    if walking_service is not None:
        application.include_router(create_case_router(case_service))
    else:
        assert isinstance(case_service, CaseService)
        # The older claimdone_api.workflow read-model prototype remains an
        # unwired test fixture. This is the only production case router.
        application.include_router(create_workflow_router(case_service))
        assert authority_service is not None
        application.include_router(create_authority_router(authority_service))
    if walking_service is not None:
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
    if authority_service is not None:
        application.state.authority_service = authority_service
    if walking_service is not None:
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
