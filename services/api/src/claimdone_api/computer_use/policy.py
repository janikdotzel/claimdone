"""Exact local-origin and approval-boundary policy for CU-001."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from .models import DEFAULT_PORTAL_ORIGIN, ComputerUseBlockReason

_APPROVAL_TOKEN = re.compile(
    r"(?:approv(?:e|ed|al)?|authoriz(?:e|ation)|receipt|"
    r"submit(?:ted|ting)?|submission)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PortalOriginPolicy:
    """Allow exactly one explicit IPv4-loopback HTTP origin."""

    origin: str = DEFAULT_PORTAL_ORIGIN
    _netloc: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.origin) is not str:
            raise ValueError("Portal origin must be a string")
        try:
            parsed = urlsplit(self.origin)
            port = parsed.port
        except ValueError as error:
            raise ValueError("Portal origin is invalid") from error
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Portal origin must be an explicit local HTTP origin")
        expected = f"http://127.0.0.1:{port}"
        if self.origin.rstrip("/") != expected:
            raise ValueError("Portal origin must use the canonical loopback spelling")
        object.__setattr__(self, "origin", expected)
        object.__setattr__(self, "_netloc", f"127.0.0.1:{port}")

    def reason_url_is_blocked(self, url: str) -> ComputerUseBlockReason | None:
        """Return a deterministic block reason without reflecting the URL."""

        return self._reason_network_url_is_blocked(url, expected_scheme="http")

    def reason_websocket_is_blocked(self, url: str) -> ComputerUseBlockReason | None:
        """Apply the same exact-origin boundary to browser WebSocket egress."""

        return self._reason_network_url_is_blocked(url, expected_scheme="ws")

    def _reason_network_url_is_blocked(
        self,
        url: str,
        *,
        expected_scheme: str,
    ) -> ComputerUseBlockReason | None:
        """Validate one canonical HTTP-equivalent network URL."""

        if type(url) is not str or not url or "\\" in url:
            return ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED
        if (
            parsed.scheme != expected_scheme
            or parsed.netloc != self._netloc
            or parsed.hostname != "127.0.0.1"
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith("/")
        ):
            return ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED
        target = f"{parsed.path}?{parsed.query}#{parsed.fragment}"
        if "%" in target:
            decoded = unquote(target, errors="strict")
            if decoded == target or "%" in decoded:
                return ComputerUseBlockReason.NAVIGATION_NOT_ALLOWED
            target = decoded
        if _APPROVAL_TOKEN.search(target):
            return ComputerUseBlockReason.APPROVAL_ACTION_BLOCKED
        return None

    def require_allowed_url(self, url: str) -> None:
        """Raise a content-free policy violation when the URL is not allowed."""

        reason = self.reason_url_is_blocked(url)
        if reason is not None:
            from .ports import BrowserPolicyViolation

            raise BrowserPolicyViolation(reason)

    def case_url(self, case_id: str) -> str:
        """Construct the only initial V1 Portal A surface."""

        if (
            type(case_id) is not str
            or not 1 <= len(case_id) <= 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", case_id) is None
        ):
            raise ValueError("caseId is not a canonical identifier")
        return f"{self.origin}/sandbox/A/cases/{case_id}"
