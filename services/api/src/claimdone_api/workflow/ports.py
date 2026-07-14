"""Narrow read ports used by the workflow HTTP layer."""

from typing import Protocol

from claimdone_api.contracts import PortalSessionView, VerificationAttemptSeries
from claimdone_api.persistence import (
    CaseRecord,
    SandboxReceiptRecord,
    SequencedWorkflowEvent,
    TranscriptRecord,
)


class WorkflowReadRepository(Protocol):
    """Persisted truth needed by snapshots and replay, without mutation methods."""

    def get_case(self, case_id: str) -> CaseRecord | None:
        """Return the current immutable case record, if it exists."""

    def get_transcript(self, case_id: str) -> TranscriptRecord | None:
        """Return content-free transcript metadata for a case."""

    def get_sandbox_receipt(self, case_id: str) -> SandboxReceiptRecord | None:
        """Return the persisted redacted sandbox receipt, if one exists."""

    def list_workflow_events(
        self,
        case_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SequencedWorkflowEvent, ...]:
        """Read redacted event projections after the database-owned cursor."""


class TranscriptTextReader(Protocol):
    """Read locally owned transcript text bound to immutable metadata."""

    def read_verified_text(self, transcript: TranscriptRecord) -> str:
        """Return UTF-8 text only after ownership, size, and digest checks."""


class PortalSessionReader(Protocol):
    """Read the current local-sandbox portal projection for a case."""

    def get_portal_session(self, case_id: str) -> PortalSessionView | None:
        """Return a closed portal view or no projection."""


class VerificationAttemptReader(Protocol):
    """Read the immutable verification attempt chain for a case."""

    def get_verification_attempts(
        self,
        case_id: str,
    ) -> VerificationAttemptSeries | None:
        """Return a closed attempt series or no completed series."""


class CaseMediaOwnershipReader(Protocol):
    """Resolve an opaque media directory without deriving it from a case ID."""

    def get_case_media_handle(self, case_id: str) -> str | None:
        """Return the normalized opaque storage handle for a case."""
