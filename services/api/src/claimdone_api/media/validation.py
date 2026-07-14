"""Pure deterministic G0 media validation and local EXIF inspection."""

import hashlib
import re
import warnings
import wave
from datetime import datetime
from fractions import Fraction
from io import BytesIO

from PIL import ExifTags, Image, UnidentifiedImageError

from claimdone_api.contracts import (
    GateId,
    GateReasonCode,
)
from claimdone_api.gates.registry import make_gate_decision

from .types import (
    ExifFieldSummary,
    ImageFormat,
    ImageUpload,
    IntakeGateResult,
    IntakeRequest,
    ValidatedAudio,
    ValidatedImage,
    ValidatedIntake,
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024
MAX_AUDIO_SECONDS = Fraction(60, 1)
PCM_WAV_MEDIA_TYPE = "audio/wav"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_MIME_BY_FORMAT = {
    ImageFormat.JPEG: "image/jpeg",
    ImageFormat.PNG: "image/png",
}
_FORMAT_BY_PILLOW = {item.value: item for item in ImageFormat}
_SENSITIVE_EXIF_TAGS = {
    "Artist",
    "BodySerialNumber",
    "CameraOwnerName",
    "Copyright",
    "GPSInfo",
    "ImageDescription",
    "LensSerialNumber",
    "UserComment",
    "XPAuthor",
    "XPComment",
    "XPKeywords",
    "XPSubject",
    "XPTitle",
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def validate_g0(
    request: IntakeRequest,
    *,
    decided_at: datetime | None = None,
) -> IntakeGateResult:
    """Validate every deterministic intake boundary before any local persistence."""

    reasons: set[GateReasonCode] = set()
    validated_images: list[ValidatedImage] = []
    if len(request.images) != 3:
        reasons.add(GateReasonCode.G0_IMAGE_COUNT_INVALID)
    else:
        for index, upload in enumerate(request.images, start=1):
            validated, image_reasons = _validate_image(upload, input_id=f"image-{index}")
            reasons.update(image_reasons)
            if validated is not None:
                validated_images.append(validated)

    normalized_text: str | None = None
    text_is_valid = request.text is None or type(request.text) is str
    if type(request.text) is str:
        normalized_text = request.text.strip() or None
        if (
            normalized_text is not None
            and len(normalized_text.encode("utf-8")) > MAX_TEXT_BYTES
        ):
            reasons.add(GateReasonCode.G0_INPUT_MODE_INVALID)
    audio_is_present = request.audio is not None
    if not text_is_valid or (normalized_text is None) is (not audio_is_present):
        reasons.add(GateReasonCode.G0_INPUT_MODE_INVALID)

    validated_audio: ValidatedAudio | None = None
    if audio_is_present:
        validated_audio, audio_invalid, audio_too_long = _validate_audio(request.audio)
        if audio_invalid:
            reasons.add(GateReasonCode.G0_INPUT_MODE_INVALID)
        if audio_too_long:
            reasons.add(GateReasonCode.G0_AUDIO_TOO_LONG)

    consent_values = (
        request.consents.sandbox_acknowledged,
        request.consents.image_rights_confirmed,
        request.consents.data_processing_approved,
    )
    if any(value is not True for value in consent_values):
        reasons.add(GateReasonCode.G0_CONSENT_MISSING)

    decision = make_gate_decision(
        GateId.G0_INTAKE,
        deterministic_reasons=tuple(reasons),
        decided_at=decided_at,
    )
    if reasons:
        return IntakeGateResult(decision=decision, validated=None)

    return IntakeGateResult(
        decision=decision,
        validated=ValidatedIntake(
            images=tuple(validated_images),
            normalized_text=normalized_text,
            audio=validated_audio,
        ),
    )


def _validate_image(
    upload: ImageUpload,
    *,
    input_id: str,
) -> tuple[ValidatedImage | None, set[GateReasonCode]]:
    reasons: set[GateReasonCode] = set()
    if type(upload.content) is not bytes or type(upload.media_type) is not str:
        return None, {GateReasonCode.G0_IMAGE_TYPE_INVALID}
    if len(upload.content) > MAX_IMAGE_BYTES:
        reasons.add(GateReasonCode.G0_IMAGE_TOO_LARGE)

    detected_format = _format_from_magic(upload.content)
    if detected_format is None or _MIME_BY_FORMAT[detected_format] != upload.media_type:
        reasons.add(GateReasonCode.G0_IMAGE_TYPE_INVALID)
    if reasons:
        return None, reasons
    assert detected_format is not None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(upload.content)) as candidate:
                pillow_format = candidate.format
                candidate.verify()
            with Image.open(BytesIO(upload.content)) as image:
                image.load()
                image_format = _FORMAT_BY_PILLOW.get(image.format or pillow_format or "")
                if image_format is not detected_format or image.width < 1 or image.height < 1:
                    raise ValueError("Decoded image format or dimensions do not match magic bytes")
                exif_summary = _summarize_exif(image.getexif())
                width, height = image.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None, {GateReasonCode.G0_IMAGE_TYPE_INVALID}
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError):
        return None, {GateReasonCode.G0_IMAGE_TYPE_INVALID}

    return (
        ValidatedImage(
            input_id=input_id,
            content=upload.content,
            media_type=upload.media_type,
            image_format=detected_format,
            sha256=hashlib.sha256(upload.content).hexdigest(),
            width=width,
            height=height,
            exif_summary=exif_summary,
        ),
        set(),
    )


