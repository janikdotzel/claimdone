"""HTTP, multipart, CORS, restart, and ASGI body-limit tests for INT-001."""

from __future__ import annotations

import asyncio
import json
import wave
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.types import Message, Scope

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    PortalDraftFields,
    RenderedPortalSnapshot,
    WorkflowSnapshot,
)
from claimdone_api.main import ApiSettings, create_app
from claimdone_api.media import MAX_IMAGE_WIDTH, MAX_TEXT_BYTES
from claimdone_api.persistence import SqliteCaseRepository
from claimdone_api.walking_skeleton.body_limit import RequestBodyLimitMiddleware
from claimdone_api.walking_skeleton.errors import PortalUnavailableError
from claimdone_api.walking_skeleton.portal import HttpPortalPort

WEB_ORIGIN = "http://127.0.0.1:3000"


@dataclass
class HttpTestPortal:
    calls: int = 0
    cleanup_calls: int = 0

    def fill_to_review(
        self,
        case_id: str,
        fields: PortalDraftFields,
    ) -> tuple[str, RenderedPortalSnapshot]:
        self.calls += 1
        return (
            f"{WEB_ORIGIN}/sandbox/A/cases/{case_id}",
            RenderedPortalSnapshot.model_validate(
                {
                    "caseId": case_id,
                    "contractVersion": CONTRACT_VERSION,
                    "state": "review",
                    "fields": fields.model_dump(mode="json", by_alias=True),
                    "renderedAt": "2026-07-14T12:00:00Z",
                    "variant": "A",
                    "version": 3,
                },
                strict=False,
            ),
        )

    def cleanup_case(self, case_id: str) -> None:
        del case_id
        self.cleanup_calls += 1


def image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (3, 2), color=(20, 120, 110))
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def wav_bytes() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(1)
        audio.setframerate(8)
        audio.writeframes(b"\x80" * 8)
    return output.getvalue()


