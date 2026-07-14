"""Deterministic coverage for the G0/G1 media boundary."""

import os
import stat
import wave
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from claimdone_api.contracts import GateReasonCode
from claimdone_api.media import (
    MAX_IMAGE_BYTES,
    AudioUpload,
    AuditField,
    CaseHandle,
    CaseMediaStore,
    ExifChoice,
    ExifDecision,
    ImageUpload,
    IntakeConsents,
    IntakeRequest,
    PrivacyReview,
    StoredAssetRef,
    UnsafeStoragePath,
    prepare_g1,
    start_intake,
    store_transcript,
)
from claimdone_api.media.storage import AssetRole

DECIDED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)
ALL_CONSENTS = IntakeConsents(
    sandbox_acknowledged=True,
    image_rights_confirmed=True,
    data_processing_approved=True,
)


def image_bytes(image_format: str, *, artist: str | None = None) -> bytes:
    image = Image.new("RGB", (3, 2), color=(20, 120, 110))
    exif = Image.Exif()
    if artist is not None:
        exif[315] = artist
        exif[36867] = "2026:07:14 12:00:00"
    output = BytesIO()
    image.save(output, format=image_format, exif=exif)
    return output.getvalue()


def wav_bytes(*, seconds: int, frame_rate: int = 1) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(1)
        audio.setframerate(frame_rate)
        audio.writeframes(b"\x80" * seconds * frame_rate)
    return output.getvalue()


def valid_images() -> tuple[ImageUpload, ...]:
    return (
        ImageUpload(image_bytes("JPEG", artist="Private Person"), "image/jpeg"),
        ImageUpload(image_bytes("PNG", artist="Private Person"), "image/png"),
        ImageUpload(image_bytes("JPEG"), "image/jpeg"),
    )


def valid_request() -> IntakeRequest:
    return IntakeRequest(
        images=valid_images(),
        text="  A staged rear-end statement.  ",
        audio=None,
        consents=ALL_CONSENTS,
    )


def case_directories(store: CaseMediaStore) -> list[Path]:
    return sorted(path for path in store.root.iterdir() if path.name.startswith("case-"))


def full_review(session: object) -> PrivacyReview:
    from claimdone_api.media import IntakeSession

    assert isinstance(session, IntakeSession)
    return PrivacyReview(
        exif_choices=tuple(
            ExifChoice(input_id=image.input_id, decision=ExifDecision.STRIP)
            for image in session.images
        ),
        model_copy_approved=True,
        audit_fields=(),
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda request: replace(request, images=request.images[:2]),
            GateReasonCode.G0_IMAGE_COUNT_INVALID,
        ),
        (
            lambda request: replace(
                request,
                images=(ImageUpload(b"not-an-image", "image/jpeg"), *request.images[1:]),
            ),
            GateReasonCode.G0_IMAGE_TYPE_INVALID,
        ),
        (
            lambda request: replace(
                request,
                images=(
                    ImageUpload(request.images[0].content, "image/png"),
                    *request.images[1:],
                ),
            ),
            GateReasonCode.G0_IMAGE_TYPE_INVALID,
        ),
        (
            lambda request: replace(
                request,
                images=(
                    ImageUpload(
                        b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES,
                        "image/png",
                    ),
                    *request.images[1:],
                ),
            ),
            GateReasonCode.G0_IMAGE_TOO_LARGE,
        ),
        (
            lambda request: replace(
                request,
                audio=AudioUpload(wav_bytes(seconds=1), "audio/wav"),
            ),
            GateReasonCode.G0_INPUT_MODE_INVALID,
        ),
        (
            lambda request: replace(
                request,
                text=None,
                audio=AudioUpload(wav_bytes(seconds=61), "audio/wav"),
            ),
            GateReasonCode.G0_AUDIO_TOO_LONG,
        ),
        (
            lambda request: replace(
                request,
                consents=replace(request.consents, image_rights_confirmed=False),
            ),
            GateReasonCode.G0_CONSENT_MISSING,
        ),
    ],
)
def test_each_g0_reason_blocks_before_persistence(
    tmp_path: Path,
    mutate: object,
    reason: GateReasonCode,
) -> None:
    assert callable(mutate)
    store = CaseMediaStore(tmp_path / "media")
    result = start_intake(store, mutate(valid_request()), decided_at=DECIDED_AT)

    assert result.session is None
    assert not result.decision.passed
    assert reason in result.decision.reason_codes
    assert case_directories(store) == []


def test_g0_reason_order_is_fixed_when_multiple_inputs_fail(tmp_path: Path) -> None:
    request = replace(
        valid_request(),
        images=valid_images()[:2],
        text=None,
        consents=IntakeConsents(False, False, False),
    )

    result = start_intake(CaseMediaStore(tmp_path / "media"), request, decided_at=DECIDED_AT)

    assert result.decision.reason_codes == (
        GateReasonCode.G0_IMAGE_COUNT_INVALID,
        GateReasonCode.G0_INPUT_MODE_INVALID,
        GateReasonCode.G0_CONSENT_MISSING,
    )