def _validate_audio(
    upload: object,
) -> tuple[ValidatedAudio | None, bool, bool]:
    from .types import AudioUpload

    if not isinstance(upload, AudioUpload):
        return None, True, False
    if (
        type(upload.content) is not bytes
        or upload.media_type != PCM_WAV_MEDIA_TYPE
        or not upload.content.startswith(b"RIFF")
        or upload.content[8:12] != b"WAVE"
    ):
        return None, True, False
    try:
        with wave.open(BytesIO(upload.content), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            frame_rate = audio.getframerate()
            frame_count = audio.getnframes()
            compression = audio.getcomptype()
            frames = audio.readframes(frame_count)
    except (EOFError, wave.Error):
        return None, True, False
    if (
        compression != "NONE"
        or channels < 1
        or sample_width not in {1, 2, 3, 4}
        or frame_rate < 1
        or frame_count < 1
        or len(frames) != frame_count * channels * sample_width
    ):
        return None, True, False
    duration = Fraction(frame_count, frame_rate)
    return (
        ValidatedAudio(
            content=upload.content,
            media_type=upload.media_type,
            duration_seconds=duration,
            frame_count=frame_count,
            sample_rate=frame_rate,
        ),
        False,
        duration > MAX_AUDIO_SECONDS,
    )


def _format_from_magic(content: bytes) -> ImageFormat | None:
    if content.startswith(_PNG_MAGIC):
        return ImageFormat.PNG
    if content.startswith(_JPEG_MAGIC):
        return ImageFormat.JPEG
    return None


def _summarize_exif(exif: Image.Exif) -> tuple[ExifFieldSummary, ...]:
    result = []
    for tag_id, value in sorted(exif.items(), key=lambda item: item[0]):
        tag = str(ExifTags.TAGS.get(tag_id, f"Tag-{tag_id}"))
        sensitive = tag in _SENSITIVE_EXIF_TAGS
        result.append(
            ExifFieldSummary(
                tag=tag,
                display_value=_safe_exif_display(value, sensitive=sensitive),
                sensitive=sensitive,
            )
        )
    return tuple(result)


def _safe_exif_display(value: object, *, sensitive: bool) -> str:
    if sensitive:
        return "Sensitive metadata present (value hidden)"
    if isinstance(value, bytes):
        return f"Binary metadata ({len(value)} bytes)"
    if isinstance(value, str | int | float):
        display = str(value)
    elif isinstance(value, tuple) and len(value) <= 16:
        display = repr(value)
    else:
        display = f"<{type(value).__name__}>"
    return _CONTROL_CHARACTERS.sub(" ", display)[:160]