def png_with_header_dimensions(width: int, height: int) -> bytes:
    content = bytearray(image_bytes("PNG"))
    assert content[12:16] == b"IHDR"
    content[16:20] = width.to_bytes(4, "big")
    content[20:24] = height.to_bytes(4, "big")
    content[29:33] = (zlib.crc32(content[12:29]) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(content)


def multipart_parts(
    *,
    expected_version: str = "1",
    sandbox: str = "true",
    text: str = "A staged HTTP statement.",
    exif_count: int = 3,
    image_count: int = 3,
    extra: tuple[str, str] | None = None,
) -> list[tuple[str, tuple[str | None, bytes | str, str | None]]]:
    parts: list[tuple[str, tuple[str | None, bytes | str, str | None]]] = [
        ("expectedVersion", (None, expected_version, None)),
        ("sandboxAcknowledged", (None, sandbox, None)),
        ("imageRightsConfirmed", (None, "true", None)),
        ("dataProcessingApproved", (None, "true", None)),
        ("statementText", (None, text, None)),
    ]
    parts.extend(("exifDecisions", (None, "strip", None)) for _ in range(exif_count))
    images = (
        ("one.jpg", image_bytes("JPEG"), "image/jpeg"),
        ("two.png", image_bytes("PNG"), "image/png"),
        ("three.jpg", image_bytes("JPEG"), "image/jpeg"),
    )
    parts.extend(("images", image) for image in images[:image_count])
    if extra is not None:
        parts.append((extra[0], (None, extra[1], None)))
    return parts


def client_for(
    tmp_path: Path,
    *,
    portal: HttpTestPortal | None = None,
    global_limit: int = 4 * 1024,
    intake_limit: int = 1024 * 1024,
) -> tuple[TestClient, HttpTestPortal]:
    selected_portal = portal or HttpTestPortal()
    app = create_app(
        ApiSettings(
            data_dir=tmp_path / "state",
            web_origin=WEB_ORIGIN,
            portal_origin=WEB_ORIGIN,
            global_body_limit=global_limit,
            intake_body_limit=intake_limit,
        ),
        portal_port=selected_portal,
        enable_legacy_walking_skeleton_for_dev=True,
    )
    return TestClient(app), selected_portal


def create_case(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/cases", json={"metadata": {}})
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def test_invalid_portal_composition_is_rejected_before_local_state_is_created(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "must-not-exist"
    settings = ApiSettings(
        data_dir=data_dir,
        web_origin=WEB_ORIGIN,
        portal_origin=WEB_ORIGIN,
    )

    with pytest.raises(ValueError, match="dev-only"):
        create_app(settings, portal_port=HttpTestPortal())

    assert not data_dir.exists()


def test_default_app_exposes_no_legacy_mutation_or_partial_commit(tmp_path: Path) -> None:
    app = create_app(
        ApiSettings(
            data_dir=tmp_path / "default-state",
            web_origin=WEB_ORIGIN,
            portal_origin=WEB_ORIGIN,
        )
    )
    repository = cast(SqliteCaseRepository, app.state.case_repository)
    assert type(repository) is SqliteCaseRepository
    assert "walking_skeleton_service" not in app.state._state
    paths = set(app.openapi()["paths"])
    assert "/api/cases/{case_id}/intake" not in paths
    assert "/api/cases/{case_id}/clarifications/{clarification_id}/answer" not in paths
    assert "/api/cases/{case_id}/events" in paths
    assert "/api/cases/{case_id}/events/history" not in paths
    assert "/api/cases/{case_id}/events/stream" not in paths
    assert "/api/dev/reset" not in paths

    with TestClient(app) as client:
        created = create_case(client)
        snapshot = WorkflowSnapshot.model_validate(created)
        assert "redactedMetadata" not in created
        assert "intakeSummary" not in created
        assert "activeClarification" not in created
        case_id = snapshot.case.case_id
        before = repository.get_case(case_id)
        assert before is not None
        fetched = client.get(f"/api/cases/{case_id}")
        response = client.post(f"/api/cases/{case_id}/intake")
        reset = client.post("/api/dev/reset")

    assert fetched.status_code == 200
    assert WorkflowSnapshot.model_validate(fetched.json()).case.case_id == case_id
    assert "redactedMetadata" not in cast(dict[str, Any], fetched.json())
    assert response.status_code == 404
    assert reset.status_code == 404
    assert repository.get_case(case_id) == before
    assert repository.list_audit_events(case_id) == ()
    assert repository.list_gate_decisions(case_id) == ()
    assert repository.list_workflow_events(case_id) == ()


def test_canonical_event_route_is_unique_and_has_no_json_or_alias_shadow(
    tmp_path: Path,
) -> None:
    app = create_app(
        ApiSettings(
            data_dir=tmp_path / "event-limit-state",
            web_origin=WEB_ORIGIN,
            portal_origin=WEB_ORIGIN,
        )
    )
    canonical_path = "/api/cases/{case_id}/events"
    registered_routes: list[Any] = []
    for route in app.routes:
        included_router = getattr(route, "original_router", None)
        registered_routes.extend(
            getattr(included_router, "routes", (route,))
            if included_router is not None
            else (route,)
        )
    matching_routes = [
        route
        for route in registered_routes
        if getattr(route, "path", None) == canonical_path
        and "GET" in getattr(route, "methods", set())
    ]

    assert len(matching_routes) == 1
    paths = set(app.openapi()["paths"])
    assert canonical_path in paths
    assert f"{canonical_path}/history" not in paths
    assert f"{canonical_path}/stream" not in paths


def test_real_multipart_happy_path_and_answer_survive_app_restart(tmp_path: Path) -> None:
    first_client, _ = client_for(tmp_path)
    created = create_case(first_client)
    intake = first_client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=multipart_parts(expected_version=str(created["version"])),
    )
    assert intake.status_code == 200, intake.text
    intake_body = cast(dict[str, Any], intake.json())
    assert intake_body["phase"] == "awaiting_clarification"
    assert intake_body["case"]["state"] == "awaiting_clarification"

    restarted_client, restarted_portal = client_for(tmp_path)
    clarification = cast(dict[str, Any], intake_body["clarification"])
    answered = restarted_client.post(
        (
            f"/api/cases/{created['caseId']}/clarifications/"
            f"{clarification['clarificationId']}/answer"
        ),
        json={"expectedVersion": clarification["expectedVersion"], "answer": "14:30"},
    )

    assert answered.status_code == 200, answered.text
    body = cast(dict[str, Any], answered.json())
    assert body["draftRevision"] == body["case"]["version"]
    assert body["case"]["state"] == "verifying"
    assert body["case"]["portalState"] == "review"
    assert body["portal"]["verificationState"] == "pending"
    assert [decision["gateId"] for decision in body["gateHistory"]] == [
        "G0",
        "G1",
        "G2",
        "G3",
        "G4",
        "G5",
    ]
    assert restarted_portal.calls == 1


def test_audio_waits_for_confirmation_with_owned_transcript_across_restart(
    tmp_path: Path,
) -> None:
    first_client, _ = client_for(tmp_path)
    created = create_case(first_client)
    parts = [part for part in multipart_parts() if part[0] != "statementText"]
    parts.append(("audio", ("statement.wav", wav_bytes(), "audio/wav")))

    intake = first_client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=parts,
    )

    assert intake.status_code == 409, intake.text
    error = cast(dict[str, Any], intake.json()["error"])
    assert error["code"] == "TRANSCRIPT_CONFIRMATION_REQUIRED"
    assert isinstance(error["currentVersion"], int)

    restarted_client, restarted_portal = client_for(tmp_path)
    waiting = restarted_client.get(f"/api/cases/{created['caseId']}")
    assert waiting.status_code == 200
    waiting_case = cast(dict[str, Any], waiting.json())
    assert waiting_case["state"] == "awaiting_transcript_confirmation"
    assert waiting_case["claimPacket"] is None
    assert waiting_case["activeClarification"] is None
    assert restarted_portal.calls == 0
    transcripts = tuple((tmp_path / "state" / "media").rglob("transcript-*.txt"))
    assert len(transcripts) == 1
    assert transcripts[0].read_text(encoding="utf-8")


def test_non_fixture_wav_never_reaches_model_safety_before_confirmation(
    tmp_path: Path,
) -> None:
    client, portal = client_for(tmp_path)
    created = create_case(client)
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(1)
        audio.setframerate(16)
        audio.writeframes(b"\x81" * 16)
    parts = [part for part in multipart_parts() if part[0] != "statementText"]
    parts.append(("audio", ("other.wav", output.getvalue(), "audio/wav")))

    response = client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=parts,
    )

    assert response.status_code == 409, response.text
    error = cast(dict[str, Any], response.json()["error"])
    assert error["code"] == "TRANSCRIPT_CONFIRMATION_REQUIRED"
    assert error["gateDecision"] is None
    waiting = client.get(f"/api/cases/{created['caseId']}")
    assert waiting.status_code == 200
    waiting_case = cast(dict[str, Any], waiting.json())
    assert waiting_case["state"] == "awaiting_transcript_confirmation"
    assert waiting_case["claimPacket"] is None
    assert waiting_case["activeClarification"] is None
    assert portal.calls == 0
    media_root = tmp_path / "state" / "media"
    assert [path for path in media_root.iterdir() if path.name.startswith("case-")]

    retry = client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=multipart_parts(expected_version=str(waiting_case["version"])),
    )
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "INTAKE_NOT_AVAILABLE"
    assert portal.calls == 0


