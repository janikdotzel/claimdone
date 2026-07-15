"""Exact-origin policy tests for HTTP, WebSocket, and approval targets."""

import pytest

from claimdone_api.computer_use import ComputerUseBlockReason, PortalOriginPolicy


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1",
        "http://127.0.0.1:3000/path",
        "http://user@127.0.0.1:3000",
        "http://127.0.0.1.evil:3000",
        "HTTP://127.0.0.1:3000",
    ],
)
def test_rejects_noncanonical_portal_origins(origin: str) -> None:
    with pytest.raises(ValueError):
        PortalOriginPolicy(origin)


def test_allows_only_the_exact_configured_http_and_ws_origin() -> None:
    policy = PortalOriginPolicy("http://127.0.0.1:3000")

    assert policy.reason_url_is_blocked(
        "http://127.0.0.1:3000/sandbox/A/cases/case_1?variant=A#review"
    ) is None
    assert policy.reason_websocket_is_blocked("ws://127.0.0.1:3000/_next/hmr") is None

    blocked = (
        "https://127.0.0.1:3000/sandbox/A/cases/case_1",
        "http://127.0.0.1.evil:3000/sandbox",
        "http://127.0.0.1:3000@evil.example/sandbox",
        "http://127.0.0.1:3001/sandbox",
        "file:///tmp/claim",
        "javascript:alert(1)",
        "http://127.0.0.1:3000\\@evil.example/",
        "http://127.0.0.1:3000/%252e%252e/evil",
        "http://127.0.0.1:3000/%ZZ",
    )
    assert all(
        policy.reason_url_is_blocked(url)
        is ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED
        for url in blocked
    )
    assert policy.reason_websocket_is_blocked(
        "ws://evil.example/socket"
    ) is ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED
    assert policy.reason_websocket_is_blocked(
        "wss://127.0.0.1:3000/socket"
    ) is ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED


@pytest.mark.parametrize(
    "target",
    [
        "/approval",
        "/api/cases/case_1/approve",
        "/submit",
        "/submitted",
        "/receipt",
        "/sandbox?operation=authorization",
        "/humanApproval",
        "/requestReceipt",
        "/submitClaim",
        "/%61pproval",
        "/safe#submission",
    ],
)
def test_blocks_approval_submission_and_receipt_targets(target: str) -> None:
    policy = PortalOriginPolicy()
    assert policy.reason_url_is_blocked(
        f"http://127.0.0.1:3000{target}"
    ) is ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED


def test_builds_only_the_canonical_portal_a_case_url() -> None:
    policy = PortalOriginPolicy()
    assert (
        policy.case_url("case_demo-001")
        == "http://127.0.0.1:3000/sandbox/A/cases/case_demo-001"
    )
    with pytest.raises(ValueError):
        policy.case_url("../approval")
