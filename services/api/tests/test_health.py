from fastapi.testclient import TestClient

from claimdone_api.main import app


def test_health_reports_healthy_api() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"service": "api", "status": "ok"}