def test_router_stale_intake_returns_409_before_media_processing(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)
    created = create_case(client)

    response = client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=multipart_parts(expected_version="2"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CASE_VERSION_CONFLICT"
    media_root = tmp_path / "state" / "media"
    assert not [path for path in media_root.iterdir() if path.name.startswith("case-")]


def test_missing_and_extra_fields_use_stable_error_envelope(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)
    created = create_case(client)
    path = f"/api/cases/{created['caseId']}/intake"
    missing = [part for part in multipart_parts() if part[0] != "expectedVersion"]

    missing_response = client.post(path, files=missing)
    extra_response = client.post(path, files=multipart_parts(extra=("unknown", "value")))

    assert missing_response.status_code == 422
    assert missing_response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert missing_response.json()["error"]["fieldErrors"]
    assert extra_response.status_code == 422
    extra_error = extra_response.json()["error"]
    assert extra_error["code"] == "INTAKE_FORM_INVALID"
    assert extra_error["fieldErrors"][0]["field"] == "unknown"


def test_closed_multipart_enforces_exact_exif_images_xor_and_strict_bool(
    tmp_path: Path,
) -> None:
    client, _ = client_for(tmp_path)
    created = create_case(client)
    path = f"/api/cases/{created['caseId']}/intake"

    invalid_exif = client.post(path, files=multipart_parts(exif_count=2))
    invalid_images = client.post(path, files=multipart_parts(image_count=2))
    invalid_bool = client.post(path, files=multipart_parts(sandbox="TRUE"))
    both_modes = client.post(
        path,
        files=[
            *multipart_parts(),
            ("audio", ("statement.wav", b"not-used", "audio/wav")),
        ],
    )
    duplicate_expected = client.post(
        path,
        files=[
            *multipart_parts(),
            ("expectedVersion", (None, "1", None)),
        ],
    )

    assert invalid_exif.json()["error"]["fieldErrors"][0]["field"] == "exifDecisions"
    assert invalid_images.json()["error"]["fieldErrors"][0]["field"] == "images"
    assert invalid_bool.json()["error"]["fieldErrors"][0]["field"] == (
        "sandboxAcknowledged"
    )
    assert both_modes.json()["error"]["fieldErrors"][0]["field"] == "statement"
    assert duplicate_expected.json()["error"]["fieldErrors"][0]["field"] == (
        "expectedVersion"
    )
    assert all(
        response.status_code == 422
        for response in (
            invalid_exif,
            invalid_images,
            invalid_bool,
            both_modes,
            duplicate_expected,
        )
    )


def test_answer_json_is_closed_before_portal(tmp_path: Path) -> None:
    client, portal = client_for(tmp_path)
    created = create_case(client)
    intake = client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=multipart_parts(),
    ).json()
    clarification = intake["clarification"]

    response = client.post(
        (
            f"/api/cases/{created['caseId']}/clarifications/"
            f"{clarification['clarificationId']}/answer"
        ),
        json={
            "expectedVersion": clarification["expectedVersion"],
            "answer": "14:30",
            "unexpected": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert portal.calls == 0


def test_text_limit_is_a_field_near_deterministic_g0_error(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)
    created = create_case(client)

    response = client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=multipart_parts(text="ü" * (MAX_TEXT_BYTES // 2 + 1)),
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["gateDecision"]["gateId"] == "G0"
    assert error["reasonCodes"] == ["G0_INPUT_MODE_INVALID"]
    assert error["fieldErrors"][0]["field"] == "statement"


def test_safe_image_dimension_limit_has_a_field_near_size_error(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)
    created = create_case(client)
    parts = multipart_parts()
    image_index = next(index for index, part in enumerate(parts) if part[0] == "images")
    parts[image_index] = (
        "images",
        (
            "wide.png",
            png_with_header_dimensions(MAX_IMAGE_WIDTH + 1, 1),
            "image/png",
        ),
    )

    response = client.post(
        f"/api/cases/{created['caseId']}/intake",
        files=parts,
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["reasonCodes"] == ["G0_IMAGE_TOO_LARGE"]
    assert error["fieldErrors"][0] == {
        "field": "images",
        "message": "Each image must be at most 10 MB and within safe dimensions.",
        "reasonCode": "G0_IMAGE_TOO_LARGE",
    }


def test_content_length_and_streamed_body_limits_include_cors_on_413(
    tmp_path: Path,
) -> None:
    client, _ = client_for(tmp_path, global_limit=64, intake_limit=128)
    headers = {"Origin": WEB_ORIGIN, "Content-Type": "application/json"}

    declared = client.post(
        "/api/cases",
        content=b"x" * 65,
        headers=headers,
    )
    streamed = client.post(
        "/api/cases",
        content=iter((b"x" * 40, b"y" * 40)),
        headers=headers,
    )

    assert declared.status_code == 413, declared.text
    assert streamed.status_code == 413, streamed.text
    for response in (declared, streamed):
        assert response.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
        assert response.headers["access-control-allow-origin"] == WEB_ORIGIN


def test_malformed_and_duplicate_content_length_are_blocked_before_app() -> None:
    async def run(headers: list[tuple[bytes, bytes]]) -> list[Message]:
        async def inner(scope: Scope, receive: object, send: object) -> None:
            raise AssertionError(f"inner app was called: {scope!r} {receive!r} {send!r}")

        middleware = RequestBodyLimitMiddleware(
            inner,
            global_limit=10,
            intake_limit=20,
        )
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope = cast(
            Scope,
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/cases",
                "raw_path": b"/api/cases",
                "query_string": b"",
                "headers": headers,
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 8000),
            },
        )
        await middleware(scope, receive, send)
        return sent

    malformed = asyncio.run(run([(b"content-length", b"abc")]))
    duplicate = asyncio.run(
        run([(b"content-length", b"1"), (b"content-length", b"1")])
    )

    assert malformed[0]["status"] == 400
    assert duplicate[0]["status"] == 400


def test_http_portal_adapter_uses_narrow_fill_and_idempotent_delete_routes() -> None:
    fields = _adapter_fields()
    calls: list[tuple[str, str]] = []

    def portal_view(*, state: str, version: int) -> dict[str, object]:
        return {
            "caseId": "case-adapter-001",
            "contractVersion": CONTRACT_VERSION,
            "variant": "A",
            "state": state,
            "version": version,
            "fields": fields.model_dump(mode="json", by_alias=True),
            "auditCount": version,
            "updatedAt": "2026-07-14T12:00:00Z",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        index = len(calls)
        if index == 1:
            assert request.method == "POST"
            assert request.url.path == "/api/dev/reset"
            assert json.loads(request.content) == {
                "caseId": "case-adapter-001",
                "fixture": "empty",
                "variant": "A",
            }
            return httpx.Response(200, json=portal_view(state="draft", version=1))
        if index == 2:
            assert request.method == "PUT"
            assert request.url.path.endswith("/draft")
            assert request.url.query == b"variant=A"
            assert json.loads(request.content)["expectedVersion"] == 1
            return httpx.Response(200, json=portal_view(state="draft", version=2))
        if index == 3:
            assert request.method == "POST"
            assert request.url.path.endswith("/review")
            assert json.loads(request.content) == {"expectedVersion": 2}
            return httpx.Response(200, json=portal_view(state="review", version=3))
        if index == 4:
            assert request.method == "GET"
            assert request.url.path.endswith("/rendered-values")
            return httpx.Response(
                200,
                json={
                    "caseId": "case-adapter-001",
                    "contractVersion": CONTRACT_VERSION,
                    "state": "review",
                    "fields": fields.model_dump(mode="json", by_alias=True),
                    "renderedAt": "2026-07-14T12:00:01Z",
                    "variant": "A",
                    "version": 3,
                },
            )
        assert index in {5, 6}
        assert request.method == "DELETE"
        assert request.url.path == "/api/sandbox/cases/case-adapter-001"
        assert request.url.query == b"variant=A"
        return httpx.Response(204 if index == 5 else 404)

    client = httpx.Client(
        base_url=WEB_ORIGIN,
        transport=httpx.MockTransport(handler),
    )
    adapter = HttpPortalPort(WEB_ORIGIN, client=client)

    review_url, rendered = adapter.fill_to_review("case-adapter-001", fields)
    adapter.cleanup_case("case-adapter-001")
    adapter.cleanup_case("case-adapter-001")

    assert review_url == f"{WEB_ORIGIN}/sandbox/A/cases/case-adapter-001"
    assert rendered.fields == fields
    assert rendered.case_id == "case-adapter-001"
    assert rendered.variant.value == "A"
    assert rendered.version == 3
    assert len(calls) == 6


@pytest.mark.parametrize(
    ("stage", "field", "value"),
    (
        ("reset", "caseId", "case-other-001"),
        ("reset", "variant", "B"),
        ("reset", "version", 2),
        ("saved", "caseId", "case-other-001"),
        ("saved", "variant", "B"),
        ("saved", "version", 3),
        ("reviewed", "caseId", "case-other-001"),
        ("reviewed", "variant", "B"),
        ("reviewed", "version", 4),
        ("rendered", "caseId", "case-other-001"),
        ("rendered", "variant", "B"),
        ("rendered", "version", 4),
    ),
)
def test_http_portal_adapter_rejects_cross_case_variant_and_version_reads(
    stage: str,
    field: str,
    value: object,
) -> None:
    fields = _adapter_fields()
    call_count = 0

    def session_body(state: str, version: int) -> dict[str, object]:
        return {
            "auditCount": version,
            "caseId": "case-adapter-001",
            "contractVersion": CONTRACT_VERSION,
            "fields": fields.model_dump(mode="json", by_alias=True),
            "state": state,
            "updatedAt": "2026-07-14T12:00:00Z",
            "variant": "A",
            "version": version,
        }

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        response_stage = ("reset", "saved", "reviewed", "rendered")[call_count - 1]
        if response_stage == "rendered":
            body: dict[str, object] = {
                "caseId": "case-adapter-001",
                "contractVersion": CONTRACT_VERSION,
                "fields": fields.model_dump(mode="json", by_alias=True),
                "renderedAt": "2026-07-14T12:00:01Z",
                "state": "review",
                "variant": "A",
                "version": 3,
            }
        else:
            response_version = {"reset": 1, "saved": 2, "reviewed": 3}[response_stage]
            body = session_body(
                "review" if response_stage == "reviewed" else "draft",
                response_version,
            )
        if response_stage == stage:
            body[field] = value
        return httpx.Response(200, json=body)

    adapter = HttpPortalPort(
        WEB_ORIGIN,
        client=httpx.Client(
            base_url=WEB_ORIGIN,
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(PortalUnavailableError):
        adapter.fill_to_review("case-adapter-001", fields)


def _adapter_fields() -> PortalDraftFields:
    return PortalDraftFields.model_validate(
        {
            "incidentDate": "2026-07-14",
            "incidentTime": "14:30:00",
            "location": "Demo Street 1, Berlin",
            "claimantName": "Demo Claimant",
            "policyReference": "DEMO-POLICY-001",
            "vehicleRegistration": "DEMO-CD-1",
            "counterpartyKnown": "yes",
            "narrative": "A staged second vehicle contacted the rear of the demo vehicle.",
            "attachments": ("model-a.jpg", "model-b.png", "model-c.jpg"),
        }
    )


@pytest.mark.parametrize(
    "origin",
    (
        "https://127.0.0.1:3000",
        "http://example.com:3000",
        "http://user:password@127.0.0.1:3000",
        "http://127.0.0.1:3000/path",
        "http://127.0.0.1",
    ),
)
def test_settings_reject_nonlocal_or_credentialed_origins(origin: str) -> None:
    with pytest.raises(ValueError, match="explicit local http origin"):
        ApiSettings(web_origin=origin)
