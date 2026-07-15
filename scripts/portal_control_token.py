from __future__ import annotations

import fcntl
import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_DIRECTORY = Path(".local/claimdone")
TOKEN_FILENAME = ".portal-control-token"
TOKEN_PATH = TOKEN_DIRECTORY / TOKEN_FILENAME

_LOCK_FILENAME = ".portal-control-token.lock"
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_MINIMUM_TOKEN_LENGTH = 32
_MAXIMUM_TOKEN_LENGTH = 512


def _open_flags(*, directory: bool = False, writable: bool = False) -> int:
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= os.O_RDWR if writable else os.O_RDONLY
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _validate_project_root(root: Path) -> Path:
    resolved = root.resolve()
    required_markers = (resolved / "package.json", resolved / "pyproject.toml")
    if not all(marker.is_file() for marker in required_markers):
        raise RuntimeError("refusing portal-token setup outside a ClaimDone repository root")
    return resolved


def _open_secure_directory(parent_fd: int, name: str) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)

    try:
        directory_fd = os.open(
            name,
            _open_flags(directory=True),
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise RuntimeError(f"refusing insecure local-state directory: {name}") from error

    try:
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError(f"refusing unowned local-state directory: {name}")
        os.fchmod(directory_fd, _DIRECTORY_MODE)
        if stat.S_IMODE(os.fstat(directory_fd).st_mode) != _DIRECTORY_MODE:
            raise RuntimeError(f"could not secure local-state directory: {name}")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _validate_regular_file_identity(file_fd: int, *, label: str) -> None:
    metadata = os.fstat(file_fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(f"refusing insecure {label} file")


def _validate_regular_file(file_fd: int, *, label: str, mode: int) -> None:
    _validate_regular_file_identity(file_fd, label=label)
    metadata = os.fstat(file_fd)
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise RuntimeError(f"refusing {label} file with insecure permissions")


def _open_lock(directory_fd: int) -> int:
    try:
        lock_fd = os.open(
            _LOCK_FILENAME,
            _open_flags(writable=True) | os.O_CREAT,
            _FILE_MODE,
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise RuntimeError("could not open the portal-token lock") from error

    try:
        _validate_regular_file_identity(lock_fd, label="portal-token lock")
        os.fchmod(lock_fd, _FILE_MODE)
        _validate_regular_file(lock_fd, label="portal-token lock", mode=_FILE_MODE)
    except BaseException:
        os.close(lock_fd)
        raise
    return lock_fd


def _decode_token(payload: bytes) -> str:
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise RuntimeError("portal control token has an invalid file format")
    try:
        token = payload[:-1].decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("portal control token must be ASCII") from error
    if (
        not _MINIMUM_TOKEN_LENGTH <= len(token) <= _MAXIMUM_TOKEN_LENGTH
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise RuntimeError("portal control token has an invalid value")
    return token


def _read_token(directory_fd: int) -> str | None:
    try:
        token_fd = os.open(
            TOKEN_FILENAME,
            _open_flags(),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError("could not securely open the portal control token") from error

    try:
        _validate_regular_file(token_fd, label="portal control token", mode=_FILE_MODE)
        payload = bytearray()
        while chunk := os.read(token_fd, 1024):
            payload.extend(chunk)
            if len(payload) > _MAXIMUM_TOKEN_LENGTH + 1:
                raise RuntimeError("portal control token file is too large")
        return _decode_token(bytes(payload))
    finally:
        os.close(token_fd)


def _write_all(file_fd: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        written += os.write(file_fd, payload[written:])


def _create_token(directory_fd: int) -> None:
    token = secrets.token_urlsafe(32)
    payload = f"{token}\n".encode("ascii")
    temporary_name = f".{TOKEN_FILENAME}.{secrets.token_hex(16)}.tmp"
    temporary_fd: int | None = None
    try:
        temporary_fd = os.open(
            temporary_name,
            _open_flags(writable=True) | os.O_CREAT | os.O_EXCL,
            _FILE_MODE,
            dir_fd=directory_fd,
        )
        os.fchmod(temporary_fd, _FILE_MODE)
        _validate_regular_file(
            temporary_fd,
            label="temporary portal control token",
            mode=_FILE_MODE,
        )
        _write_all(temporary_fd, payload)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.link(
            temporary_name,
            TOKEN_FILENAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)


def load_or_create_portal_control_token(root: Path) -> str:
    safe_root = _validate_project_root(root)
    root_fd = os.open(safe_root, _open_flags(directory=True))
    local_fd: int | None = None
    claimdone_fd: int | None = None
    lock_fd: int | None = None
    try:
        local_fd = _open_secure_directory(root_fd, ".local")
        claimdone_fd = _open_secure_directory(local_fd, "claimdone")
        lock_fd = _open_lock(claimdone_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        token = _read_token(claimdone_fd)
        if token is None:
            _create_token(claimdone_fd)
            token = _read_token(claimdone_fd)
        if token is None:
            raise RuntimeError("portal control token creation did not persist")
        return token
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if claimdone_fd is not None:
            os.close(claimdone_fd)
        if local_fd is not None:
            os.close(local_fd)
        os.close(root_fd)


def launch_dev_with_portal_control_token(root: Path) -> NoReturn:
    safe_root = _validate_project_root(root)
    dev_script = safe_root / "scripts/dev.sh"
    try:
        dev_metadata = dev_script.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("ClaimDone dev launcher is missing") from error
    if (
        not stat.S_ISREG(dev_metadata.st_mode)
        or dev_metadata.st_uid != os.getuid()
        or dev_metadata.st_nlink != 1
    ):
        raise RuntimeError("ClaimDone dev launcher is missing")

    environment = os.environ.copy()
    environment["CLAIMDONE_PORTAL_CONTROL_TOKEN"] = (
        load_or_create_portal_control_token(safe_root)
    )
    os.execve(
        "/bin/bash",
        ["/bin/bash", str(dev_script)],
        environment,
    )


def main() -> NoReturn:
    launch_dev_with_portal_control_token(PROJECT_ROOT)


if __name__ == "__main__":
    main()
