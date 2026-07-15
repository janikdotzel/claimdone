import hashlib
import json
import os
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from scripts.generate_int002_fixtures import (
    FixtureManifestError,
    build_png,
    check_materialized,
    computed_digests,
    load_manifest,
    manifest_path,
    materialize,
    normalized_statement_bytes,
    repository_root,
)


def test_manifest_binds_exactly_three_reproducible_synthetic_pngs() -> None:
    root = repository_root()
    manifest = load_manifest(manifest_path(root))
    digests = computed_digests(manifest, root)

    assert manifest.fixture_id == "claimdone-int002-main-v1"
    assert tuple(image.seed for image in manifest.images) == (1, 2, 3)
    assert len({image.semantic_id for image in manifest.images}) == 3
    assert len({image.sha256 for image in manifest.images}) == 3
    assert digests["statement"] == manifest.statement_sha256

    for image in manifest.images:
        payload = build_png(image)
        assert hashlib.sha256(payload).hexdigest() == image.sha256
        assert payload == build_png(image)
        with Image.open(BytesIO(payload)) as decoded:
            assert decoded.format == "PNG"
            assert decoded.mode == "RGB"
            assert decoded.size == (image.width, image.height)
            decoded.verify()


def test_statement_and_clarification_are_exact_and_non_identifying() -> None:
    root = repository_root()
    manifest = load_manifest(manifest_path(root))
    statement = normalized_statement_bytes(
        root / "fixtures" / "int002" / manifest.statement_filename
    ).decode("utf-8")

    assert manifest.clarification_field == "incident_time"
    assert manifest.clarification_round == 1
    assert manifest.clarification_answer == "14:30:00"
    assert "Synthetic ClaimDone Build Week demo" in statement
    assert "DEMO-POLICY-001" in statement
    assert "incident time is not yet provided" in statement
    assert "@" not in statement


def test_manifest_parser_fails_closed_for_unknown_fields_and_tampered_digest(
    tmp_path: Path,
) -> None:
    source = json.loads(manifest_path(repository_root()).read_text(encoding="utf-8"))
    unknown = {**source, "provider": "forbidden"}
    unknown_path = tmp_path / "unknown.json"
    unknown_path.write_text(json.dumps(unknown), encoding="utf-8")
    with pytest.raises(FixtureManifestError, match="canonical fields"):
        load_manifest(unknown_path)

    tampered = json.loads(json.dumps(source))
    tampered["images"][0]["sha256"] = "f" * 64
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    manifest = load_manifest(tampered_path)
    assert hashlib.sha256(build_png(manifest.images[0])).hexdigest() != "f" * 64


@pytest.mark.parametrize(
    ("path", "value"),
    (("schemaVersion", True), ("clarification.round", True)),
)
def test_manifest_parser_rejects_boolean_integer_fields(
    tmp_path: Path,
    path: str,
    value: bool,
) -> None:
    source = json.loads(manifest_path(repository_root()).read_text(encoding="utf-8"))
    if path == "schemaVersion":
        source["schemaVersion"] = value
    else:
        source["clarification"]["round"] = value
    candidate = tmp_path / "boolean.json"
    candidate.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(FixtureManifestError):
        load_manifest(candidate)


def test_statement_normalization_rejects_ambiguous_line_endings(tmp_path: Path) -> None:
    for index, payload in enumerate((b"value", b"value\n\n", b"value\r\n"), start=1):
        path = tmp_path / f"statement-{index}.txt"
        path.write_bytes(payload)
        with pytest.raises(FixtureManifestError, match="one LF terminator"):
            normalized_statement_bytes(path)


def test_materialize_checks_statement_before_writing_generated_files(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(manifest_path(repository_root()))
    statement = tmp_path / "fixtures" / "int002" / manifest.statement_filename
    statement.parent.mkdir(parents=True)
    statement.write_text("tampered synthetic statement\n", encoding="utf-8")

    with pytest.raises(FixtureManifestError, match="statement.*manifest digest"):
        materialize(manifest, tmp_path)

    assert not (tmp_path / ".local").exists()


def test_materialize_and_check_roundtrip_only_manifest_bound_bytes(
    tmp_path: Path,
) -> None:
    root = repository_root()
    manifest = load_manifest(manifest_path(root))
    source_statement = root / "fixtures" / "int002" / manifest.statement_filename
    target_statement = tmp_path / "fixtures" / "int002" / manifest.statement_filename
    target_statement.parent.mkdir(parents=True)
    target_statement.write_bytes(source_statement.read_bytes())

    descriptor = materialize(manifest, tmp_path)

    assert descriptor == check_materialized(manifest, tmp_path)
    generated = descriptor["images"]
    assert isinstance(generated, list)
    assert len(generated) == 3


def test_materialize_refuses_predictable_temporary_symlink_escape(
    tmp_path: Path,
) -> None:
    root = repository_root()
    manifest = load_manifest(manifest_path(root))
    source_statement = root / "fixtures" / "int002" / manifest.statement_filename
    target_statement = tmp_path / "fixtures" / "int002" / manifest.statement_filename
    target_statement.parent.mkdir(parents=True)
    target_statement.write_bytes(source_statement.read_bytes())
    output = tmp_path / ".local" / "int002-fixtures"
    output.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"sentinel")
    first = manifest.images[0]
    temporary = output / f".{first.filename}.{os.getpid()}.tmp"
    temporary.symlink_to(outside)

    with pytest.raises(FixtureManifestError, match="temporary path is unsafe"):
        materialize(manifest, tmp_path)

    assert outside.read_bytes() == b"sentinel"


def test_materialize_never_removes_a_preexisting_regular_temporary_file(
    tmp_path: Path,
) -> None:
    root = repository_root()
    manifest = load_manifest(manifest_path(root))
    source_statement = root / "fixtures" / "int002" / manifest.statement_filename
    target_statement = tmp_path / "fixtures" / "int002" / manifest.statement_filename
    target_statement.parent.mkdir(parents=True)
    target_statement.write_bytes(source_statement.read_bytes())
    output = tmp_path / ".local" / "int002-fixtures"
    output.mkdir(parents=True)
    first = manifest.images[0]
    temporary = output / f".{first.filename}.{os.getpid()}.tmp"
    temporary.write_bytes(b"foreign sentinel")

    with pytest.raises(FixtureManifestError, match="temporary path is unsafe"):
        materialize(manifest, tmp_path)

    assert temporary.read_bytes() == b"foreign sentinel"


def test_check_rejects_but_never_deletes_unexpected_materialized_files(
    tmp_path: Path,
) -> None:
    root = repository_root()
    manifest = load_manifest(manifest_path(root))
    source_statement = root / "fixtures" / "int002" / manifest.statement_filename
    target_statement = tmp_path / "fixtures" / "int002" / manifest.statement_filename
    target_statement.parent.mkdir(parents=True)
    target_statement.write_bytes(source_statement.read_bytes())
    descriptor = materialize(manifest, tmp_path)
    assert descriptor["fixtureId"] == manifest.fixture_id
    extra = tmp_path / ".local" / "int002-fixtures" / "04-unexpected.png"
    extra.write_bytes(b"foreign sentinel")

    with pytest.raises(FixtureManifestError, match="exactly the manifest-bound files"):
        check_materialized(manifest, tmp_path)

    assert extra.read_bytes() == b"foreign sentinel"