def test_valid_text_and_exactly_sixty_second_pcm_wav_pass_g0(tmp_path: Path) -> None:
    text_result = start_intake(
        CaseMediaStore(tmp_path / "text"), valid_request(), decided_at=DECIDED_AT
    )
    audio_request = replace(
        valid_request(),
        text=None,
        audio=AudioUpload(wav_bytes(seconds=60), "audio/wav"),
    )
    audio_result = start_intake(
        CaseMediaStore(tmp_path / "audio"), audio_request, decided_at=DECIDED_AT
    )

    assert text_result.decision.passed and text_result.session is not None
    assert audio_result.decision.passed and audio_result.session is not None
    assert text_result.session.text is not None
    assert audio_result.session.audio is not None
    assert audio_result.session.audio_duration_seconds == 60


def test_empty_or_non_pcm_audio_is_rejected(tmp_path: Path) -> None:
    empty = replace(
        valid_request(),
        text=None,
        audio=AudioUpload(wav_bytes(seconds=0), "audio/wav"),
    )
    wrong_mime = replace(
        valid_request(),
        text=None,
        audio=AudioUpload(wav_bytes(seconds=1), "audio/mpeg"),
    )

    for index, request in enumerate((empty, wrong_mime)):
        result = start_intake(
            CaseMediaStore(tmp_path / f"media-{index}"), request, decided_at=DECIDED_AT
        )
        assert result.decision.reason_codes == (GateReasonCode.G0_INPUT_MODE_INVALID,)
        assert result.session is None


def test_exif_summary_hides_sensitive_values_and_no_model_exists_before_g1(
    tmp_path: Path,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    start = start_intake(store, valid_request(), decided_at=DECIDED_AT)
    assert start.session is not None

    artist = next(
        field
        for field in start.session.images[0].exif_summary
        if field.tag == "Artist"
    )
    assert artist.sensitive
    assert artist.display_value == "Sensitive metadata present (value hidden)"
    assert "Private Person" not in repr(start.session.images[0].exif_summary)
    assert not list((store.root / start.session.handle.storage_name).glob("model-*"))


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda review: replace(review, exif_choices=review.exif_choices[:2]),
            GateReasonCode.G1_EXIF_UNREVIEWED,
        ),
        (
            lambda review: replace(review, model_copy_approved=False),
            GateReasonCode.G1_MODEL_COPY_NOT_APPROVED,
        ),
        (
            lambda review: replace(
                review,
                audit_fields=(AuditField(key="claimant", value="Private Person"),),
            ),
            GateReasonCode.G1_SENSITIVE_LOG_DATA,
        ),
    ],
)
def test_each_g1_reason_blocks_without_creating_model_copies(
    tmp_path: Path,
    mutate: object,
    reason: GateReasonCode,
) -> None:
    assert callable(mutate)
    store = CaseMediaStore(tmp_path / "media")
    start = start_intake(store, valid_request(), decided_at=DECIDED_AT)
    assert start.session is not None

    result = prepare_g1(
        store,
        start.session,
        mutate(full_review(start.session)),
        decided_at=DECIDED_AT,
    )

    assert result.prepared is None
    assert reason in result.decision.reason_codes
    case_path = store.root / start.session.handle.storage_name
    assert not list(case_path.glob("model-*"))


