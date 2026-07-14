from __future__ import annotations

import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXACT_GENERATED_PATHS = (
    Path(".local/claimdone"),
    Path(".local/state"),
    Path(".local/tmp"),
    Path(".mypy_cache"),
    Path(".pytest_cache"),
    Path(".ruff_cache"),
    Path("apps/web/.next"),
    Path("apps/web/coverage"),
    Path("apps/web/next-env.d.ts"),
    Path("apps/web/tsconfig.tsbuildinfo"),
    Path("coverage"),
)

CACHE_SEARCH_ROOTS = (
    Path("services/api"),
    Path("scripts"),
    Path("evals"),
)


def _is_present(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_project_root(root: Path) -> Path:
    resolved = root.resolve()
    required_markers = (resolved / "package.json", resolved / "pyproject.toml")
    if not all(marker.is_file() for marker in required_markers):
        raise RuntimeError("refusing reset outside a ClaimDone repository root")
    return resolved


def _remove_entry(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root)
    except ValueError as error:
        raise RuntimeError("refusing reset target outside the repository") from error

    if not _is_present(path):
        return False

    try:
        path.parent.resolve().relative_to(root)
    except ValueError as error:
        message = "refusing reset target whose parent resolves outside the repository"
        raise RuntimeError(message) from error

    if path.is_symlink():
        path.unlink()
        return True

    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise RuntimeError("refusing reset target that resolves outside the repository") from error

    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _discover_python_caches(root: Path) -> set[Path]:
    targets: set[Path] = set()
    for relative_search_root in CACHE_SEARCH_ROOTS:
        search_root = root / relative_search_root
        if not search_root.is_dir() or search_root.is_symlink():
            continue

        for current, directories, filenames in os.walk(search_root, followlinks=False):
            current_path = Path(current)
            for directory in tuple(directories):
                if directory == "__pycache__":
                    targets.add(current_path / directory)
                    directories.remove(directory)
            for filename in filenames:
                if filename.endswith((".pyc", ".pyo")):
                    targets.add(current_path / filename)
    return targets


def reset_generated_state(root: Path) -> int:
    safe_root = _validate_project_root(root)
    targets = {safe_root / relative for relative in EXACT_GENERATED_PATHS}
    targets.update(_discover_python_caches(safe_root))

    removed = 0
    for target in sorted(targets, key=lambda item: len(item.parts), reverse=True):
        removed += int(_remove_entry(target, safe_root))
    return removed


def main() -> None:
    removed = reset_generated_state(PROJECT_ROOT)
    print(f"Reset complete: removed {removed} generated entries.")
    print("Environment files, dependencies, source files, and fixtures were preserved.")


if __name__ == "__main__":
    main()
