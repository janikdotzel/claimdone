from pathlib import Path

import pytest

from scripts.reset import reset_generated_state


def _create_project_root(root: Path) -> None:
    root.mkdir()
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.uv]\n", encoding="utf-8")


def _write(root: Path, relative: str, value: str = "sentinel") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def test_reset_removes_only_generated_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _create_project_root(project)

    generated = (
        _write(project, "apps/web/.next/build.json"),
        _write(project, "apps/web/coverage/report.json"),
        _write(project, "apps/web/next-env.d.ts"),
        _write(project, "apps/web/tsconfig.tsbuildinfo"),
        _write(project, ".pytest_cache/cache"),
        _write(project, ".mypy_cache/cache"),
        _write(project, ".ruff_cache/cache"),
        _write(project, ".local/state/case.sqlite3"),
        _write(project, ".local/tmp/upload.bin"),
        _write(project, "services/api/src/example/__pycache__/module.pyc"),
    )
    preserved = {
        _write(project, ".env", "local-secret"): "local-secret",
        _write(project, ".env.local", "other-secret"): "other-secret",
        _write(project, ".venv/keep"): "sentinel",
        _write(project, ".tools/keep"): "sentinel",
        _write(project, "node_modules/keep"): "sentinel",
        _write(project, "fixtures/sample.txt"): "sentinel",
        _write(project, "services/api/src/example/module.py"): "sentinel",
    }

    removed_count = reset_generated_state(project)

    assert removed_count > 0
    assert all(not path.exists() for path in generated)
    for path, expected in preserved.items():
        assert path.read_text(encoding="utf-8") == expected


def test_reset_unlinks_allowed_symlink_without_following_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    _create_project_root(project)
    outside.mkdir()
    sentinel = _write(outside, "do-not-delete.txt")
    (project / ".local").mkdir()
    (project / ".local/state").symlink_to(outside, target_is_directory=True)

    reset_generated_state(project)

    assert not (project / ".local/state").exists()
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_reset_refuses_target_below_escaping_parent_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    _create_project_root(project)
    sentinel = _write(outside, "state/do-not-delete.txt")
    (project / ".local").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="resolves outside"):
        reset_generated_state(project)

    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_reset_refuses_unknown_directory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ClaimDone repository root"):
        reset_generated_state(tmp_path)
