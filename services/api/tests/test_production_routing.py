"""Production route-matrix and CORS guards for the INT-002 composition root."""

from collections import Counter
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIRouter
from fastapi.testclient import TestClient

from claimdone_api.cases import CaseService
from claimdone_api.contracts import WorkflowSnapshot
from claimdone_api.main import ApiSettings, create_app
from claimdone_api.persistence import SqliteCaseRepository

WEB_ORIGIN = "http://127.0.0.1:3000"


def _production_app(tmp_path: Path) -> FastAPI:
    return create_app(
        ApiSettings(
            data_dir=tmp_path / "production-routing",
            web_origin=WEB_ORIGIN,
            portal_origin=WEB_ORIGIN,
        )
    )


def _registered_api_routes(router: FastAPI | APIRouter) -> tuple[APIRoute, ...]:
    """Flatten FastAPI's included-router wrappers without inspecting OpenAPI."""

    registered: list[APIRoute] = []
    for route in router.routes:
        if isinstance(route, APIRoute):
            registered.append(route)
            continue
        included = getattr(route, "original_router", None)
        if isinstance(included, APIRouter):
            registered.extend(_registered_api_routes(included))
    return tuple(registered)


def test_production_case_route_matrix_is_exact_and_uses_atomic_authority(
    tmp_path: Path,
) -> None:
    app = _production_app(tmp_path)
    assert type(app.state.case_service) is CaseService
    assert type(app.state.case_repository) is SqliteCaseRepository

    case_routes = tuple(
        route
        for route in _registered_api_routes(app)
        if route.path.startswith("/api/cases")
    )
    route_matrix = Counter(
        (method, route.path)
        for route in case_routes
        for method in cast(set[str], route.methods)
    )

    assert route_matrix == Counter(
        {
            ("POST", "/api/cases"): 1,
            ("GET", "/api/cases/{case_id}"): 1,
            ("DELETE", "/api/cases/{case_id}"): 1,
            ("GET", "/api/cases/{case_id}/events"): 1,
        }
    )

    response_models = {
        (method, route.path): route.response_model
        for route in case_routes
        for method in cast(set[str], route.methods)
    }
    assert response_models[("POST", "/api/cases")] is WorkflowSnapshot
    assert response_models[("GET", "/api/cases/{case_id}")] is WorkflowSnapshot

    production_paths = set(app.openapi()["paths"])
    assert not any(path.startswith("/api/workflow/cases") for path in production_paths)
    assert "/api/cases/{case_id}/events/stream" not in production_paths
    assert "/api/cases/{case_id}/events/history" not in production_paths


def test_event_stream_cors_allows_exact_local_origin_and_replay_header(
    tmp_path: Path,
) -> None:
    with TestClient(_production_app(tmp_path)) as client:
        preflight = client.options(
            "/api/cases/case-cors/events",
            headers={
                "Origin": WEB_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Content-Type, Last-Event-ID",
            },
        )
        invalid_cursor = client.get(
            "/api/cases/case-cors/events?after=01",
            headers={"Origin": WEB_ORIGIN},
        )
        invalid_request = client.post(
            "/api/cases",
            json={"unexpected": True},
            headers={"Origin": WEB_ORIGIN},
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert "access-control-allow-credentials" not in preflight.headers
    allowed_headers = {
        value.strip().lower()
        for value in preflight.headers["access-control-allow-headers"].split(",")
    }
    assert {"content-type", "last-event-id"} <= allowed_headers

    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["error"]["code"] == "WORKFLOW_CURSOR_INVALID"
    assert invalid_cursor.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert "access-control-allow-credentials" not in invalid_cursor.headers

    assert invalid_request.status_code == 422
    assert invalid_request.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert invalid_request.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert "access-control-allow-credentials" not in invalid_request.headers


def test_outer_cors_covers_unhandled_500_without_allowing_foreign_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_exception_value = "private-create-case-value"

    def fail_create_case(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(private_exception_value)

    monkeypatch.setattr(CaseService, "create_case", fail_create_case)
    with TestClient(
        _production_app(tmp_path),
        raise_server_exceptions=False,
    ) as client:
        allowed = client.post(
            "/api/cases",
            json={"metadata": {}},
            headers={"Origin": WEB_ORIGIN},
        )
        disallowed = client.post(
            "/api/cases",
            json={"metadata": {}},
            headers={"Origin": "http://127.0.0.1:3001"},
        )

    for response in (allowed, disallowed):
        assert response.status_code == 500
        assert response.text == "Internal Server Error"
        assert private_exception_value not in response.text
        assert "access-control-allow-credentials" not in response.headers
    assert allowed.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert "access-control-allow-origin" not in disallowed.headers
