"""Digest- and ownership-verified local transcript reads."""

import hashlib

from claimdone_api.media import CaseHandle, CaseMediaStore, StoredAssetRef
from claimdone_api.persistence import TranscriptRecord

from .errors import WorkflowDataIntegrityError
from .ports import CaseMediaOwnershipReader

MAX_TRANSCRIPT_TEXT_CHARACTERS = 4_000
MAX_TRANSCRIPT_TEXT_BYTES = 16_000


def validate_transcript_text(text: str, expected_sha256: str) -> str:
    """Verify the exact UTF-8 bytes that the confirmation view will display."""

    if type(text) is not str:
        raise WorkflowDataIntegrityError("Stored transcript text is invalid.")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise WorkflowDataIntegrityError("Stored transcript text is invalid.") from error
    if not encoded or len(encoded) > MAX_TRANSCRIPT_TEXT_BYTES:
        raise WorkflowDataIntegrityError("Stored transcript text is invalid.")
    if len(text) > MAX_TRANSCRIPT_TEXT_CHARACTERS or text != text.strip():
        # TranscriptConfirmationView strips boundary whitespace. Rejecting it here
        # prevents the displayed value from diverging from the bytes named by the hash.
        raise WorkflowDataIntegrityError("Stored transcript text is invalid.")
    if hashlib.sha256(encoded).hexdigest() != expected_sha256:
        raise WorkflowDataIntegrityError("Stored transcript text is invalid.")
    return text


class MediaTranscriptTextReader:
    """Read transcript bytes through the symlink-safe owned media store."""

    def __init__(
        self,
        ownership: CaseMediaOwnershipReader,
        store: CaseMediaStore,
    ) -> None:
        self._ownership = ownership
        self._store = store

    def read_verified_text(self, transcript: TranscriptRecord) -> str:
        storage_name = self._ownership.get_case_media_handle(transcript.case_id)
        if storage_name is None:
            raise WorkflowDataIntegrityError("Stored transcript text is unavailable.")
        asset = StoredAssetRef(
            file_id=transcript.local_ref,
            media_type="text/plain",
            sha256=transcript.transcript_sha256,
        )
        try:
            path = self._store.path_for(CaseHandle(storage_name=storage_name), asset)
            size = path.stat().st_size
            if size < 1 or size > MAX_TRANSCRIPT_TEXT_BYTES:
                raise WorkflowDataIntegrityError("Stored transcript text is invalid.")
            content = self._store.read_bytes(
                CaseHandle(storage_name=storage_name),
                asset,
            )
            text = content.decode("utf-8", errors="strict")
        except WorkflowDataIntegrityError:
            raise
        except (OSError, RuntimeError, UnicodeDecodeError, ValueError) as error:
            raise WorkflowDataIntegrityError(
                "Stored transcript text is unavailable."
            ) from error
        return validate_transcript_text(text, transcript.transcript_sha256)
