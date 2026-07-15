"""Deterministic coverage for the G0/G1 media boundary."""

import os
import secrets
import stat
import threading
import wave
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

import claimdone_api.media.storage as media_storage
import claimdone_api.media.validation as media_validation
from claimdone_api.contracts import GateReasonCode
from claimdone_api.media import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_HEIGHT,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_WIDTH,
    MAX_TEXT_BYTES,
    AudioUpload,
    AuditField,
    CaseHandle,
    CaseMediaStore,
    ExifChoice,
    ExifDecision,
    ImageUpload,
    IntakeConsents,
    IntakeRequest,
    MediaStorageError,
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


def png_with_header_dimensions(width: int, height: int) -> bytes:
    """Change only IHDR dimensions; never allocate the represented pixel buffer."""

    content = bytearray(image_bytes("PNG"))
    assert content[12:16] == b"IHDR"
    content[16:20] = width.to_bytes(4, "big")
    content[20:24] = height.to_bytes(4, "big")
    content[29:33] = (zlib.crc32(content[12:29]) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(content)


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


def add_sanitized_tombstones(parent: Path, count: int) -> tuple[Path, ...]:
    paths = tuple(
        parent / f".claimdone-delete-{index:032x}"
        for index in range(count)
    )
    for path in paths:
        path.write_bytes(b"")
    return paths


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


@pytest.mark.parametrize(
    ("width", "height"),
    (
        (MAX_IMAGE_WIDTH + 1, 1),
        (1, MAX_IMAGE_HEIGHT + 1),
        (5_000, MAX_IMAGE_PIXELS // 5_000 + 1),
    ),
)
def test_header_dimension_and_pixel_bombs_block_before_pillow_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    height: int,
) -> None:
    request = valid_request()
    malicious_size = (width, height)
    request = replace(
        request,
        images=(
            ImageUpload(png_with_header_dimensions(width, height), "image/png"),
            *request.images[1:],
        ),
    )
    original_load = Image.Image.load

    def guarded_load(image: Image.Image, *args: Any, **kwargs: Any) -> Any:
        if image.size == malicious_size:
            raise AssertionError("oversized image reached Pillow decompression")
        return original_load(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", guarded_load)
    store = CaseMediaStore(tmp_path / "media")

    result = start_intake(store, request, decided_at=DECIDED_AT)

    assert result.session is None
    assert result.decision.reason_codes == (GateReasonCode.G0_IMAGE_TOO_LARGE,)
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


def test_wrong_image_count_never_decodes_but_keeps_independent_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_image_validation(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"image validation was called: {args!r} {kwargs!r}")

    monkeypatch.setattr(media_validation, "_validate_image", forbidden_image_validation)
    request = IntakeRequest(
        images=(
            ImageUpload(b"must-not-be-read-1", "image/jpeg"),
            ImageUpload(b"must-not-be-read-2", "image/png"),
        ),
        text="Text and audio are deliberately both present.",
        audio=AudioUpload(wav_bytes(seconds=61), "audio/wav"),
        consents=IntakeConsents(False, False, False),
    )
    store = CaseMediaStore(tmp_path / "media")

    result = start_intake(store, request, decided_at=DECIDED_AT)

    assert result.session is None
    assert result.decision.reason_codes == (
        GateReasonCode.G0_IMAGE_COUNT_INVALID,
        GateReasonCode.G0_INPUT_MODE_INVALID,
        GateReasonCode.G0_AUDIO_TOO_LONG,
        GateReasonCode.G0_CONSENT_MISSING,
    )
    assert case_directories(store) == []


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


def test_text_statement_has_a_deterministic_utf8_byte_limit(tmp_path: Path) -> None:
    request = replace(valid_request(), text="ü" * (MAX_TEXT_BYTES // 2 + 1))

    result = start_intake(
        CaseMediaStore(tmp_path / "media"),
        request,
        decided_at=DECIDED_AT,
    )

    assert result.session is None
    assert result.decision.reason_codes == (GateReasonCode.G0_INPUT_MODE_INVALID,)


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

    with pytest.raises(UnsafeStoragePath, match="authority-marked"):
        store.delete_case(CaseHandle(fake_name))
    assert sentinel.read_text(encoding="utf-8") == "keep"
    with pytest.raises(UnsafeStoragePath, match="authority-marked"):
        store.reset()
    (store.root / fake_name).unlink()
    assert store.reset() == 2

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert unrelated.is_dir()
    assert case_directories(store) == []


@pytest.mark.parametrize(
    "later_failure",
    (
        "fifo",
        "asset_hardlink",
        "marker_hardlink",
        "not_writable",
        "nested_fifo",
    ),
)
def test_reset_preflights_every_later_case_before_mutating_any_case(
    tmp_path: Path,
    later_failure: str,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handles = tuple(
        sorted(
            (store.create_case(), store.create_case()),
            key=lambda handle: handle.storage_name,
        )
    )
    refs = tuple(
        store.write_bytes(
            handle,
            f"owned-{index}".encode(),
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )
        for index, handle in enumerate(handles)
    )
    case_paths = tuple(store.root / handle.storage_name for handle in handles)
    later_path = case_paths[1]
    if later_failure == "fifo":
        os.mkfifo(later_path / "blocked.fifo")
    elif later_failure == "asset_hardlink":
        os.link(later_path / refs[1].file_id, tmp_path / "outside-asset.bin")
    elif later_failure == "marker_hardlink":
        os.link(later_path / ".claimdone-case-v2", tmp_path / "outside-marker")
    elif later_failure == "not_writable":
        (later_path / refs[1].file_id).chmod(0o400)
    else:
        assert later_failure == "nested_fifo"
        nested = later_path / "nested"
        nested.mkdir()
        os.mkfifo(nested / "blocked.fifo")

    with pytest.raises(UnsafeStoragePath):
        store.reset()

    assert tuple(
        (case_path / ref.file_id).read_bytes()
        for case_path, ref in zip(case_paths, refs, strict=True)
    ) == (b"owned-0", b"owned-1")
    assert all((case_path / ".claimdone-case-v2").is_file() for case_path in case_paths)
    assert all(
        not any(path.name.startswith(".claimdone-delete-") for path in case_path.iterdir())
        for case_path in case_paths
    )


@pytest.mark.parametrize(
    "injection",
    ("root_marker", "root_tombstone", "case_entry"),
)
def test_reset_revalidates_complete_plan_before_first_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injection: str,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handles = tuple(
        sorted(
            (store.create_case(), store.create_case()),
            key=lambda handle: handle.storage_name,
        )
    )
    refs = tuple(
        store.write_bytes(
            handle,
            f"owned-{index}".encode(),
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )
        for index, handle in enumerate(handles)
    )
    original = CaseMediaStore._revalidate_root_deletion_plan
    injected = False

    def inject_before_revalidation(
        selected_store: CaseMediaStore,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            if injection == "root_marker":
                marker = selected_store.root / ".claimdone-media-root-v2"
                content = marker.read_bytes()
                marker.unlink()
                marker.write_bytes(content)
            elif injection == "root_tombstone":
                add_sanitized_tombstones(selected_store.root, 1)
            else:
                assert injection == "case_entry"
                later = selected_store.root / handles[1].storage_name
                (later / "injected.bin").write_bytes(b"injected")
        original(selected_store, *args, **kwargs)

    monkeypatch.setattr(
        CaseMediaStore,
        "_revalidate_root_deletion_plan",
        inject_before_revalidation,
    )

    with pytest.raises(UnsafeStoragePath):
        store.reset()

    assert tuple(
        (store.root / handle.storage_name / ref.file_id).read_bytes()
        for handle, ref in zip(handles, refs, strict=True)
    ) == (b"owned-0", b"owned-1")


def test_reset_root_tombstone_capacity_allows_exact_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handles = (store.create_case(), store.create_case())
    for handle in handles:
        store.write_bytes(
            handle,
            b"owned",
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )
    add_sanitized_tombstones(store.root, 1)
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 3)

    assert store.reset() == 2
    assert case_directories(store) == []
    assert len(
        tuple(
            path
            for path in store.root.iterdir()
            if path.name.startswith(".claimdone-delete-")
        )
    ) == 3


def test_reset_root_tombstone_capacity_one_short_fails_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handles = tuple(
        sorted(
            (store.create_case(), store.create_case()),
            key=lambda handle: handle.storage_name,
        )
    )
    refs = tuple(
        store.write_bytes(
            handle,
            f"owned-{index}".encode(),
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )
        for index, handle in enumerate(handles)
    )
    add_sanitized_tombstones(store.root, 2)
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 3)

    with pytest.raises(MediaStorageError, match="tombstone limit"):
        store.reset()

    assert tuple(
        (store.root / handle.storage_name / ref.file_id).read_bytes()
        for handle, ref in zip(handles, refs, strict=True)
    ) == (b"owned-0", b"owned-1")


def test_case_delete_capacity_allows_existing_tombstone_plus_assets_exact_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    for index in range(2):
        store.write_bytes(
            handle,
            f"owned-{index}".encode(),
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )
    add_sanitized_tombstones(case_path, 1)
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 3)

    assert store.delete_case(handle)
    assert not case_path.exists()


def test_case_delete_over_capacity_never_truncates_asset_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    refs = tuple(
        store.write_bytes(
            handle,
            f"owned-{index}".encode(),
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )
        for index in range(2)
    )
    add_sanitized_tombstones(case_path, 1)
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 2)

    with pytest.raises(MediaStorageError, match="tombstone limit"):
        store.delete_case(handle)

    assert tuple((case_path / ref.file_id).read_bytes() for ref in refs) == (
        b"owned-0",
        b"owned-1",
    )
    assert (case_path / ".claimdone-case-v2").is_file()


def test_case_delete_root_capacity_failure_preserves_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    add_sanitized_tombstones(store.root, 1)
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 1)

    with pytest.raises(MediaStorageError, match="tombstone limit"):
        store.delete_case(handle)

    assert (store.root / handle.storage_name / ref.file_id).read_bytes() == b"owned"


def test_reset_preflights_later_case_local_capacity_before_first_case_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handles = tuple(
        sorted(
            (store.create_case(), store.create_case()),
            key=lambda handle: handle.storage_name,
        )
    )
    refs_by_handle = {
        handle.storage_name: tuple(
            store.write_bytes(
                handle,
                f"{handle.storage_name}-{index}".encode(),
                role="temp",
                suffix=".bin",
                media_type="application/octet-stream",
            )
            for index in range(1 if position == 0 else 3)
        )
        for position, handle in enumerate(handles)
    }
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 2)

    with pytest.raises(MediaStorageError, match="tombstone limit"):
        store.reset()

    for handle in handles:
        case_path = store.root / handle.storage_name
        for index, ref in enumerate(refs_by_handle[handle.storage_name]):
            assert (case_path / ref.file_id).read_bytes() == (
                f"{handle.storage_name}-{index}".encode()
            )


