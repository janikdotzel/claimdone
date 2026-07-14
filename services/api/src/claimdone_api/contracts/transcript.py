"""Fail-closed transcript confirmation contracts."""

from typing import Annotated

from pydantic import Field

from .base import (
    AlwaysFalse,
    AlwaysTrue,
    ContractModel,
    ContractVersion,
    Identifier,
    NonEmptyText,
    Sha256Digest,
    StrictInteger,
)


class TranscriptConfirmationView(ContractModel):
    """Exact transcript content shown to a human before it may become evidence."""

    contract_version: ContractVersion
    case_id: Identifier
    transcript_id: Identifier
    transcript_sha256: Sha256Digest
    text: NonEmptyText
    version: Annotated[StrictInteger, Field(ge=1)]
    confirmed: AlwaysFalse


class TranscriptConfirmationRequest(ContractModel):
    """Human confirmation bound to the displayed transcript bytes and version."""

    contract_version: ContractVersion
    case_id: Identifier
    transcript_id: Identifier
    transcript_sha256: Sha256Digest
    expected_version: Annotated[StrictInteger, Field(ge=1)]
    confirmed: AlwaysTrue
