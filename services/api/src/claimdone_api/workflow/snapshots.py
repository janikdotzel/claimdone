"""Canonical, fail-closed WorkflowSnapshot assembly."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from pydantic import ValidationError

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    CaseState,
    ClarificationView,
    PortalSessionView,
    TranscriptConfirmationView,
    VerificationAttemptSeries,
    WorkflowCaseView,
    WorkflowSnapshot,
)
from claimdone_api.persistence import (
    CaseRecord,
    SandboxReceiptRecord,
    TranscriptRecord,
)

from .errors import (
    WorkflowCaseNotFoundError,
    WorkflowDataIntegrityError,
    WorkflowVersionChurnError,
)
from .ports import (
    PortalSessionReader,
    TranscriptTextReader,
    VerificationAttemptReader,
    WorkflowReadRepository,
)
from .transcript import validate_transcript_text

RequestIdFactory = Callable[[], str]
_PORTAL_READ_STATES = frozenset(
    {
        CaseState.READY_TO_FILL,
        CaseState.FILLING,
        CaseState.VERIFYING,
        CaseState.REVIEW,
        CaseState.BLOCKED,
        CaseState.EMERGENCY_STOPPED,
        CaseState.ABANDONED,
        CaseState.FAILED,
    }
)
_VERIFICATION_READ_STATES = frozenset(
    {
        CaseState.VERIFYING,
        CaseState.REVIEW,
        CaseState.BLOCKED,
        CaseState.EMERGENCY_STOPPED,
        CaseState.ABANDONED,
        CaseState.FAILED,
    }
)


def _request_id() -> str:
    return f"request-{uuid4().hex}"


class SnapshotAssembler:
    """Join persisted truth and narrow read projections under one case version."""

    def __init__(
        self,
        repository: WorkflowReadRepository,
        *,
        transcript_reader: TranscriptTextReader | None = None,
        portal_reader: PortalSessionReader | None = None,
        verification_reader: VerificationAttemptReader | None = None,
        request_id_factory: RequestIdFactory = _request_id,
    ) -> None:
        self._repository = repository
        self._transcript_reader = transcript_reader
        self._portal_reader = portal_reader
        self._verification_reader = verification_reader
        self._request_id_factory = request_id_factory

    def assemble(self, case_id: str) -> WorkflowSnapshot:
        """Build a stable snapshot, retrying one concurrent case-version change."""

        current_version: int | None = None
        for _attempt in range(2):
            before = self._get_case(case_id)
            projections = self._read_projections(before)
            after = self._repository.get_case(case_id)
            if after is None:
                current_version = None
                continue
            if after.case_id != case_id:
                raise WorkflowDataIntegrityError("Workflow case data is invalid.")
            current_version = after.version
            if before.version != after.version:
                continue
            if before != after:
                raise WorkflowDataIntegrityError("Workflow case data is invalid.")
            return self._validate_snapshot(before, projections)
        raise WorkflowVersionChurnError(current_version)

    def _get_case(self, case_id: str) -> CaseRecord:
        record = self._repository.get_case(case_id)
        if record is None:
            raise WorkflowCaseNotFoundError("The workflow case does not exist.")
        if record.case_id != case_id:
            raise WorkflowDataIntegrityError("Workflow case data is invalid.")
        return record

    def _read_projections(self, record: CaseRecord) -> "_SnapshotProjections":
        transcript = self._transcript_view(record)
        clarification = self._clarification_view(record)
        portal = None
        if record.state in _PORTAL_READ_STATES and self._portal_reader is not None:
            portal = self._portal_reader.get_portal_session(record.case_id)
        verification = None
        if (
            record.state in _VERIFICATION_READ_STATES
            and self._verification_reader is not None
        ):
            verification = self._verification_reader.get_verification_attempts(
                record.case_id
            )
        receipt_record = self._repository.get_sandbox_receipt(record.case_id)
        return _SnapshotProjections(
            transcript=transcript,
            clarification=clarification,
            portal=portal,
            verification=verification,
            receipt_record=receipt_record,
        )

    def _transcript_view(
        self,
        record: CaseRecord,
    ) -> TranscriptConfirmationView | None:
        if record.state is not CaseState.AWAITING_TRANSCRIPT_CONFIRMATION:
            return None
        transcript = self._repository.get_transcript(record.case_id)
        if transcript is None or self._transcript_reader is None:
            raise WorkflowDataIntegrityError("Workflow transcript data is invalid.")
        self._validate_transcript_metadata(record, transcript)
        try:
            text = self._transcript_reader.read_verified_text(transcript)
            validate_transcript_text(text, transcript.transcript_sha256)
            view = TranscriptConfirmationView.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "caseId": record.case_id,
                    "transcriptId": transcript.transcript_id,
                    "transcriptSha256": transcript.transcript_sha256,
                    "text": text,
                    "version": record.version,
                    "confirmed": False,
                }
            )
            if view.text != text:
                raise ValueError("Transcript text changed at the contract boundary")
            return view
        except (ValidationError, ValueError, TypeError) as error:
            raise WorkflowDataIntegrityError("Workflow transcript data is invalid.") from error

    @staticmethod
    def _validate_transcript_metadata(
        record: CaseRecord,
        transcript: TranscriptRecord,
    ) -> None:
        digest_valid = (
            re.fullmatch(r"[a-f0-9]{64}", transcript.transcript_sha256) is not None
        )
        summary = record.snapshot.intake_summary
        statement = None if summary is None else summary.get("statement")
        audio = None if summary is None else summary.get("audio")
        text = None if summary is None else summary.get("text")
        if not isinstance(statement, dict) or not isinstance(audio, dict) or text is not None:
            raise WorkflowDataIntegrityError("Workflow transcript data is invalid.")
        local_ref = statement.get("fileId")
        summary_digest = statement.get("sha256")
        audio_ref = audio.get("fileId")
        audio_digest = audio.get("sha256")
        derived_identity = hashlib.sha256(
            (
                "claimdone-transcript-v1\0"
                f"{record.case_id}\0{transcript.local_ref}\0"
                f"{transcript.transcript_sha256}"
            ).encode()
        ).hexdigest()
        derived_transcript_id = f"transcript-{derived_identity[:32]}"
        if (
            transcript.case_id != record.case_id
            or transcript.bound_case_version != record.version
            or transcript.version != 1
            or transcript.confirmed
            or transcript.confirmed_at is not None
            or not digest_valid
            or transcript.created_at < record.created_at
            or transcript.created_at > record.updated_at
            or re.fullmatch(r"transcript-[a-f0-9]{32}\.txt", transcript.local_ref)
            is None
            or local_ref != transcript.local_ref
            or statement.get("mediaType") != "text/plain"
            or summary_digest != transcript.transcript_sha256
            or transcript.transcript_id != derived_transcript_id
            or not isinstance(audio_ref, str)
            or re.fullmatch(r"audio-[a-f0-9]{32}\.wav", audio_ref) is None
            or audio.get("mediaType") != "audio/wav"
            or not isinstance(audio_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", audio_digest) is None
        ):
            raise WorkflowDataIntegrityError("Workflow transcript data is invalid.")

    @staticmethod
    def _clarification_view(record: CaseRecord) -> ClarificationView | None:
        raw = record.snapshot.active_clarification
        if raw is None:
            return None
        try:
            return ClarificationView.model_validate(raw)
        except (ValidationError, ValueError, TypeError) as error:
            raise WorkflowDataIntegrityError(
                "Workflow clarification data is invalid."
            ) from error

    def _validate_snapshot(
        self,
        record: CaseRecord,
        projections: "_SnapshotProjections",
    ) -> WorkflowSnapshot:
        try:
            case = WorkflowCaseView.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "caseId": record.case_id,
                    "state": record.state,
                    "version": record.version,
                    "createdAt": record.created_at,
                    "updatedAt": record.updated_at,
                }
            )
            receipt = (
                None
                if projections.receipt_record is None
                else projections.receipt_record.receipt
            )
            return WorkflowSnapshot.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "requestId": self._request_id_factory(),
                    "case": case,
                    "claimPacket": record.snapshot.claim_packet,
                    "transcriptConfirmation": projections.transcript,
                    "clarification": projections.clarification,
                    "portalSession": projections.portal,
                    "verificationAttempts": projections.verification,
                    "receipt": receipt,
                }
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise WorkflowDataIntegrityError("Workflow snapshot data is invalid.") from error


@dataclass(frozen=True, slots=True)
class _SnapshotProjections:
    """Internal immutable projection bundle kept out of the public contract."""

    transcript: TranscriptConfirmationView | None
    clarification: ClarificationView | None
    portal: PortalSessionView | None
    verification: VerificationAttemptSeries | None
    receipt_record: SandboxReceiptRecord | None


__all__ = ["SnapshotAssembler"]
