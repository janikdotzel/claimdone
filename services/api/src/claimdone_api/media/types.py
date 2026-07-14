"""Immutable values used by the local media intake services."""

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path

from claimdone_api.contracts import GateDecision


class ImageFormat(StrEnum):
    JPEG = "JPEG"
    PNG = "PNG"


class ExifDecision(StrEnum):
    STRIP = "strip"
    RETAIN = "retain"


@dataclass(frozen=True, slots=True)
class ImageUpload:
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class AudioUpload:
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class IntakeConsents:
    sandbox_acknowledged: bool
    image_rights_confirmed: bool
    data_processing_approved: bool


@dataclass(frozen=True, slots=True)
class IntakeRequest:
    images: tuple[ImageUpload, ...]
    text: str | None
    audio: AudioUpload | None
    consents: IntakeConsents


@dataclass(frozen=True, slots=True)
class ExifFieldSummary:
    tag: str
    display_value: str
    sensitive: bool


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    input_id: str
    content: bytes
    media_type: str
    image_format: ImageFormat
    sha256: str
    width: int
    height: int
    exif_summary: tuple[ExifFieldSummary, ...]


@dataclass(frozen=True, slots=True)
class ValidatedAudio:
    content: bytes
    media_type: str
    duration_seconds: Fraction
    frame_count: int
    sample_rate: int


@dataclass(frozen=True, slots=True)
class ValidatedIntake:
    images: tuple[ValidatedImage, ...]
    normalized_text: str | None
    audio: ValidatedAudio | None


@dataclass(frozen=True, slots=True)
class IntakeGateResult:
    decision: GateDecision
    validated: ValidatedIntake | None


@dataclass(frozen=True, slots=True)
class CaseHandle:
    storage_name: str


@dataclass(frozen=True, slots=True)
class StoredAssetRef:
    file_id: str
    media_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredImage:
    input_id: str
    source: StoredAssetRef
    image_format: ImageFormat
    exif_summary: tuple[ExifFieldSummary, ...]


@dataclass(frozen=True, slots=True)
class IntakeSession:
    handle: CaseHandle
    images: tuple[StoredImage, ...]
    text: StoredAssetRef | None
    audio: StoredAssetRef | None
    audio_duration_seconds: Fraction | None


@dataclass(frozen=True, slots=True)
class IntakeStartResult:
    decision: GateDecision
    session: IntakeSession | None


@dataclass(frozen=True, slots=True)
class ExifChoice:
    input_id: str
    decision: ExifDecision


@dataclass(frozen=True, slots=True)
class AuditField:
    key: str
    value: object


@dataclass(frozen=True, slots=True)
class PrivacyReview:
    exif_choices: tuple[ExifChoice, ...]
    model_copy_approved: bool
    audit_fields: tuple[AuditField, ...]


@dataclass(frozen=True, slots=True)
class SafeAuditSummary:
    image_count: int
    image_media_types: tuple[str, ...]
    has_audio: bool
    exif_decisions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelAsset:
    local_ref: str
    path: Path
    media_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    handle: CaseHandle
    model_images: tuple[ModelAsset, ...]
    text: str | None
    audio: ModelAsset | None
    safe_audit_summary: SafeAuditSummary


@dataclass(frozen=True, slots=True)
class PrivacyResult:
    decision: GateDecision
    prepared: PreparedMedia | None