def test_case_delete_preflights_nested_capacity_before_outer_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    nested = case_path / "nested"
    nested.mkdir()
    payloads = (nested / "first.bin", nested / "second.bin")
    payloads[0].write_bytes(b"first")
    payloads[1].write_bytes(b"second")
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 1)

    with pytest.raises(MediaStorageError, match="tombstone limit"):
        store.delete_case(handle)

    assert tuple(path.read_bytes() for path in payloads) == (b"first", b"second")
    assert (case_path / ".claimdone-case-v2").is_file()


def test_case_delete_at_limit_keeps_existing_sanitized_tombstone_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    add_sanitized_tombstones(case_path, 1)
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 1)

    assert store.delete_case(handle)
    assert not case_path.exists()


@pytest.mark.parametrize("permission_target", ("root", "case", "nested"))
def test_case_delete_preflights_directory_mutation_permissions(
    tmp_path: Path,
    permission_target: str,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    ref = store.write_bytes(
        handle,
        b"outer-owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    nested = case_path / "nested"
    nested.mkdir()
    nested_payload = nested / "nested.bin"
    nested_payload.write_bytes(b"nested-owned")
    target = {
        "root": store.root,
        "case": case_path,
        "nested": nested,
    }[permission_target]
    target.chmod(0o500)
    try:
        with pytest.raises(UnsafeStoragePath, match="cannot be mutated safely"):
            store.delete_case(handle)
    finally:
        target.chmod(0o700)

    assert (case_path / ref.file_id).read_bytes() == b"outer-owned"
    assert nested_payload.read_bytes() == b"nested-owned"
    assert not any(
        path.name.startswith(".claimdone-delete-")
        for path in case_path.iterdir()
    )


def test_case_delete_accepts_empty_nested_directory_at_exact_depth_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    (case_path / "nested").mkdir()
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONE_DEPTH", 1)

    assert store.delete_case(handle)
    assert not case_path.exists()


def test_case_delete_rejects_content_beyond_tombstone_depth_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    nested = case_path / "nested"
    nested.mkdir()
    payload = nested / "too-deep.bin"
    payload.write_bytes(b"owned")
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONE_DEPTH", 1)

    with pytest.raises(UnsafeStoragePath, match="nesting is too deep"):
        store.delete_case(handle)

    assert payload.read_bytes() == b"owned"
    assert (case_path / ".claimdone-case-v2").is_file()


def test_delete_and_reset_reject_case_shaped_regular_files(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    fake_name = f"case-{'d' * 32}"
    replacement = store.root / fake_name
    replacement.write_bytes(b"foreign")

    with pytest.raises(UnsafeStoragePath, match="authority-marked"):
        store.delete_case(CaseHandle(fake_name))
    with pytest.raises(UnsafeStoragePath, match="authority-marked"):
        store.reset()

    assert replacement.read_bytes() == b"foreign"


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

    with pytest.raises(MediaStorageError, match="digest"):
        store.read_bytes(start.session.handle, start.session.text)


def test_storage_fails_closed_when_root_is_replaced_by_outside_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    store = CaseMediaStore(root)
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )

    moved_root = tmp_path / "moved-media"
    root.rename(moved_root)
    outside = tmp_path / "outside"
    outside_case = outside / handle.storage_name
    outside_case.mkdir(parents=True)
    outside_asset = outside_case / ref.file_id
    outside_asset.write_bytes(b"outside")
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    root.symlink_to(outside, target_is_directory=True)

    operations = (
        lambda: store.write_bytes(
            handle,
            b"escape",
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        ),
        lambda: store.read_bytes(handle, ref),
        lambda: store.path_for(handle, ref),
        lambda: store.delete_asset(handle, ref),
        lambda: store.delete_case(handle),
        store.reset,
    )
    for operation in operations:
        with pytest.raises(UnsafeStoragePath, match="root identity"):
            operation()

    assert outside_asset.read_bytes() == b"outside"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in outside_case.iterdir()) == [ref.file_id]
    store.close()


def test_storage_fails_closed_when_parent_path_is_replaced(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    root = parent / "media"
    store = CaseMediaStore(root)
    handle = store.create_case()

    moved_parent = tmp_path / "moved-parent"
    parent.rename(moved_parent)
    replacement_root = parent / "media"
    replacement_case = replacement_root / handle.storage_name
    replacement_case.mkdir(parents=True)
    sentinel = replacement_case / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(UnsafeStoragePath, match="root identity"):
        store.write_bytes(
            handle,
            b"escape",
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )
    with pytest.raises(UnsafeStoragePath, match="root identity"):
        store.reset()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    store.close()


def test_storage_rejects_parent_symlink_even_when_it_points_to_pinned_root(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    store = CaseMediaStore(parent / "media")
    handle = store.create_case()
    moved_parent = tmp_path / "moved-parent"
    parent.rename(moved_parent)
    parent.symlink_to(moved_parent, target_is_directory=True)

    with pytest.raises(UnsafeStoragePath, match="root identity"):
        store.write_bytes(
            handle,
            b"must-not-write",
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )

    assert [
        path.name for path in (moved_parent / "media" / handle.storage_name).iterdir()
    ] == [".claimdone-case-v2"]
    store.close()


def test_storage_rejects_preexisting_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeStoragePath, match="component"):
        CaseMediaStore(linked_parent / "media")

    assert list(outside.iterdir()) == []


def test_storage_refuses_preexisting_empty_unowned_root(tmp_path: Path) -> None:
    root = tmp_path / "empty-unowned"
    root.mkdir()

    with pytest.raises(UnsafeStoragePath, match="pre-existing unowned"):
        CaseMediaStore(root)

    assert list(root.iterdir()) == []


@pytest.mark.parametrize("invalid", (1, 0, None, "true"))
def test_storage_existing_only_flag_requires_an_exact_bool(
    tmp_path: Path,
    invalid: object,
) -> None:
    root = tmp_path / "media"

    with pytest.raises(TypeError, match="exact bool"):
        CaseMediaStore(root, require_existing=cast(Any, invalid))

    assert not root.exists()


def test_storage_existing_only_never_creates_missing_parent_or_root(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "missing-parent"
    root = parent / "media"

    with pytest.raises(UnsafeStoragePath, match="component"):
        CaseMediaStore(root, require_existing=True)

    assert not parent.exists()


def test_storage_existing_only_reopens_v2_root_without_mutating_permissions_or_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    marker = root / ".claimdone-media-root-v2"
    with CaseMediaStore(root) as created:
        created.create_case()
    root.chmod(0o750)
    before_root = root.stat()
    before_marker = marker.stat()
    marker_content = marker.read_bytes()

    with CaseMediaStore(root, require_existing=True) as reopened:
        assert reopened.root == root

    after_root = root.stat()
    after_marker = marker.stat()
    assert stat.S_IMODE(after_root.st_mode) == 0o750
    assert (after_root.st_dev, after_root.st_ino) == (
        before_root.st_dev,
        before_root.st_ino,
    )
    assert marker.read_bytes() == marker_content
    assert (after_marker.st_dev, after_marker.st_ino, after_marker.st_mtime_ns) == (
        before_marker.st_dev,
        before_marker.st_ino,
        before_marker.st_mtime_ns,
    )


def test_storage_rejects_content_injected_during_root_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media"
    injected_name = f"case-{'e' * 32}"
    original_listdir = os.listdir
    injected = False

    def injecting_listdir(path: int) -> list[str]:
        nonlocal injected
        names = original_listdir(path)
        if (
            not injected
            and isinstance(path, int)
            and ".claimdone-media-root-v2" in names
        ):
            injected = True
            os.mkdir(injected_name, mode=0o700, dir_fd=path)
            case_fd = os.open(injected_name, os.O_RDONLY, dir_fd=path)
            try:
                keep_fd = os.open(
                    "keep.bin",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=case_fd,
                )
                os.close(keep_fd)
            finally:
                os.close(case_fd)
        return names

    monkeypatch.setattr(os, "listdir", injecting_listdir)
    with pytest.raises(UnsafeStoragePath, match="ownership was claimed"):
        CaseMediaStore(root)

    assert (root / injected_name / "keep.bin").is_file()
    assert not (root / ".claimdone-media-root-v1").exists()


def test_storage_cleans_partial_marker_and_created_root_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media"
    original_write = os.write
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    with pytest.raises(MediaStorageError, match="root marker"):
        CaseMediaStore(root)

    monkeypatch.setattr(os, "write", original_write)
    assert not root.exists()
    with CaseMediaStore(root) as restarted:
        assert restarted.create_case().storage_name.startswith("case-")


def test_storage_root_publish_retry_survives_forced_staging_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media"
    original_open = os.open

    def failing_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str) and path.startswith(".claimdone-create-"):
            raise OSError("forced staged-root open failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(UnsafeStoragePath, match="pinned safely"):
        CaseMediaStore(root)

    assert not root.exists()
    monkeypatch.setattr(os, "open", original_open)
    with CaseMediaStore(root) as restarted:
        assert restarted.create_case().storage_name.startswith("case-")


def test_root_publish_collision_failure_closes_staging_fd_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media"
    original_open = os.open
    original_close = os.close
    original_rename = media_storage._rename_noreplace
    staged_fd: int | None = None
    collision_created = False
    staging_close_count = 0

    def tracking_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal staged_fd
        if path == root.name and collision_created:
            raise OSError("forced concurrent-root open failure")
        file_fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith(".claimdone-create-"):
            staged_fd = file_fd
        return file_fd

    def colliding_rename(parent_fd: int, source: str, target: str) -> None:
        nonlocal collision_created
        if source.startswith(".claimdone-create-") and target == root.name:
            os.mkdir(root.name, dir_fd=parent_fd)
            collision_created = True
            raise FileExistsError("forced root publish collision")
        original_rename(parent_fd, source, target)

    def tracking_close(file_fd: int) -> None:
        nonlocal staging_close_count
        if staged_fd is not None and file_fd == staged_fd:
            staging_close_count += 1
        original_close(file_fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(media_storage, "_rename_noreplace", colliding_rename)

    with pytest.raises(UnsafeStoragePath, match="Concurrent media root"):
        CaseMediaStore(root)

    assert staging_close_count == 1


def test_case_publish_failure_leaves_only_hidden_restart_safe_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media"
    store = CaseMediaStore(root)
    original_writer = CaseMediaStore._write_case_marker

    def failing_writer(_directory_fd: int, _storage_name: str) -> None:
        raise OSError("forced case marker failure")

    monkeypatch.setattr(
        CaseMediaStore,
        "_write_case_marker",
        staticmethod(failing_writer),
    )
    with pytest.raises(OSError, match="forced case marker failure"):
        store.create_case()

    assert case_directories(store) == []
    assert any(path.name.startswith(".claimdone-delete-") for path in root.iterdir())
    store.close()
    monkeypatch.setattr(
        CaseMediaStore,
        "_write_case_marker",
        staticmethod(original_writer),
    )
    with CaseMediaStore(root) as restarted:
        assert restarted.create_case().storage_name.startswith("case-")


def test_storage_detects_case_directory_replacement(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    case_path = store.root / handle.storage_name
    parked = store.root / "parked-case"
    case_path.rename(parked)
    case_path.mkdir(mode=0o700)
    sentinel = case_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(UnsafeStoragePath, match="identity changed"):
        store.read_bytes(handle, ref)
    with pytest.raises(UnsafeStoragePath, match="identity changed"):
        store.delete_case(handle)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_storage_delete_rejects_nested_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    nested = case_path / "nested"
    nested.mkdir()
    (nested / "owned.bin").write_bytes(b"owned")
    parked = case_path / "parked"
    replacement = case_path / "nested"
    original_rename = media_storage._rename_noreplace
    swapped = False

    def swapping_rename(parent_fd: int, source: str, target: str) -> None:
        nonlocal swapped
        if source == "nested" and not swapped:
            swapped = True
            os.rename(
                "nested",
                "parked",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.mkdir("nested", dir_fd=parent_fd)
            replacement_fd = os.open(
                "nested",
                os.O_RDONLY | os.O_DIRECTORY,
                dir_fd=parent_fd,
            )
            try:
                keep_fd = os.open(
                    "keep.bin",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_fd,
                )
                try:
                    os.write(keep_fd, b"keep")
                finally:
                    os.close(keep_fd)
            finally:
                os.close(replacement_fd)
        original_rename(parent_fd, source, target)

    monkeypatch.setattr(media_storage, "_rename_noreplace", swapping_rename)
    with pytest.raises(UnsafeStoragePath, match="changed before deletion"):
        store.delete_case(handle)

    parked_files = [path for path in parked.iterdir() if path.is_file()]
    assert parked_files
    assert all(path.read_bytes() == b"" for path in parked_files)
    assert (replacement / "keep.bin").read_bytes() == b"keep"


def test_restarted_storage_delete_rejects_case_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media"
    original_store = CaseMediaStore(root)
    handle = original_store.create_case()
    original_store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    original_store.close()
    restarted = CaseMediaStore(root)
    case_path = root / handle.storage_name
    parked = tmp_path / "parked-case"
    original_rename = media_storage._rename_noreplace
    swapped = False

    def swapping_rename(parent_fd: int, source: str, target: str) -> None:
        nonlocal swapped
        if source == handle.storage_name and not swapped:
            swapped = True
            case_path.rename(parked)
            case_path.mkdir()
            (case_path / "keep.bin").write_bytes(b"keep")
        original_rename(parent_fd, source, target)

    monkeypatch.setattr(media_storage, "_rename_noreplace", swapping_rename)
    with pytest.raises(UnsafeStoragePath, match="changed before deletion"):
        restarted.delete_case(handle)

    parked_payloads = [
        path
        for path in parked.rglob("*")
        if path.is_file() and path.name != ".claimdone-case-v2"
    ]
    assert parked_payloads
    assert all(path.read_bytes() == b"" for path in parked_payloads)
    assert (case_path / "keep.bin").read_bytes() == b"keep"


def test_restarted_storage_rejects_markerless_case_replacement(tmp_path: Path) -> None:
    root = tmp_path / "media"
    original = CaseMediaStore(root)
    handle = original.create_case()
    original.close()
    case_path = root / handle.storage_name
    parked = tmp_path / "parked-case"
    case_path.rename(parked)
    case_path.mkdir()
    sentinel = case_path / "keep.bin"
    sentinel.write_bytes(b"keep")
    with pytest.raises(UnsafeStoragePath, match="ownership marker"):
        CaseMediaStore(root)

    assert sentinel.read_bytes() == b"keep"
    assert (parked / ".claimdone-case-v2").is_file()


def test_restarted_storage_rejects_copied_marker_on_replacement_inode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    original = CaseMediaStore(root)
    handle = original.create_case()
    original.close()
    case_path = root / handle.storage_name
    parked = tmp_path / "parked-case"
    case_path.rename(parked)
    case_path.mkdir()
    marker_name = ".claimdone-case-v2"
    (case_path / marker_name).write_bytes((parked / marker_name).read_bytes())

    with pytest.raises(UnsafeStoragePath, match="ownership marker"):
        CaseMediaStore(root)

    assert (case_path / marker_name).is_file()
    assert (parked / marker_name).is_file()


def test_storage_delete_asset_quarantines_replacement_instead_of_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    case_path = store.root / handle.storage_name
    asset_path = case_path / ref.file_id
    parked = case_path / "parked-owned.bin"
    original_rename = media_storage._rename_noreplace
    swapped = False

    def swapping_rename(parent_fd: int, source: str, target: str) -> None:
        nonlocal swapped
        if source == ref.file_id and not swapped:
            swapped = True
            asset_path.rename(parked)
            asset_path.write_bytes(b"keep")
        original_rename(parent_fd, source, target)

    monkeypatch.setattr(media_storage, "_rename_noreplace", swapping_rename)
    with pytest.raises(UnsafeStoragePath, match="changed before deletion"):
        store.delete_asset(handle, ref)

    assert parked.read_bytes() == b""
    assert asset_path.read_bytes() == b"keep"


def test_storage_quarantine_never_overwrites_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    case_path = store.root / handle.storage_name
    collision = case_path / f".claimdone-delete-{'a' * 32}"
    collision.write_bytes(b"keep")
    original_token_hex = secrets.token_hex
    monkeypatch.setattr(secrets, "token_hex", lambda _size: "a" * 32)

    with pytest.raises(MediaStorageError, match="private deletion quarantine"):
        store.delete_asset(handle, ref)

    assert collision.read_bytes() == b"keep"
    assert (case_path / ref.file_id).read_bytes() == b"owned"

    monkeypatch.setattr(secrets, "token_hex", original_token_hex)
    assert store.delete_asset(handle, ref)
    assert collision.read_bytes() == b"keep"
    assert not (case_path / ref.file_id).exists()


def test_delete_asset_restores_name_after_pretruncate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    asset_path = store.root / handle.storage_name / ref.file_id
    original_ftruncate = os.ftruncate

    def failing_ftruncate(_file_fd: int, _length: int) -> None:
        raise OSError("forced asset truncate failure")

    monkeypatch.setattr(os, "ftruncate", failing_ftruncate)
    with pytest.raises(OSError, match="forced asset truncate failure"):
        store.delete_asset(handle, ref)

    assert asset_path.read_bytes() == b"owned"
    monkeypatch.setattr(os, "ftruncate", original_ftruncate)
    assert store.delete_asset(handle, ref)
    assert not asset_path.exists()


def test_delete_asset_rejects_external_hardlink_without_mutating_bytes(
    tmp_path: Path,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    asset_path = store.root / handle.storage_name / ref.file_id
    outside = tmp_path / "outside.bin"
    os.link(asset_path, outside)

    with pytest.raises(UnsafeStoragePath, match="multiple hard links"):
        store.delete_asset(handle, ref)

    assert asset_path.read_bytes() == b"owned"
    assert outside.read_bytes() == b"owned"
    outside.unlink()
    assert store.delete_asset(handle, ref)


def test_write_asset_directory_fsync_failure_destroys_unpublished_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    original_fsync = os.fsync

    def failing_fsync(file_fd: int) -> None:
        metadata = os.fstat(file_fd)
        if stat.S_ISDIR(metadata.st_mode):
            raise OSError("forced case directory fsync failure")
        original_fsync(file_fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="forced case directory fsync failure"):
        store.write_bytes(
            handle,
            b"must-not-survive",
            role="temp",
            suffix=".bin",
            media_type="application/octet-stream",
        )

    for path in case_path.iterdir():
        if path.name != ".claimdone-case-v2" and path.is_file():
            assert path.read_bytes() == b""


def test_case_delete_failure_preserves_marker_and_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    store.write_bytes(
        handle,
        b"sensitive",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    case_path = store.root / handle.storage_name
    original_ftruncate = os.ftruncate
    failed = False

    def failing_ftruncate(file_fd: int, length: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("forced truncate failure")
        original_ftruncate(file_fd, length)

    monkeypatch.setattr(os, "ftruncate", failing_ftruncate)
    with pytest.raises(OSError, match="forced truncate failure"):
        store.delete_case(handle)

    assert (case_path / ".claimdone-case-v2").is_file()
    monkeypatch.setattr(os, "ftruncate", original_ftruncate)
    assert store.delete_case(handle)
    assert not case_path.exists()


def test_case_delete_fifo_failure_preserves_marker_then_retry_succeeds(
    tmp_path: Path,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    blocked = case_path / "blocked.fifo"
    os.mkfifo(blocked)

    with pytest.raises(UnsafeStoragePath, match="unsupported filesystem type"):
        store.delete_case(handle)

    assert (case_path / ".claimdone-case-v2").is_file()
    blocked.unlink()
    assert store.delete_case(handle)
    assert not case_path.exists()


def test_case_delete_rejects_external_hardlink_and_remains_retryable(
    tmp_path: Path,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"foreign")
    linked = case_path / "foreign.bin"
    os.link(outside, linked)

    with pytest.raises(UnsafeStoragePath, match="multiple hard links"):
        store.delete_case(handle)

    assert outside.read_bytes() == b"foreign"
    assert linked.read_bytes() == b"foreign"
    assert (case_path / ".claimdone-case-v2").is_file()
    linked.unlink()
    assert store.delete_case(handle)
    assert outside.read_bytes() == b"foreign"


def test_successful_case_delete_destroys_media_bytes_and_root_reopens(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    store = CaseMediaStore(root)
    handle = store.create_case()
    store.write_bytes(
        handle,
        b"sensitive-media-payload",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )

    assert store.delete_case(handle)
    store.close()
    tombstone_files = [path for path in root.rglob("*") if path.is_file()]
    assert tombstone_files
    for path in tombstone_files:
        content = path.read_bytes()
        assert b"sensitive-media-payload" not in content
        if path.name != ".claimdone-media-root-v2" and path.name != ".claimdone-case-v2":
            assert content == b""

    restarted = CaseMediaStore(root)
    assert restarted.create_case().storage_name.startswith("case-")


def test_case_is_sanitized_before_public_name_becomes_a_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    case_path = store.root / handle.storage_name
    store.write_bytes(
        handle,
        b"sensitive-before-detach",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    original_quarantine = media_storage._quarantine_entry
    observed_sanitized = False

    def observing_quarantine(
        parent_fd: int,
        name: str,
        *,
        expected: tuple[int, int],
        initial_quarantine: str | None = None,
    ) -> str:
        nonlocal observed_sanitized
        if name == handle.storage_name:
            payloads = [
                path
                for path in case_path.rglob("*")
                if path.is_file() and path.name != ".claimdone-case-v2"
            ]
            assert payloads
            assert all(path.read_bytes() == b"" for path in payloads)
            observed_sanitized = True
        return original_quarantine(
            parent_fd,
            name,
            expected=expected,
            initial_quarantine=initial_quarantine,
        )

    monkeypatch.setattr(media_storage, "_quarantine_entry", observing_quarantine)
    assert store.delete_case(handle)
    assert observed_sanitized


def test_restart_rejects_unsanitized_tombstone_with_reset_guidance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    store = CaseMediaStore(root)
    store.close()
    tombstone = root / f".claimdone-delete-{'c' * 32}"
    tombstone.mkdir()
    payload = tombstone / f"temp-{'d' * 32}.bin"
    payload.write_bytes(b"must-remain-visible")

    with pytest.raises(UnsafeStoragePath, match="explicit local reset"):
        CaseMediaStore(root)

    assert payload.read_bytes() == b"must-remain-visible"


def test_tombstone_limit_fails_before_asset_bytes_are_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaseMediaStore(tmp_path / "media")
    handle = store.create_case()
    ref = store.write_bytes(
        handle,
        b"owned",
        role="temp",
        suffix=".bin",
        media_type="application/octet-stream",
    )
    case_path = store.root / handle.storage_name
    (case_path / f".claimdone-delete-{'e' * 32}").write_bytes(b"")
    monkeypatch.setattr(media_storage, "_MAX_TOMBSTONES_PER_DIRECTORY", 1)

    with pytest.raises(MediaStorageError, match="tombstone limit"):
        store.delete_asset(handle, ref)

    assert (case_path / ref.file_id).read_bytes() == b"owned"


def test_root_staging_limit_is_bounded_and_requires_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "data"
    parent.mkdir()
    (parent / f".claimdone-create-{'f' * 32}").mkdir()
    monkeypatch.setattr(media_storage, "_MAX_CREATION_STAGING_PER_PARENT", 1)

    with pytest.raises(MediaStorageError, match="staging limit"):
        CaseMediaStore(parent / "media")

    assert not (parent / "media").exists()


def test_persisted_markers_do_not_bind_transient_device_number(tmp_path: Path) -> None:
    root = tmp_path / "media"
    store = CaseMediaStore(root)
    handle = store.create_case()

    root_marker = (root / ".claimdone-media-root-v2").read_bytes()
    case_marker = (root / handle.storage_name / ".claimdone-case-v2").read_bytes()
    assert b":" not in root_marker
    assert b":" not in case_marker


def test_storage_rejects_legacy_v1_root_with_reset_guidance(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    marker = root / ".claimdone-media-root-v1"
    marker.write_bytes(b"ClaimDone temporary media root v1\n")

    with pytest.raises(UnsafeStoragePath, match="explicit reset"):
        CaseMediaStore(root)

    assert marker.read_bytes() == b"ClaimDone temporary media root v1\n"


def test_storage_close_is_idempotent_and_blocks_future_operations(tmp_path: Path) -> None:
    store = CaseMediaStore(tmp_path / "media")
    store.create_case()
    store.close()
    store.close()

    with pytest.raises(MediaStorageError, match="closed"):
        store.create_case()


def test_storage_close_waits_for_inflight_fd_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media"
    store = CaseMediaStore(root)
    entered = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    created: list[CaseHandle] = []
    failures: list[BaseException] = []
    original_require = store._require_root_identity

    def paused_require() -> None:
        original_require()
        entered.set()
        if not release.wait(timeout=2):
            raise AssertionError("test did not release the media operation")

    def create_case() -> None:
        try:
            created.append(store.create_case())
        except BaseException as error:
            failures.append(error)

    def close_store() -> None:
        close_started.set()
        try:
            store.close()
        except BaseException as error:
            failures.append(error)
        finally:
            close_finished.set()

    monkeypatch.setattr(store, "_require_root_identity", paused_require)
    creator = threading.Thread(target=create_case)
    closer = threading.Thread(target=close_store)
    creator.start()
    assert entered.wait(timeout=2)
    closer.start()
    assert close_started.wait(timeout=2)
    assert not close_finished.wait(timeout=0.05)
    release.set()
    creator.join(timeout=2)
    closer.join(timeout=2)

    assert not creator.is_alive() and not closer.is_alive()
    assert failures == []
    assert len(created) == 1
    assert (root / created[0].storage_name).is_dir()


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
