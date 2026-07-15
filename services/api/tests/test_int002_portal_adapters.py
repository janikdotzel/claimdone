"""Focused contract and semantic-boundary tests for the INT-002 portal adapters."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from claimdone_api.computer_use import (
    BrowserOperationError,
    HttpPortalGateway,
    PlaywrightSemanticPortalBrowser,
    PortalGatewayError,
    PortalOriginPolicy,
    RenderedCapture,
)
from claimdone_api.contracts import (
    CONTRACT_VERSION,
    PortalRunExpectedFields,
    PortalRunRelease,
    PortalRunRenderFaultInjection,
    PortalRunRenderFaultRepair,
    PortalRunSetup,
    PortalSessionView,
    PortalVariant,
    RenderedPortalSnapshot,
)

TOKEN = "portal-control-token-that-is-server-only-00000001"
CASE_ID = "case-int002-portal"
RUN_ID = "run-int002-portal"
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\nsemantic-rendered-values"


def expected_fields() -> PortalRunExpectedFields:
    return PortalRunExpectedFields.model_validate(
        {
            "incidentDate": "2026-07-14",
            "incidentTime": "08:15:30",
            "location": "Invalidenstrasse 116, Berlin",
            "claimantName": "Demo Claimant",
            "policyReference": "DEMO-POLICY-001",
            "vehicleRegistration": "B-CD-1001",
            "counterpartyKnown": "unknown",
            "narrative": "Synthetic incident narrative for the local sandbox.",
            "attachments": [
                "asset-demo-front",
                "asset-demo-rear",
                "asset-demo-context",
            ],
        }
    )


def setup_command() -> PortalRunSetup:
    return PortalRunSetup.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "runId": RUN_ID,
            "caseId": CASE_ID,
            "variant": "A",
            "expectedFields": expected_fields().model_dump(mode="json", by_alias=True),
        }
    )


def release_command() -> PortalRunRelease:
    return PortalRunRelease.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "runId": RUN_ID,
            "caseId": CASE_ID,
            "variant": "A",
        }
    )


def fault_command() -> PortalRunRenderFaultInjection:
    return PortalRunRenderFaultInjection.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "runId": RUN_ID,
            "caseId": CASE_ID,
            "variant": "A",
            "expectedVersion": 3,
            "field": "incident_time",
        }
    )


def repair_command() -> PortalRunRenderFaultRepair:
    return PortalRunRenderFaultRepair.model_validate(
        fault_command().model_dump(mode="json", by_alias=True)
    )


def portal_session(
    *,
    version: int,
    state: str,
    fields: PortalRunExpectedFields | None = None,
) -> PortalSessionView:
    values: dict[str, object]
    if fields is None:
        values = {
            "incidentDate": "",
            "incidentTime": "",
            "location": "",
            "claimantName": "",
            "policyReference": "",
            "vehicleRegistration": "",
            "counterpartyKnown": "",
            "narrative": "",
            "attachments": list(expected_fields().attachments),
        }
    else:
        values = fields.model_dump(mode="json", by_alias=True)
    return PortalSessionView.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": CASE_ID,
            "variant": "A",
            "state": state,
            "version": version,
            "fields": values,
            "updatedAt": NOW + timedelta(seconds=version),
            "auditCount": version,
        }
    )


def rendered_snapshot(
    *,
    version: int = 3,
    rendered_at: datetime = NOW + timedelta(seconds=10),
    case_id: str = CASE_ID,
) -> RenderedPortalSnapshot:
    return RenderedPortalSnapshot.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": case_id,
            "variant": "A",
            "state": "review",
            "version": version,
            "fields": expected_fields().model_dump(mode="json", by_alias=True),
            "renderedAt": rendered_at,
        }
    )


def test_http_gateway_binds_control_and_public_reads_without_leaking_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/setup"):
            return httpx.Response(
                201,
                json=portal_session(version=1, state="draft").model_dump(
                    mode="json", by_alias=True
                ),
            )
        if path.endswith("/inject-render-fault"):
            return httpx.Response(204)
        if path.endswith("/repair-render-fault"):
            return httpx.Response(
                200,
                json=portal_session(
                    version=4,
                    state="review",
                    fields=expected_fields(),
                ).model_dump(mode="json", by_alias=True),
            )
        if path.endswith("/release") or path.endswith("/abort"):
            return httpx.Response(204)
        if path.endswith("/rendered-values"):
            return httpx.Response(
                200,
                json=rendered_snapshot().model_dump(mode="json", by_alias=True),
            )
        return httpx.Response(
            200,
            json=portal_session(
                version=3,
                state="review",
                fields=expected_fields(),
            ).model_dump(mode="json", by_alias=True),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = HttpPortalGateway(control_token=TOKEN, client=client)
    try:
        assert gateway.setup_run(setup_command()).version == 1
        assert gateway.read_session(CASE_ID).version == 3
        assert gateway.read_rendered(CASE_ID).version == 3
        gateway.inject_render_fault(fault_command())
        assert gateway.repair_render_fault(repair_command()).version == 4
        gateway.release_run(release_command())
        gateway.abort_run(release_command())
        gateway.close()
        gateway.close()
    finally:
        client.close()

    assert requests
    for request in requests:
        assert request.url.scheme == "http"
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 3000
        assert TOKEN not in str(request.url)
        assert TOKEN.encode() not in request.content
        if request.url.path.startswith("/api/internal/portal-runs/"):
            assert request.headers["X-ClaimDone-Portal-Control"] == TOKEN
        else:
            assert "X-ClaimDone-Portal-Control" not in request.headers
    assert TOKEN not in repr(gateway)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000",
        "http://[::1]:3000",
        "https://127.0.0.1:3000",
        "http://127.0.0.1:3001/path",
        "http://example.invalid:3000",
    ],
)
def test_http_gateway_rejects_every_noncanonical_origin(origin: str) -> None:
    with pytest.raises(ValueError, match="explicit local HTTP origin|canonical loopback"):
        HttpPortalGateway(control_token=TOKEN, origin=origin)


@pytest.mark.parametrize(
    "token",
    ["short", f" {TOKEN}", f"{TOKEN}\n", "x" * 513],
)
def test_http_gateway_rejects_malformed_server_token(token: str) -> None:
    with pytest.raises(ValueError, match="control token"):
        HttpPortalGateway(control_token=token)


def test_http_gateway_does_not_follow_redirect_or_reflect_response_content() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            headers={"Location": "https://example.invalid/steal"},
            text=f"forbidden {TOKEN}",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    gateway = HttpPortalGateway(control_token=TOKEN, client=client)
    try:
        with pytest.raises(PortalGatewayError) as captured:
            gateway.read_session(CASE_ID)
        assert captured.value.status_code == 307
        assert TOKEN not in str(captured.value)
        assert calls == 1
    finally:
        client.close()


class FakeSemanticSession:
    def __init__(self, body: str, screenshot: bytes = PNG) -> None:
        self.body = body
        self.screenshot_bytes = screenshot
        self.operations: list[tuple[str, ...]] = []
        self.close_count = 0

    def navigate(self, url: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.operations.append(("navigate", url))

    def fill_label(self, label: str, value: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.operations.append(("fill", label, value))

    def select_label(self, label: str, value: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.operations.append(("select", label, value))

    def click_button(self, name: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.operations.append(("button", name))

    def wait_text(self, text: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.operations.append(("text", text))

    def wait_heading(self, text: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        self.operations.append(("heading", text))

    def body_text(self, *, timeout_seconds: float) -> str:
        del timeout_seconds
        self.operations.append(("body",))
        return self.body

    def screenshot(self, *, timeout_seconds: float) -> bytes:
        del timeout_seconds
        self.operations.append(("screenshot",))
        return self.screenshot_bytes

    def close(self) -> None:
        self.close_count += 1


class FakeSemanticSessionFactory:
    def __init__(self, session: FakeSemanticSession) -> None:
        self.session = session
        self.origins: list[str] = []

    def open_case(
        self,
        case_id: str,
        *,
        policy: PortalOriginPolicy,
        viewport_width: int,
        viewport_height: int,
        wait_action_seconds: float,
        timeout_seconds: float,
    ) -> FakeSemanticSession:
        assert case_id == CASE_ID
        assert (viewport_width, viewport_height) == (1280, 900)
        assert wait_action_seconds == 0.05
        assert timeout_seconds == 15.0
        self.origins.append(policy.origin)
        return self.session


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class SequenceMonotonic:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_semantic_browser_uses_closed_labels_and_captures_rendered_json_png() -> None:
    requested_at = NOW + timedelta(seconds=9)
    received_at = NOW + timedelta(seconds=11)
    snapshot = rendered_snapshot(rendered_at=NOW + timedelta(seconds=10))
    session = FakeSemanticSession(snapshot.model_dump_json(by_alias=True))
    factory = FakeSemanticSessionFactory(session)
    browser = PlaywrightSemanticPortalBrowser(
        session_factory=factory,
        clock=SequenceClock(requested_at, received_at),
        monotonic_clock=SequenceMonotonic(100.0, 100.1, 100.2, 100.3),
    )

    browser.open_case(CASE_ID)
    browser.fill_expected_fields(expected_fields())
    browser.save_draft()
    browser.continue_to_review()
    capture = browser.capture_rendered_values()
    browser.close()
    browser.close()

    assert factory.origins == ["http://127.0.0.1:3000"]
    assert session.operations[0] == (
        "navigate",
        f"http://127.0.0.1:3000/sandbox/A/cases/{CASE_ID}",
    )
    assert ("fill", "Incident time", "08:15:30") in session.operations
    assert (
        "select",
        "Are the other driver's details known?",
        "unknown",
    ) in session.operations
    assert ("button", "Save draft") in session.operations
    assert ("text", "Server version 2") in session.operations
    assert ("button", "Continue to review") in session.operations
    assert ("heading", "Ready for human review") in session.operations
    assert session.operations[-3:] == [
        (
            "navigate",
            (
                f"http://127.0.0.1:3000/api/sandbox/cases/{CASE_ID}"
                "/rendered-values?variant=A"
            ),
        ),
        ("body",),
        ("screenshot",),
    ]
    assert all("attachment" not in " ".join(operation).lower() for operation in session.operations)
    assert all("approval" not in " ".join(operation).lower() for operation in session.operations)
    assert all("receipt" not in " ".join(operation).lower() for operation in session.operations)
    assert capture == RenderedCapture(
        snapshot=snapshot,
        screenshot_sha256=hashlib.sha256(PNG).hexdigest(),
        requested_at=requested_at,
        received_at=received_at,
    )
    assert session.close_count == 1


def test_semantic_browser_rejects_layout_b_and_foreign_or_non_png_capture() -> None:
    foreign = rendered_snapshot(
        rendered_at=NOW + timedelta(seconds=10),
        case_id="case-foreign",
    )
    session = FakeSemanticSession(foreign.model_dump_json(by_alias=True))
    browser = PlaywrightSemanticPortalBrowser(
        session_factory=FakeSemanticSessionFactory(session),
        clock=SequenceClock(
            NOW + timedelta(seconds=9),
            NOW + timedelta(seconds=11),
        ),
        monotonic_clock=SequenceMonotonic(1.0, 1.1, 1.2),
    )
    with pytest.raises(BrowserOperationError):
        browser.open_case(CASE_ID, PortalVariant.B)
    browser.open_case(CASE_ID)
    with pytest.raises(BrowserOperationError):
        browser.capture_rendered_values()
    browser.close()

    valid = rendered_snapshot(rendered_at=NOW + timedelta(seconds=10))
    invalid_png = FakeSemanticSession(valid.model_dump_json(by_alias=True), b"not-a-png")
    invalid_browser = PlaywrightSemanticPortalBrowser(
        session_factory=FakeSemanticSessionFactory(invalid_png),
        clock=SequenceClock(NOW + timedelta(seconds=9)),
        monotonic_clock=SequenceMonotonic(2.0, 2.1, 2.2, 2.3),
    )
    invalid_browser.open_case(CASE_ID)
    with pytest.raises(BrowserOperationError):
        invalid_browser.capture_rendered_values()
    invalid_browser.close()


def test_semantic_browser_can_open_directly_on_fresh_render_capture() -> None:
    requested_at = NOW + timedelta(seconds=9)
    received_at = NOW + timedelta(seconds=11)
    snapshot = rendered_snapshot(rendered_at=NOW + timedelta(seconds=10))
    session = FakeSemanticSession(snapshot.model_dump_json(by_alias=True))
    browser = PlaywrightSemanticPortalBrowser(
        session_factory=FakeSemanticSessionFactory(session),
        clock=SequenceClock(requested_at, received_at),
        monotonic_clock=SequenceMonotonic(10.0, 10.1, 10.2, 10.3),
    )

    capture = browser.capture_rendered_values(CASE_ID, PortalVariant.A)
    browser.close()

    assert capture.snapshot == snapshot
    assert session.operations == [
        (
            "navigate",
            (
                f"http://127.0.0.1:3000/api/sandbox/cases/{CASE_ID}"
                "/rendered-values?variant=A"
            ),
        ),
        ("body",),
        ("screenshot",),
    ]


def test_rendered_capture_rejects_stale_or_unbound_timestamps() -> None:
    snapshot = rendered_snapshot(rendered_at=NOW + timedelta(seconds=10))
    with pytest.raises(ValueError, match="fresh and canonical"):
        RenderedCapture(
            snapshot=snapshot,
            screenshot_sha256="a" * 64,
            requested_at=NOW,
            received_at=NOW + timedelta(seconds=11),
        )
