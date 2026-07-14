"""Deterministic G0/G1 orchestration for temporary, local-only media."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from PIL import Image

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    GateDecision,
    GateId,
    GateReasonCode,
)

from .storage import CaseMediaStore
from .types import (
    CaseHandle,
    ExifChoice,
    ExifDecision,
    IntakeRequest,
    IntakeSession,
    IntakeStartResult,
    ModelAsset,
    PreparedMedia,
    PrivacyResult,
    PrivacyReview,
    SafeAuditSummary,
    StoredAssetRef,
    StoredImage,
    ValidatedImage,
)
from .validation import validate_g0

_G1_REASON_ORDER = (
    GateReasonCode.G1_EXIF_UNREVIEWED,
    GateReasonCode.G1_MODEL_COPY_NOT_APPROVED,
    GateReasonCode.G1_SENSITIVE_LOG_DATA,
)


class MediaPreparationError(RuntimeError):
    """Raised when already-validated local media cannot be normalized safely."""


def start_intake(
    store: CaseMediaStore,
    request: IntakeRequest,
    *,
    decided_at: datetime | None = None,
) -> IntakeStartResult:
    """Run G0 before creating any case directory or persistent media artifact."""

    gate_result = validate_g0(request, decided_at=decided_at)
    if not gate_result.decision.passed or gate_result.validated is None:
        return IntakeStartResult(decision=gate_result.decision, session=None)

    validated = gate_result.validated
    handle = store.create_case()
    try:
        images = tuple(_store_source_image(store, handle, image) for image in validated.images)
        text_ref = (
            store.write_bytes(
                handle,
                validated.normalized_text.encode("utf-8"),
                role="text",
                suffix=".txt",
                media_type="text/plain",
            )
            if validated.normalized_text is not None
            else None
        )
        audio_ref = (
            store.write_bytes(
                handle,
                validated.audio.content,
                role="audio",
                suffix=".wav",
                media_type=validated.audio.media_type,
            )
            if validated.audio is not None
            else None
        )
    except Exception:
        store.delete_case(handle)
        raise

    return IntakeStartResult(
        decision=gate_result.decision,
        session=IntakeSession(
            handle=handle,
            images=images,
            text=text_ref,
            audio=audio_ref,
            audio_duration_seconds=(
                validated.audio.duration_seconds if validated.audio is not None else None
            ),
        ),
    )


def prepare_g1(
    store: CaseMediaStore,
    session: IntakeSession,
    review: PrivacyReview,
    *,
    decided_at: datetime | None = None,
) -> PrivacyResult:
    """Apply explicit EXIF choices and expose model paths only after G1 passes."""

    reasons: set[GateReasonCode] = set()
    expected_ids = tuple(image.input_id for image in session.images)
    choices = tuple(review.exif_choices)
    choices_are_typed = all(isinstance(choice, ExifChoice) for choice in choices)
    supplied_ids = tuple(
        choice.input_id for choice in choices if isinstance(choice, ExifChoice)
    )
    if (
        len(expected_ids) != 3
        or len(set(expected_ids)) != len(expected_ids)
        or not choices_are_typed
        or len(supplied_ids) != len(expected_ids)
        or len(set(supplied_ids)) != len(supplied_ids)
        or set(supplied_ids) != set(expected_ids)
        or any(not isinstance(choice.decision, ExifDecision) for choice in choices)
    ):
        reasons.add(GateReasonCode.G1_EXIF_UNREVIEWED)
    if review.model_copy_approved is not True:
        reasons.add(GateReasonCode.G1_MODEL_COPY_NOT_APPROVED)
    # Caller-provided event fields are never trusted. The pipeline emits its own
    # fixed, value-free SafeAuditSummary after approval.
    if review.audit_fields:
        reasons.add(GateReasonCode.G1_SENSITIVE_LOG_DATA)

    ordered_reasons = tuple(reason for reason in _G1_REASON_ORDER if reason in reasons)
    decision = _gate_decision(GateId.G1_PRIVACY, ordered_reasons, decided_at=decided_at)
    if ordered_reasons:
        return PrivacyResult(decision=decision, prepared=None)

    choice_by_id = {choice.input_id: choice.decision for choice in choices}
    normalized: list[tuple[StoredImage, bytes]] = []
    for image in session.images:
        source = store.read_bytes(session.handle, image.source)
        if choice_by_id[image.input_id] is ExifDecision.STRIP:
            source = _strip_image_metadata(source, image.image_format.value)
        normalized.append((image, source))

    text = (
        store.read_bytes(session.handle, session.text).decode("utf-8")
        if session.text is not None
        else None
    )
    audio = (
        ModelAsset(
            local_ref=session.audio.file_id,
            path=store.path_for(session.handle, session.audio),
            media_type=session.audio.media_type,
            sha256=session.audio.sha256,
        )
        if session.audio is not None
        else None
    )

    written: list[StoredAssetRef] = []
    try:
        model_images: list[ModelAsset] = []
        for image, content in normalized:
            suffix = ".jpg" if image.image_format.value == "JPEG" else ".png"
            ref = store.write_bytes(
                session.handle,
                content,
                role="model",
                suffix=suffix,
                media_type=image.source.media_type,
            )
            written.append(ref)
            model_images.append(
                ModelAsset(
                    local_ref=ref.file_id,
                    path=store.path_for(session.handle, ref),
                    media_type=ref.media_type,
                    sha256=ref.sha256,
                )
            )
    except Exception:
        for ref in written:
            store.delete_asset(session.handle, ref)
        raise

    ordered_choices = tuple(choice_by_id[input_id].value for input_id in expected_ids)
    return PrivacyResult(
        decision=decision,
        prepared=PreparedMedia(
            handle=session.handle,
            model_images=tuple(model_images),
            text=text,
            audio=audio,
            safe_audit_summary=SafeAuditSummary(
                image_count=len(model_images),
                image_media_types=tuple(asset.media_type for asset in model_images),
                has_audio=audio is not None,
                exif_decisions=ordered_choices,
            ),
        ),
    )


def store_transcript(
    store: CaseMediaStore,
    session: IntakeSession,
    transcript: str,
) -> StoredAssetRef:
    """Store a local transcript under the case so case deletion removes it too."""

    if type(transcript) is not str or not transcript.strip():
        raise ValueError("Transcript must be non-empty text")
    return store.write_bytes(
        session.handle,
        transcript.strip().encode("utf-8"),
        role="transcript",
        suffix=".txt",
        media_type="text/plain",
    )


def _store_source_image(
    store: CaseMediaStore,
    handle: CaseHandle,
    image: ValidatedImage,
) -> StoredImage:
    suffix = ".jpg" if image.image_format.value == "JPEG" else ".png"
    source = store.write_bytes(
        handle,
        image.content,
        role="source",
        suffix=suffix,
        media_type=image.media_type,
    )
    return StoredImage(
        input_id=image.input_id,
        source=source,
        image_format=image.image_format,
        exif_summary=image.exif_summary,
    )


def _strip_image_metadata(content: bytes, image_format: str) -> bytes:
    try:
        with Image.open(BytesIO(content)) as source:
            source.load()
            clean = source.copy()
            clean.info.clear()
            clean.getexif().clear()
            if image_format == "JPEG" and clean.mode not in {"L", "RGB", "CMYK"}:
                clean = clean.convert("RGB")
            output = BytesIO()
            if image_format == "JPEG":
                clean.save(output, format="JPEG", exif=b"")
            elif image_format == "PNG":
                clean.save(output, format="PNG", exif=b"")
            else:
                raise MediaPreparationError("Unsupported validated image format")
    except (OSError, ValueError) as error:
        raise MediaPreparationError("Validated image could not be normalized") from error
    normalized = output.getvalue()
    try:
        with Image.open(BytesIO(normalized)) as verification:
            verification.verify()
        with Image.open(BytesIO(normalized)) as verification:
            if verification.getexif():
                raise MediaPreparationError("Normalized model copy still contains EXIF")
    except OSError as error:
        raise MediaPreparationError("Normalized image verification failed") from error
    return normalized


def _gate_decision(
    gate_id: GateId,
    reasons: tuple[GateReasonCode, ...],
    *,
    decided_at: datetime | None,
) -> GateDecision:
    timestamp = decided_at or datetime.now(UTC)
    if timestamp.utcoffset() is None:
        raise ValueError("Gate decision timestamp must be timezone-aware")
    passed = not reasons
    return GateDecision.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "gateId": gate_id.value,
            "deterministicPassed": passed,
            "modelBlocked": False,
            "passed": passed,
            "reasonCodes": [reason.value for reason in reasons],
            "evidenceRefs": [],
            "decidedAt": timestamp,
        }
    )
