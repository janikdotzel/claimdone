from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts.portal_control_token import (
    TOKEN_PATH,
    launch_dev_with_portal_control_token,
    load_or_create_portal_control_token,
)
from scripts.reset import reset_generated_state


def _create_project_root(root: Path) -> None:
    root.mkdir()
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.uv]\n", encoding="utf-8")


def test_token_is_created_once_without_output_and_with_private_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _create_project_root(project)

    first = load_or_create_portal_control_token(project)
    second = load_or_create_portal_control_token(project)

    token_path = project / TOKEN_PATH
    assert first == second
    assert token_path.read_bytes() == f"{first}\n".encode("ascii")
    assert 32 <= len(first) <= 512
    assert all(33 <= ord(character) <= 126 for character in first)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((project / ".local").stat().st_mode) == 0o700
    assert stat.S_IMODE((project / ".local/claimdone").stat().st_mode) == 0o700
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_token_creation_refuses_symlinked_local_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    _create_project_root(project)
    outside.mkdir()
    (project / ".local").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="insecure local-state directory"):
        load_or_create_portal_control_token(project)

    assert tuple(outside.iterdir()) == ()


def test_token_creation_refuses_symlinked_token_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside-token"
    _create_project_root(project)
    (project / ".local/claimdone").mkdir(parents=True)
    outside.write_text("x" * 43 + "\n", encoding="ascii")
    outside.chmod(0o600)
    (project / TOKEN_PATH).symlink_to(outside)

    with pytest.raises(RuntimeError, match="securely open"):
        load_or_create_portal_control_token(project)

    assert outside.read_text(encoding="ascii") == "x" * 43 + "\n"


def test_token_creation_refuses_existing_world_readable_secret(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _create_project_root(project)
    token_path = project / TOKEN_PATH
    token_path.parent.mkdir(parents=True)
    token_path.write_text("x" * 43 + "\n", encoding="ascii")
    token_path.chmod(0o644)

    with pytest.raises(RuntimeError, match="insecure permissions"):
        load_or_create_portal_control_token(project)

    assert stat.S_IMODE(token_path.stat().st_mode) == 0o644


def test_token_creation_does_not_chmod_a_hardlinked_lock_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside-lock"
    _create_project_root(project)
    token_directory = project / TOKEN_PATH.parent
    token_directory.mkdir(parents=True)
    outside.write_text("sentinel\n", encoding="utf-8")
    outside.chmod(0o644)
    os.link(outside, token_directory / ".portal-control-token.lock")

    with pytest.raises(RuntimeError, match="insecure portal-token lock"):
        load_or_create_portal_control_token(project)

    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_launcher_reexecutes_dev_without_exposing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _create_project_root(project)
    dev_script = project / "scripts/dev.sh"
    dev_script.parent.mkdir()
    dev_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    executed: dict[str, object] = {}

    class ExecIntercepted(RuntimeError):
        pass

    def fake_execve(path: str, argv: list[str], environment: dict[str, str]) -> None:
        executed.update(path=path, argv=argv, environment=environment)
        raise ExecIntercepted

    monkeypatch.setattr(os, "execve", fake_execve)

    with pytest.raises(ExecIntercepted):
        launch_dev_with_portal_control_token(project)

    environment = executed["environment"]
    assert isinstance(environment, dict)
    token = environment["CLAIMDONE_PORTAL_CONTROL_TOKEN"]
    assert isinstance(token, str)
    assert (project / TOKEN_PATH).read_text(encoding="ascii") == f"{token}\n"
    argv = executed["argv"]
    assert isinstance(argv, list)
    assert token not in argv
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_reset_removes_persisted_portal_control_token(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _create_project_root(project)
    load_or_create_portal_control_token(project)

    reset_generated_state(project)

    assert not (project / TOKEN_PATH).exists()
