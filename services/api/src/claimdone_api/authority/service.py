"""Server-only capability issuance and human approval orchestration."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from claimdone_api.contracts import CaseState, GateId, PortalVariant, SandboxReceipt
from claimdone_api.persistence import (
    AuthorityCapabilityError,
    CaseRecord,
    CaseRecordNotFoundError,
    CaseRecordVersionConflictError,
    HumanApprovalCommand,
    PersistedDataIntegrityError,
    SqliteCaseRepository,
    WorkflowAtomicityError,
)

from .errors import (
    agent_forbidden,
    case_not_found,
    role_invalid,
    state_conflict,
    token_invalid,
)
from .models import (
    AuthorityPurpose,
    AuthorityRole,
    AuthorizedHumanApproval,
    IssuedCapability,
)

_TOKEN_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AGENT_TOKEN = re.compile(r"^cdcap_a_([A-Za-z0-9_-]{43})$")
_HUMAN_TOKEN = re.compile(r"^cdcap_h_([ab])_([A-Za-z0-9_-]{43})$")
_TOKEN_TTL = timedelta(seconds=90)
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def _new_approval_id(variant: PortalVariant) -> str:
    return f"approval-{variant.value.lower()}-{uuid4().hex}"


def _new_receipt_id() -> str:
    return f"receipt-{uuid4().hex}"


class AuthorityService:
    """Keep plaintext credentials out of persistence and agent-facing surfaces."""

    def __init__(
        self,
        repository: SqliteCaseRepository,
        *,
        now: Callable[[], datetime] = _utc_now,
        secret_factory: Callable[[], str] = _new_secret,
        approval_id_factory: Callable[[PortalVariant], str] = _new_approval_id,
        receipt_id_factory: Callable[[], str] = _new_receipt_id,
    ) -> None:
        if type(repository) is not SqliteCaseRepository:
            raise TypeError("AuthorityService requires the exact canonical repository type")
        self._repository = repository
        self._now = now
        self._secret_factory = secret_factory
        self._approval_id_factory = approval_id_factory
        self._receipt_id_factory = receipt_id_factory

    def issue_agent_capability(
        self,
        case_id: str,
        *,
        expected_version: int,
    ) -> IssuedCapability:
        """Issue a portal-run-only capability to a trusted runner service."""

        current = self._require_case_version(case_id, expected_version)
        if current.state not in {CaseState.READY_TO_FILL, CaseState.FILLING}:
            raise state_conflict(current_version=current.version)
        return self._issue(
            case_id,
            expected_version=expected_version,
            role="agent",
            purpose="portal_run",
            variant=None,
            not_before=current.updated_at,
        )

    def issue_human_approval_capability(
        self,
        case_id: str,
        *,
        expected_version: int,
        variant: PortalVariant,
    ) -> IssuedCapability:
        """Server-only OTP issuance; this method is intentionally not an HTTP route."""

        current = self._require_case_version(case_id, expected_version)
        packet = current.snapshot.claim_packet
        if (
            current.state is not CaseState.REVIEW
            or packet is None
            or packet.state is not CaseState.REVIEW
            or not packet.verification.review_allowed
            or tuple(decision.gate_id for decision in packet.gate_decisions)
            != tuple(GateId(f"G{index}") for index in range(9))
            or any(not decision.passed for decision in packet.gate_decisions)
        ):
            raise state_conflict(current_version=current.version)
        return self._issue(
            case_id,
            expected_version=expected_version,
            role="human",
            purpose="human_approve",
            variant=variant,
            not_before=current.updated_at,
        )

    def authorize_human_bearer(self, token: str) -> AuthorizedHumanApproval:
        """Classify bearer authority before reading request body or case state."""

        digest = self._digest(token)
        if digest is None:
            raise token_invalid()
        try:
            capability = self._repository.get_authority_capability(digest)
        except (ValueError, PersistedDataIntegrityError, WorkflowAtomicityError):
            raise token_invalid() from None
        if capability is None:
            raise token_invalid()
        if capability.role == "agent":
            raise agent_forbidden()
        if capability.role != "human" or capability.purpose != "human_approve":
            raise role_invalid()
        match = _HUMAN_TOKEN.fullmatch(token)
        if match is None:
            raise role_invalid()
        checked_at = self._aware_now()
        if (
            capability.consumed_at is not None
            or capability.revoked_at is not None
            or checked_at < capability.issued_at
            or checked_at >= capability.expires_at
        ):
            raise token_invalid()
        variant = PortalVariant(match.group(1).upper())
        if capability.portal_variant is not variant:
            raise token_invalid()
        return AuthorizedHumanApproval(
            digest=digest,
            case_id=capability.case_id,
            bound_case_version=capability.bound_case_version,
            variant=variant,
            checked_at=checked_at,
            issued_at=capability.issued_at,
            expires_at=capability.expires_at,
        )

    def approve_authorized(
        self,
        case_id: str,
        *,
        authorization: AuthorizedHumanApproval,
    ) -> SandboxReceipt:
        """Close the already-classified human boundary without carrying plaintext."""

        if case_id != authorization.case_id:
            raise token_invalid()
        consumed_at = self._strictly_after(
            max(authorization.checked_at, authorization.issued_at)
        )
        if consumed_at >= authorization.expires_at:
            raise token_invalid()
        approved_at = self._strictly_after(consumed_at)
        rendered_at = self._strictly_after(approved_at)
        try:
            result = self._repository.approve_human_and_create_receipt(
                HumanApprovalCommand(
                    case_id=case_id,
                    expected_case_version=authorization.bound_case_version,
                    capability_digest=authorization.digest,
                    portal_variant=authorization.variant,
                    approval_id=self._approval_id_factory(authorization.variant),
                    receipt_id=self._receipt_id_factory(),
                    consumed_at=consumed_at,
                    approved_at=approved_at,
                    rendered_at=rendered_at,
                )
            )
        except CaseRecordNotFoundError:
            raise case_not_found() from None
        except CaseRecordVersionConflictError as error:
            raise state_conflict(current_version=error.current_version) from None
        except AuthorityCapabilityError:
            raise token_invalid() from None
        except (PersistedDataIntegrityError, WorkflowAtomicityError):
            raise state_conflict() from None
        return result.receipt.receipt

    def _issue(
        self,
        case_id: str,
        *,
        expected_version: int,
        role: AuthorityRole,
        purpose: AuthorityPurpose,
        variant: PortalVariant | None,
        not_before: datetime,
    ) -> IssuedCapability:
        secret = self._secret_factory()
        if type(secret) is not str or _TOKEN_SECRET.fullmatch(secret) is None:
            raise ValueError("Capability secret factory returned an invalid secret")
        if role == "agent":
            token = f"cdcap_a_{secret}"
        elif role == "human" and variant is not None:
            token = f"cdcap_h_{variant.value.lower()}_{secret}"
        else:  # pragma: no cover - private call shape is closed above
            raise AssertionError("Unsupported capability issue shape")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        issued_at = max(self._aware_now(), not_before)
        expires_at = issued_at + _TOKEN_TTL
        try:
            self._repository.issue_authority_capability(
                case_id=case_id,
                expected_case_version=expected_version,
                digest=digest,
                role=role,
                purpose=purpose,
                portal_variant=variant,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except CaseRecordNotFoundError:
            raise case_not_found() from None
        except CaseRecordVersionConflictError as error:
            raise state_conflict(current_version=error.current_version) from None
        return IssuedCapability(
            token=token,
            digest=digest,
            case_id=case_id,
            bound_case_version=expected_version,
            role=role,
            purpose=purpose,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def _require_case_version(self, case_id: str, expected_version: int) -> CaseRecord:
        if (
            type(expected_version) is not int
            or expected_version < 1
            or expected_version > _SQLITE_MAX_INTEGER
        ):
            raise state_conflict()
        try:
            current = self._repository.get_case(case_id)
        except ValueError:
            raise case_not_found() from None
        if current is None:
            raise case_not_found()
        if current.version != expected_version:
            raise state_conflict(current_version=current.version)
        return current

    @staticmethod
    def _digest(token: object) -> bytes | None:
        if type(token) is not str or len(token) > 80:
            return None
        if _AGENT_TOKEN.fullmatch(token) is None and _HUMAN_TOKEN.fullmatch(token) is None:
            return None
        return hashlib.sha256(token.encode("ascii")).digest()

    def _strictly_after(self, floor: datetime) -> datetime:
        value = self._aware_now()
        return value if value > floor else floor + timedelta(microseconds=1)

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.utcoffset() is None:
            raise ValueError("AuthorityService clock must return timezone-aware timestamps")
        return value
