"""HTTP-boundary tests for the unwired Case API router."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from claimdone_api.cases import CaseService, create_case_router
from claimdone_api.cases.errors import CaseVersionConflictError
from claimdone_api.cases.router import case_error_response
from claimdone_api.persistence import SqliteCaseRepository

NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)


@dataclass
class RecordingCleaner:
    """Test double for resources owned by the later media pipeline."""

    deleted_case_ids: list[str] = field(default_factory=list)
    reset_count: int = 0

    def delete_case_resources(self, case_id: str) -> None:
        self.deleted_case_ids.append(case_id)

    def reset_resources(self) -> None:
        self.reset_count += 1


def _build_client(database_path: Path) -> TestClient:
    service = CaseService(
        SqliteCaseRepository(database_path),
        now=lambda: NOW,
        case_id_factory=lambda: "case-api-001",
    )
    app = FastAPI()
    app.include_router(create_case_router(service))
    return TestClient(app)


def _json_object(response_json: object) -> dict[str, Any]:
    return cast(dict[str, Any], response_json)


def test_create_get_delete_round_trip_redacts_metadata(tmp_path: Path) -> None:
    client = _build_client(tmp_path / "cases.db")
    raw_claimant = "Ada Lovelace"
    raw_filename = "private-incident.jpg"

    created = client.post(
        "/api/cases",
        json={
            "metadata": {
                "claimantName": raw_claimant,
                "attachmentNames": [raw_filename, "second.jpg", "third.jpg"],
                "counterpartyKnown": False,
            }
        },
    )

    assert created.status_code == 201
    created_body = _json_object(created.json())
    assert created_body == {
        "caseId": "case-api-001",
        "version": 1,
        "state": "created",
        "portalState": "draft",
        "redactedMetadata": {
            "attachmentNames": "array(items=3)",
            "claimantName": "text(length=12)",
            "counterpartyKnown": "boolean",
        },
        "claimPacket": None,
        "intakeSummary": None,
        "activeClarification": None,
        "createdAt": "2026-07-14T12:00:00Z",
        "updatedAt": "2026-07-14T12:00:00Z",
    }
    assert raw_claimant not in created.text
    assert raw_filename not in created.text

    fetched = client.get("/api/cases/case-api-001")
    assert fetched.status_code == 200
    assert fetched.json() == created_body

    deleted = client.delete("/api/cases/case-api-001")
    assert deleted.status_code == 204
    assert deleted.content == b""

    missing = client.get("/api/cases/case-api-001")
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "CASE_NOT_FOUND",
            "message": "The case does not exist.",
            "reasonCodes": [],
            "fieldErrors": [],
            "gateDecision": None,
            "currentVersion": None,
        }
    }


def test_missing_case_and_repeated_delete_are_safe(tmp_path: Path) -> None:
    client = _build_client(tmp_path / "cases.db")

    missing = client.get("/api/cases/does-not-exist")
    first_delete = client.delete("/api/cases/does-not-exist")
    second_delete = client.delete("/api/cases/does-not-exist")

    assert missing.status_code == 404
    assert first_delete.status_code == 204
    assert second_delete.status_code == 204


def test_invalid_metadata_key_is_rejected_before_service_execution(tmp_path: Path) -> None:
    client = _build_client(tmp_path / "cases.db")

    invalid = client.post(
        "/api/cases",
        json={"metadata": {"Claimant Name": "Ada Lovelace"}},
    )

    assert invalid.status_code == 422
    assert client.get("/api/cases/case-api-001").status_code == 404


def test_syntactically_valid_pii_like_metadata_key_is_not_persisted(tmp_path: Path) -> None:
    database_path = tmp_path / "cases.db"
    client = _build_client(database_path)
    pii_like_key = "claimant_Janik_Dotzel"

    invalid = client.post(
        "/api/cases",
        json={"metadata": {pii_like_key: "private value"}},
    )

    assert invalid.status_code == 422
    assert client.get("/api/cases/case-api-001").status_code == 404
    assert pii_like_key.encode("utf-8") not in database_path.read_bytes()


def test_stale_version_maps_to_stable_http_409_envelope() -> None:
    response = case_error_response(
        CaseVersionConflictError(
            case_id="case-conflict",
            expected_version=1,
            current_version=2,
        )
    )

    assert response.status_code == 409
    assert bytes(response.body).decode("utf-8") == (
        '{"error":{"code":"CASE_VERSION_CONFLICT",'
        '"message":"The case changed since it was loaded.",'
        '"reasonCodes":[],"fieldErrors":[],"gateDecision":null,"currentVersion":2}}'
    )