def test_g1_reason_priority_is_fixed(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    start = start_intake(store, valid_request(), decided_at=DECIDED_AT)
    assert start.session is not None
    review = PrivacyReview(
        exif_choices=(),
        model_copy_approved=False,
        audit_fields=(AuditField(key="raw", value=b"image-bytes"),),
    )

    result = prepare_g1(store, start.session, review, decided_at=DECIDED_AT)

    assert result.decision.reason_codes == (
        GateReasonCode.G1_EXIF_UNREVIEWED,
        GateReasonCode.G1_MODEL_COPY_NOT_APPROVED,
        GateReasonCode.G1_SENSITIVE_LOG_DATA,
    )


def test_g1_creates_only_explicitly_stripped_or_retained_approved_copies(
    tmp_path: Path,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    start = start_intake(store, valid_request(), decided_at=DECIDED_AT)
    assert start.session is not None
    choices = tuple(
        ExifChoice(
            input_id=image.input_id,
            decision=ExifDecision.STRIP if index == 0 else ExifDecision.RETAIN,
        )
        for index, image in enumerate(start.session.images)
    )

    result = prepare_g1(
        store,
        start.session,
        PrivacyReview(choices, True, ()),
        decided_at=DECIDED_AT,
    )

    assert result.decision.passed and result.prepared is not None
    assert len(result.prepared.model_images) == 3
    stripped = result.prepared.model_images[0].path.read_bytes()
    retained = result.prepared.model_images[1].path.read_bytes()
    with Image.open(BytesIO(stripped)) as stripped_image:
        assert not stripped_image.getexif()
    with Image.open(BytesIO(retained)) as retained_image:
        assert retained_image.getexif().get(315) == "Private Person"
    assert retained == store.read_bytes(start.session.handle, start.session.images[1].source)
    assert result.prepared.safe_audit_summary.image_count == 3
    assert "Private Person" not in repr(result.prepared.safe_audit_summary)


def test_delete_case_removes_every_owned_media_role(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    request = replace(
        valid_request(),
        text=None,
        audio=AudioUpload(wav_bytes(seconds=1), "audio/wav"),
    )
    start = start_intake(store, request, decided_at=DECIDED_AT)
    assert start.session is not None
    prepared = prepare_g1(
        store, start.session, full_review(start.session), decided_at=DECIDED_AT
    )
    assert prepared.prepared is not None
    store_transcript(store, start.session, "Synthetic transcript")
    store.write_bytes(
        start.session.handle,
        b"temporary",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    case_path = store.root / start.session.handle.storage_name
    roles = {path.name.split("-", 1)[0] for path in case_path.iterdir()}
    assert {"source", "model", "audio", "transcript", "temp"} <= roles

    assert store.delete_case(start.session.handle)
    assert not case_path.exists()
    assert not store.delete_case(start.session.handle)


def test_reset_and_delete_do_not_follow_case_symlinks(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    first = start_intake(store, valid_request(), decided_at=DECIDED_AT)
    second = start_intake(store, valid_request(), decided_at=DECIDED_AT)
    assert first.session is not None and second.session is not None
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    fake_name = f"case-{'f' * 32}"
    (store.root / fake_name).symlink_to(outside, target_is_directory=True)
    unrelated = store.root / "unrelated"
    unrelated.mkdir()

    assert store.delete_case(CaseHandle(fake_name))
    assert sentinel.read_text(encoding="utf-8") == "keep"
    (store.root / fake_name).symlink_to(outside, target_is_directory=True)
    assert store.reset() == 3

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert unrelated.is_dir()
    assert case_directories(store) == []


def test_storage_rejects_unowned_roots_traversal_and_asset_symlinks(tmp_path: Path) -> None:
    unowned = tmp_path / "unowned"
    unowned.mkdir()
    (unowned / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(UnsafeStoragePath):
        CaseMediaStore(unowned)

    actual_root = tmp_path / "actual"
    CaseMediaStore(actual_root)
    root_link = tmp_path / "root-link"
    root_link.symlink_to(actual_root, target_is_directory=True)
    with pytest.raises(UnsafeStoragePath):
        CaseMediaStore(root_link)

    store = CaseMediaStore(tmp_path / "media")
    with pytest.raises(UnsafeStoragePath):
        store.delete_case(CaseHandle("../outside"))
    handle = store.create_case()
    with pytest.raises(UnsafeStoragePath):
        store.write_bytes(
            handle,
            b"escape",
            role=cast(AssetRole, "../escape"),
            suffix=".bin",
            media_type="application/octet-stream",
        )
    outside = tmp_path / "external.bin"
    outside.write_bytes(b"external")
    file_id = f"source-{'a' * 32}.bin"
    (store.root / handle.storage_name / file_id).symlink_to(outside)
    ref = StoredAssetRef(file_id, "application/octet-stream", "0" * 64)
    with pytest.raises(UnsafeStoragePath):
        store.path_for(handle, ref)
    assert outside.read_bytes() == b"external"


def test_storage_uses_private_permissions_and_detects_tampering(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    start = start_intake(store, valid_request(), decided_at=DECIDED_AT)
    assert start.session is not None and start.session.text is not None
    case_path = store.root / start.session.handle.storage_name
    text_path = store.path_for(start.session.handle, start.session.text)
    assert stat.S_IMODE(case_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(text_path.stat().st_mode) == 0o600
    text_path.write_bytes(b"tampered")

    from claimdone_api.media import MediaStorageError

    with pytest.raises(MediaStorageError, match="digest"):
        store.read_bytes(start.session.handle, start.session.text)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_case_paths_are_random_and_ignore_user_content(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    request = replace(valid_request(), text="../../private claimant.txt")
    start = start_intake(store, request, decided_at=DECIDED_AT)
    assert start.session is not None

    names = [path.name for path in (store.root / start.session.handle.storage_name).iterdir()]
    assert all(
        "private" not in name and "claimant" not in name and ".." not in name
        for name in names
    )
