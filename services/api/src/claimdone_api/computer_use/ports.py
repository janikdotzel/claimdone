"""Injected ports and sanitized failures for the CU-001 runner."""

from __future__ import annotations

from typing import Protocol

from claimdone_api.contracts import CaseState

from .models import (
    ComputerAction,
    ComputerTurn,
    ComputerUseBlockReason,
)
from .policy import PortalOriginPolicy


class BrowserPolicyViolation(RuntimeError):
    """A content-free deterministic browser policy failure."""

    def __init__(self, reason: ComputerUseBlockReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class BrowserOperationError(RuntimeError):
    """A content-free local browser failure."""


class BrowserOperationTimeout(BrowserOperationError):
    """A bounded browser operation exceeded its own timeout."""


class InvalidComputerResponseError(RuntimeError):
    """The provider response could not be reduced to the closed GA contract."""


class PendingComputerSafetyCheckError(InvalidComputerResponseError):
    """A valid computer call requires a provider safety acknowledgement."""


class ComputerBrowserSession(Protocol):
    """One isolated case-owned browser context."""

    def navigate(self, url: str, *, timeout_seconds: float) -> None:
        """Open the initial local portal URL after policy interception is active."""

    def execute(self, action: ComputerAction, *, timeout_seconds: float) -> None:
        """Execute exactly one already-counted action."""

    def screenshot(self, *, timeout_seconds: float) -> bytes:
        """Capture the current full browser viewport as PNG."""

    def assert_safe(self) -> None:
        """Raise a pending navigation, popup, download, or approval violation."""

    def close(self) -> None:
        """Idempotently close every resource owned by this case."""


class ComputerBrowserFactory(Protocol):
    """Create a fresh isolated browser context for one case."""

    def open_case(
        self,
        case_id: str,
        *,
        policy: PortalOriginPolicy,
        viewport_width: int,
        viewport_height: int,
        wait_action_seconds: float,
        timeout_seconds: float,
    ) -> ComputerBrowserSession:
        """Return a new case-owned session; never reuse a context."""


class CaseStateReader(Protocol):
    """Read the current canonical backend case state."""

    def current_state(self, case_id: str) -> CaseState:
        """Return the authoritative state for the selected case."""


class PortalReviewSignal(Protocol):
    """Read a trusted portal-owned review signal, independent of backend state."""

    def review_reached(self, case_id: str) -> bool:
        """Return true only after the selected local portal reached review."""


class ComputerResponsesPort(Protocol):
    """Exact two-operation surface of the GA Responses computer loop."""

    def start(self, task: str, *, timeout_seconds: float) -> ComputerTurn:
        """Send text plus the GA computer tool; no screenshot is invented here."""

    def continue_with_screenshot(
        self,
        *,
        previous_response_id: str,
        call_id: str,
        screenshot_png: bytes,
        timeout_seconds: float,
    ) -> ComputerTurn:
        """Return a PNG as computer_call_output for the exact preceding call."""
