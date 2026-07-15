"""Server-only portal control and semantic browser adapters for INT-002."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Protocol, Self
from urllib.parse import quote

import httpx
from playwright.sync_api import Locator
from pydantic import ValidationError

from claimdone_api.contracts import (
    PortalRunExpectedFields,
    PortalRunRelease,
    PortalRunRenderFaultInjection,
    PortalRunRenderFaultRepair,
    PortalRunSetup,
    PortalSessionView,
    PortalState,
    PortalVariant,
    RenderedPortalSnapshot,
)

from .models import DEFAULT_PORTAL_ORIGIN, ComputerUseBlockReason
from .playwright import PlaywrightBrowserFactory, PlaywrightBrowserSession
from .policy import PortalOriginPolicy
from .ports import (
    BrowserOperationError,
    BrowserOperationTimeout,
    BrowserPolicyViolation,
)

PORTAL_CONTROL_HEADER = "X-ClaimDone-Portal-Control"
MAX_VERIFICATION_CAPTURE_SECONDS = 5.0
_DEFAULT_CAPTURE_TIMEOUT_SECONDS = 4.5
_CONTROL_TOKEN = re.compile(r"^[!-~]{32,512}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
_APPROVAL_SEMANTIC = re.compile(
    r"(?:approv(?:e|ed|al)?|authoriz(?:e|ation)|receipt|"
    r"submit(?:ted|ting)?|submission)",
    re.IGNORECASE,
)

_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("Incident date", "incident_date"),
    ("Incident time", "incident_time"),
    ("Incident location", "location"),
    ("Claimant name", "claimant_name"),
    ("Policy reference", "policy_reference"),
    ("Vehicle registration", "vehicle_registration"),
    ("What happened?", "narrative"),
)
_COUNTERPARTY_LABEL = "Are the other driver's details known?"
_SAVE_BUTTON = "Save draft"
_SAVE_STATUS = "Draft saved on the sandbox server."
_REVIEW_BUTTON = "Continue to review"
_REVIEW_HEADING = "Ready for human review"
_LOCATOR_SEMANTICS_SCRIPT = """
target => {
  const clip = (value) => String(value ?? '').slice(0, 256);
  const labels = target.labels
    ? Array.from(target.labels).slice(0, 8).map((label) => clip(label.textContent))
    : [];
  return [
    clip(target.getAttribute('aria-label')),
    clip(target.getAttribute('data-action')),
    clip(target.getAttribute('href')),
    clip(target.getAttribute('id')),
    clip(target.getAttribute('name')),
    clip(target.getAttribute('role')),
    clip(target.getAttribute('title')),
    clip(target.getAttribute('value')),
    clip(target.textContent),
    ...labels,
  ].filter(Boolean).join(' ').slice(0, 4096);
}
"""


class PortalGatewayError(RuntimeError):
    """A content-free local portal HTTP or contract failure."""

    def __init__(self, operation: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.status_code = status_code
        super().__init__(f"Local portal {operation} failed")


@dataclass(frozen=True, slots=True)
class RenderedCapture:
    """One fresh rendered JSON snapshot and digest-only screenshot evidence."""

    snapshot: RenderedPortalSnapshot
    screenshot_sha256: str
    requested_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot, RenderedPortalSnapshot)
            or type(self.screenshot_sha256) is not str
            or _SHA256.fullmatch(self.screenshot_sha256) is None
            or type(self.requested_at) is not datetime
            or self.requested_at.utcoffset() is None
            or type(self.received_at) is not datetime
            or self.received_at.utcoffset() is None
            or not (
                self.requested_at
                <= self.snapshot.rendered_at
                <= self.received_at
            )
            or self.received_at - self.requested_at
            > timedelta(seconds=MAX_VERIFICATION_CAPTURE_SECONDS)
        ):
            raise ValueError("Rendered capture is not fresh and canonical")


class PortalGateway(Protocol):
    """Closed server-to-server portal control and read surface."""

    def setup_run(self, command: PortalRunSetup) -> PortalSessionView: ...

    def read_session(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
    ) -> PortalSessionView: ...

    def read_rendered(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
    ) -> RenderedPortalSnapshot: ...

    def inject_render_fault(self, command: PortalRunRenderFaultInjection) -> None: ...

    def repair_render_fault(
        self,
        command: PortalRunRenderFaultRepair,
    ) -> PortalSessionView: ...

    def release_run(self, command: PortalRunRelease) -> None: ...

    def abort_run(self, command: PortalRunRelease) -> None: ...

    def close(self) -> None: ...


class HttpPortalGateway:
    """Exact-origin HTTP adapter; the control credential never reaches Chromium."""

    def __init__(
        self,
        *,
        control_token: str,
        origin: str = DEFAULT_PORTAL_ORIGIN,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._policy = PortalOriginPolicy(origin)
        if type(control_token) is not str or _CONTROL_TOKEN.fullmatch(control_token) is None:
            raise ValueError("Portal control token is invalid")
        if (
            type(timeout_seconds) not in {int, float}
            or not 0.001 <= float(timeout_seconds) <= 30.0
        ):
            raise ValueError("Portal gateway timeout is invalid")
        if client is not None and type(client) is not httpx.Client:
            raise TypeError("client must be an exact httpx.Client")
        self._control_token = control_token
        self._client = client or httpx.Client(
            base_url=self._policy.origin,
            follow_redirects=False,
            timeout=float(timeout_seconds),
            trust_env=False,
        )
        self._owns_client = client is None
        self._closed = False

    def __enter__(self) -> Self:
        if self._closed:
            raise PortalGatewayError("open")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def setup_run(self, command: PortalRunSetup) -> PortalSessionView:
        if not isinstance(command, PortalRunSetup):
            raise TypeError("command must be PortalRunSetup")
        response = self._control_request("setup", command, expected_status=201)
        view = self._session(response, "setup")
        expected = command.expected_fields
        if (
            view.case_id != command.case_id
            or view.variant is not command.variant
            or view.state is not PortalState.DRAFT
            or view.version != 1
            or view.fields.incident_date
            or view.fields.incident_time
            or view.fields.location
            or view.fields.claimant_name
            or view.fields.policy_reference
            or view.fields.vehicle_registration
            or view.fields.counterparty_known != ""
            or view.fields.narrative
            or view.fields.attachments != expected.attachments
        ):
            raise PortalGatewayError("setup-contract")
        return view

    def read_session(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
    ) -> PortalSessionView:
        encoded, canonical_variant = self._read_identity(case_id, variant)
        response = self._request(
            "GET",
            f"/api/sandbox/cases/{encoded}?variant={canonical_variant.value}",
            expected_status=200,
            operation="read-session",
        )
        view = self._session(response, "read-session")
        if view.case_id != case_id or view.variant is not canonical_variant:
            raise PortalGatewayError("read-session-contract")
        return view

    def read_rendered(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
    ) -> RenderedPortalSnapshot:
        encoded, canonical_variant = self._read_identity(case_id, variant)
        response = self._request(
            "GET",
            (
                f"/api/sandbox/cases/{encoded}/rendered-values"
                f"?variant={canonical_variant.value}"
            ),
            expected_status=200,
            operation="read-rendered",
        )
        try:
            snapshot = RenderedPortalSnapshot.model_validate_json(response.content)
        except (ValidationError, ValueError):
            raise PortalGatewayError("read-rendered-contract") from None
        if snapshot.case_id != case_id or snapshot.variant is not canonical_variant:
            raise PortalGatewayError("read-rendered-contract")
        return snapshot

    def inject_render_fault(self, command: PortalRunRenderFaultInjection) -> None:
        if not isinstance(command, PortalRunRenderFaultInjection):
            raise TypeError("command must be PortalRunRenderFaultInjection")
        self._empty_control_request(
            "inject-render-fault",
            command,
            operation="inject-render-fault",
        )

    def repair_render_fault(
        self,
        command: PortalRunRenderFaultRepair,
    ) -> PortalSessionView:
        if not isinstance(command, PortalRunRenderFaultRepair):
            raise TypeError("command must be PortalRunRenderFaultRepair")
        response = self._control_request(
            "repair-render-fault",
            command,
            expected_status=200,
        )
        view = self._session(response, "repair-render-fault")
        if (
            view.case_id != command.case_id
            or view.variant is not command.variant
            or view.state is not PortalState.REVIEW
            or view.version != command.expected_version + 1
        ):
            raise PortalGatewayError("repair-render-fault-contract")
        return view

    def release_run(self, command: PortalRunRelease) -> None:
        if not isinstance(command, PortalRunRelease):
            raise TypeError("command must be PortalRunRelease")
        self._empty_control_request("release", command, operation="release")

    def abort_run(self, command: PortalRunRelease) -> None:
        if not isinstance(command, PortalRunRelease):
            raise TypeError("command must be PortalRunRelease")
        self._empty_control_request("abort", command, operation="abort")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self._client.close()

    def _control_request(
        self,
        action: str,
        command: (
            PortalRunSetup
            | PortalRunRelease
            | PortalRunRenderFaultInjection
            | PortalRunRenderFaultRepair
        ),
        *,
        expected_status: int,
    ) -> httpx.Response:
        return self._request(
            "POST",
            f"/api/internal/portal-runs/{action}",
            expected_status=expected_status,
            operation=action,
            headers={
                "Content-Type": "application/json",
                PORTAL_CONTROL_HEADER: self._control_token,
            },
            json=command.model_dump(mode="json", by_alias=True),
        )

    def _empty_control_request(
        self,
        action: str,
        command: PortalRunRelease | PortalRunRenderFaultInjection,
        *,
        operation: str,
    ) -> None:
        response = self._control_request(action, command, expected_status=204)
        if response.content:
            raise PortalGatewayError(f"{operation}-contract")

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        operation: str,
        headers: dict[str, str] | None = None,
        json: object | None = None,
    ) -> httpx.Response:
        if self._closed:
            raise PortalGatewayError(operation)
        url = f"{self._policy.origin}{path}"
        self._policy.require_allowed_url(url)
        try:
            response = self._client.request(
                method,
                url,
                headers=headers,
                json=json,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise PortalGatewayError(operation) from None
        if response.status_code != expected_status:
            raise PortalGatewayError(operation, response.status_code)
        if str(response.url).split("?", 1)[0] != url.split("?", 1)[0]:
            raise PortalGatewayError(operation)
        return response

    @staticmethod
    def _session(response: httpx.Response, operation: str) -> PortalSessionView:
        try:
            return PortalSessionView.model_validate_json(response.content)
        except (ValidationError, ValueError):
            raise PortalGatewayError(f"{operation}-contract") from None

    @staticmethod
    def _read_identity(
        case_id: str,
        variant: PortalVariant,
    ) -> tuple[str, PortalVariant]:
        if type(case_id) is not str or _IDENTIFIER.fullmatch(case_id) is None:
            raise ValueError("case_id must be canonical")
        if not isinstance(variant, PortalVariant):
            raise ValueError("variant must be canonical")
        return quote(case_id, safe=""), variant


class SemanticPortalBrowser(Protocol):
    """Closed browser surface with no caller-supplied selectors, URLs, or headers."""

    def open_case(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
        *,
        timeout_seconds: float = 15.0,
    ) -> None: ...

    def fill_expected_fields(
        self,
        fields: PortalRunExpectedFields,
        *,
        timeout_seconds: float = 5.0,
    ) -> None: ...

    def save_draft(self, *, timeout_seconds: float = 5.0) -> None: ...

    def continue_to_review(self, *, timeout_seconds: float = 5.0) -> None: ...

    def capture_rendered_values(
        self,
        case_id: str | None = None,
        variant: PortalVariant = PortalVariant.A,
        *,
        timeout_seconds: float = _DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    ) -> RenderedCapture: ...

    def close(self) -> None: ...


class _SemanticSession(Protocol):
    def navigate(self, url: str, *, timeout_seconds: float) -> None: ...

    def fill_label(self, label: str, value: str, *, timeout_seconds: float) -> None: ...

    def select_label(self, label: str, value: str, *, timeout_seconds: float) -> None: ...

    def click_button(self, name: str, *, timeout_seconds: float) -> None: ...

    def wait_text(self, text: str, *, timeout_seconds: float) -> None: ...

    def wait_heading(self, text: str, *, timeout_seconds: float) -> None: ...

    def body_text(self, *, timeout_seconds: float) -> str: ...

    def screenshot(self, *, timeout_seconds: float) -> bytes: ...

    def close(self) -> None: ...


class _SemanticSessionFactory(Protocol):
    def open_case(
        self,
        case_id: str,
        *,
        policy: PortalOriginPolicy,
        viewport_width: int,
        viewport_height: int,
        wait_action_seconds: float,
        timeout_seconds: float,
    ) -> _SemanticSession: ...


class _PlaywrightSemanticSession:
    """Semantic bridge over the already hardened CU-001 Playwright session."""

    def __init__(self, delegate: PlaywrightBrowserSession) -> None:
        self._delegate = delegate
        self._page = delegate._page

    def navigate(self, url: str, *, timeout_seconds: float) -> None:
        self._delegate.navigate(url, timeout_seconds=timeout_seconds)

    def fill_label(self, label: str, value: str, *, timeout_seconds: float) -> None:
        self._require_safe_semantic(label)
        locator = self._page.get_by_label(label, exact=True)
        self._require_safe_locator(locator)
        self._semantic_call(
            lambda: locator.fill(
                value,
                timeout=_milliseconds(timeout_seconds),
            )
        )

    def select_label(self, label: str, value: str, *, timeout_seconds: float) -> None:
        self._require_safe_semantic(label)
        locator = self._page.get_by_label(label, exact=True)
        self._require_safe_locator(locator)
        self._semantic_call(
            lambda: locator.select_option(
                value,
                timeout=_milliseconds(timeout_seconds),
            )
        )

    def click_button(self, name: str, *, timeout_seconds: float) -> None:
        self._require_safe_semantic(name)
        locator = self._page.get_by_role(
            "button",
            name=name,
            exact=True,
        )
        self._require_safe_locator(locator)
        self._semantic_call(
            lambda: locator.click(timeout=_milliseconds(timeout_seconds))
        )

    def wait_text(self, text: str, *, timeout_seconds: float) -> None:
        self._semantic_call(
            lambda: self._page.get_by_text(text, exact=True).wait_for(
                state="visible",
                timeout=_milliseconds(timeout_seconds),
            )
        )

    def wait_heading(self, text: str, *, timeout_seconds: float) -> None:
        self._semantic_call(
            lambda: self._page.get_by_role(
                "heading",
                name=text,
                exact=True,
            ).wait_for(
                state="visible",
                timeout=_milliseconds(timeout_seconds),
            )
        )

    def body_text(self, *, timeout_seconds: float) -> str:
        value = self._semantic_call(
            lambda: self._page.locator("body").inner_text(
                timeout=_milliseconds(timeout_seconds)
            )
        )
        if type(value) is not str:
            raise BrowserOperationError
        return value

    def screenshot(self, *, timeout_seconds: float) -> bytes:
        return self._delegate.screenshot(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._delegate.close()

    def _semantic_call(self, callback: Callable[[], object]) -> object:
        self._delegate.assert_safe()
        value = self._delegate._call(callback)
        self._delegate.assert_safe()
        return value

    def _require_safe_locator(self, locator: Locator) -> None:
        value = self._semantic_call(
            lambda: locator.evaluate(_LOCATOR_SEMANTICS_SCRIPT)
        )
        semantics = value[:4_096] if type(value) is str else ""
        self._require_safe_semantic(semantics)

    @staticmethod
    def _require_safe_semantic(value: str) -> None:
        if _APPROVAL_SEMANTIC.search(value):
            raise BrowserPolicyViolation(
                ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED
            )


class _PlaywrightSemanticSessionFactory:
    def __init__(self, *, headless: bool) -> None:
        self._factory = PlaywrightBrowserFactory(headless=headless)

    def open_case(
        self,
        case_id: str,
        *,
        policy: PortalOriginPolicy,
        viewport_width: int,
        viewport_height: int,
        wait_action_seconds: float,
        timeout_seconds: float,
    ) -> _SemanticSession:
        session = self._factory.open_case(
            case_id,
            policy=policy,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            wait_action_seconds=wait_action_seconds,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(session, PlaywrightBrowserSession):
            session.close()
            raise BrowserOperationError
        return _PlaywrightSemanticSession(session)


class PlaywrightSemanticPortalBrowser:
    """Case-bound semantic browser using exact Portal A labels and roles only."""

    def __init__(
        self,
        *,
        origin: str = DEFAULT_PORTAL_ORIGIN,
        headless: bool = True,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        session_factory: _SemanticSessionFactory | None = None,
    ) -> None:
        self._policy = PortalOriginPolicy(origin)
        if type(headless) is not bool:
            raise ValueError("headless must be a strict boolean")
        self._clock = clock or _utc_now
        self._monotonic = monotonic_clock
        self._session_factory = session_factory or _PlaywrightSemanticSessionFactory(
            headless=headless
        )
        self._session: _SemanticSession | None = None
        self._case_id: str | None = None
        self._variant: PortalVariant | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open_case(
        self,
        case_id: str,
        variant: PortalVariant = PortalVariant.A,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        if self._session is not None:
            raise BrowserOperationError
        if variant is not PortalVariant.A:
            raise BrowserOperationError
        _require_timeout(timeout_seconds, maximum=30.0)
        url = self._policy.case_url(case_id)
        session = self._create_session(case_id, variant, timeout_seconds)
        try:
            session.navigate(url, timeout_seconds=timeout_seconds)
        except Exception:
            session.close()
            self._clear_binding()
            raise

    def fill_expected_fields(
        self,
        fields: PortalRunExpectedFields,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(fields, PortalRunExpectedFields):
            raise TypeError("fields must be PortalRunExpectedFields")
        session = self._require_open()
        _require_timeout(timeout_seconds, maximum=30.0)
        for label, field_name in _TEXT_FIELDS:
            session.fill_label(
                label,
                getattr(fields, field_name),
                timeout_seconds=timeout_seconds,
            )
        session.select_label(
            _COUNTERPARTY_LABEL,
            fields.counterparty_known.value,
            timeout_seconds=timeout_seconds,
        )

    def save_draft(self, *, timeout_seconds: float = 5.0) -> None:
        session = self._require_open()
        _require_timeout(timeout_seconds, maximum=30.0)
        session.click_button(_SAVE_BUTTON, timeout_seconds=timeout_seconds)
        session.wait_text(_SAVE_STATUS, timeout_seconds=timeout_seconds)
        session.wait_text("Server version 2", timeout_seconds=timeout_seconds)

    def continue_to_review(self, *, timeout_seconds: float = 5.0) -> None:
        session = self._require_open()
        _require_timeout(timeout_seconds, maximum=30.0)
        session.click_button(_REVIEW_BUTTON, timeout_seconds=timeout_seconds)
        session.wait_heading(_REVIEW_HEADING, timeout_seconds=timeout_seconds)

    def capture_rendered_values(
        self,
        case_id: str | None = None,
        variant: PortalVariant = PortalVariant.A,
        *,
        timeout_seconds: float = _DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    ) -> RenderedCapture:
        _require_timeout(
            timeout_seconds,
            maximum=MAX_VERIFICATION_CAPTURE_SECONDS,
        )
        if self._session is None:
            if case_id is None:
                raise BrowserOperationError
            session = self._create_session(case_id, variant, 15.0)
        else:
            session = self._session
            if case_id is not None and case_id != self._case_id:
                raise BrowserOperationError
            if variant is not self._variant:
                raise BrowserOperationError
        bound_case_id = self._case_id
        bound_variant = self._variant
        if bound_case_id is None or bound_variant is not PortalVariant.A:
            raise BrowserOperationError
        encoded = quote(bound_case_id, safe="")
        url = (
            f"{self._policy.origin}/api/sandbox/cases/{encoded}/rendered-values"
            f"?variant={bound_variant.value}"
        )
        self._policy.require_allowed_url(url)
        deadline = self._now_monotonic() + timeout_seconds
        requested_at = self._aware_now()
        try:
            session.navigate(url, timeout_seconds=self._remaining(deadline))
            body = session.body_text(timeout_seconds=self._remaining(deadline))
            snapshot = RenderedPortalSnapshot.model_validate_json(body)
            if (
                snapshot.case_id != bound_case_id
                or snapshot.variant is not bound_variant
            ):
                raise ValueError("Foreign rendered snapshot")
            screenshot = session.screenshot(timeout_seconds=self._remaining(deadline))
            if (
                type(screenshot) is not bytes
                or not len(_PNG_SIGNATURE) < len(screenshot) <= _MAX_SCREENSHOT_BYTES
                or not screenshot.startswith(_PNG_SIGNATURE)
            ):
                raise BrowserOperationError
            received_at = self._aware_now()
            return RenderedCapture(
                snapshot=snapshot,
                screenshot_sha256=hashlib.sha256(screenshot).hexdigest(),
                requested_at=requested_at,
                received_at=received_at,
            )
        except BrowserOperationTimeout:
            raise
        except (BrowserOperationError, ValidationError, ValueError):
            raise BrowserOperationError from None

    def close(self) -> None:
        session = self._session
        self._clear_binding()
        if session is not None:
            session.close()

    def _create_session(
        self,
        case_id: str,
        variant: PortalVariant,
        timeout_seconds: float,
    ) -> _SemanticSession:
        if self._session is not None or variant is not PortalVariant.A:
            raise BrowserOperationError
        self._policy.case_url(case_id)
        session = self._session_factory.open_case(
            case_id,
            policy=self._policy,
            viewport_width=1280,
            viewport_height=900,
            wait_action_seconds=0.05,
            timeout_seconds=timeout_seconds,
        )
        self._session = session
        self._case_id = case_id
        self._variant = variant
        return session

    def _clear_binding(self) -> None:
        self._session = None
        self._case_id = None
        self._variant = None

    def _require_open(self) -> _SemanticSession:
        if self._session is None:
            raise BrowserOperationError
        return self._session

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._now_monotonic()
        if remaining <= 0:
            raise BrowserOperationTimeout
        return remaining

    def _aware_now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.utcoffset() is None:
            raise BrowserOperationError
        return value

    def _now_monotonic(self) -> float:
        value = self._monotonic()
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise BrowserOperationTimeout
        return float(value)


def _milliseconds(timeout_seconds: float) -> float:
    _require_timeout(timeout_seconds, maximum=30.0)
    return float(timeout_seconds) * 1_000


def _require_timeout(timeout_seconds: float, *, maximum: float) -> None:
    if (
        type(timeout_seconds) not in {int, float}
        or not 0.001 <= float(timeout_seconds) <= maximum
    ):
        raise BrowserOperationTimeout


def _utc_now() -> datetime:
    return datetime.now(UTC)
