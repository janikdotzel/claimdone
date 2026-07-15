"""Directory-FD-anchored local storage for temporary case media."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from functools import cache, wraps
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any, Concatenate, Literal, Self, cast

from .types import CaseHandle, StoredAssetRef

AssetRole = Literal["source", "model", "text", "audio", "transcript", "temp"]
_ASSET_ROLES = frozenset({"source", "model", "text", "audio", "transcript", "temp"})

_CASE_NAME = re.compile(r"^case-[a-f0-9]{32}$")
_FILE_NAME = re.compile(
    r"^(?:source|model|text|audio|transcript|temp)-[a-f0-9]{32}\."
    r"(?:jpg|png|wav|txt|bin)$"
)
_SUFFIX = re.compile(r"^\.(?:jpg|png|wav|txt|bin)$")
_LEGACY_ROOT_MARKER = ".claimdone-media-root-v1"
_ROOT_MARKER = ".claimdone-media-root-v2"
_ROOT_MARKER_PREFIX = b"ClaimDone temporary media root v2\0"
_CASE_MARKER = ".claimdone-case-v2"
_CASE_MARKER_PREFIX = b"ClaimDone temporary case v2\0"
_TOMBSTONE_PREFIX = ".claimdone-delete-"
_CREATION_PREFIX = ".claimdone-create-"
_MAX_TOMBSTONES_PER_DIRECTORY = 4096
_MAX_CREATION_STAGING_PER_PARENT = 64
_MAX_TOMBSTONE_DEPTH = 64


class UnsafeStoragePath(ValueError):
    """Raised when a caller supplies a non-owned or unsafe storage reference."""


class MediaStorageError(RuntimeError):
    """Raised when stored bytes are missing, altered, or not regular files."""


type _FileIdentity = tuple[int, int]


def _serialized[**P, R](
    method: Callable[Concatenate[CaseMediaStore, P], R],
) -> Callable[Concatenate[CaseMediaStore, P], R]:
    """Keep the pinned descriptor alive for the complete public operation."""

    @wraps(method)
    def wrapper(self: CaseMediaStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast("Callable[Concatenate[CaseMediaStore, P], R]", wrapper)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return (metadata.st_dev, metadata.st_ino)


def _read_fd(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(file_fd, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _require_entry_capacity(
    directory_fd: int,
    *,
    prefix: str,
    limit: int,
    message: str,
) -> None:
    count = sum(1 for name in os.listdir(directory_fd) if name.startswith(prefix))
    if count >= limit:
        raise MediaStorageError(message)


def _open_absolute_directory(path: Path) -> int:
    """Open every component without following a replaceable parent symlink."""

    if not path.is_absolute():
        raise UnsafeStoragePath("Media root must resolve to an absolute directory")
    try:
        directory_fd = os.open(path.anchor, _directory_open_flags())
    except OSError as error:
        raise UnsafeStoragePath("Filesystem anchor could not be opened safely") from error
    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise UnsafeStoragePath(
                    "Media root component could not be opened safely"
                ) from error
            previous_fd = directory_fd
            directory_fd = next_fd
            os.close(previous_fd)
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _open_or_create_absolute_directory(path: Path) -> tuple[int, bool]:
    """Create missing components and reject every pre-existing symlink."""

    if not path.is_absolute():
        raise UnsafeStoragePath("Media root must be an absolute directory")
    try:
        directory_fd = os.open(path.anchor, _directory_open_flags())
    except OSError as error:
        raise UnsafeStoragePath("Filesystem anchor could not be opened safely") from error
    try:
        final_created = False
        components = path.parts[1:]
        for index, component in enumerate(components):
            try:
                next_fd = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                    if index == len(components) - 1:
                        final_created = True
                    next_fd = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise UnsafeStoragePath(
                        "Media root component could not be created safely"
                    ) from error
            except OSError as error:
                raise UnsafeStoragePath(
                    "Media root component could not be opened safely"
                ) from error
            previous_fd = directory_fd
            directory_fd = next_fd
            os.close(previous_fd)
        return directory_fd, final_created
    except BaseException:
        os.close(directory_fd)
        raise


@cache
def _rename_noreplace_api() -> tuple[Any, int]:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        symbol = "renameatx_np"
        flag = 0x00000004  # RENAME_EXCL from Darwin sys/stdio.h.
    elif sys.platform.startswith("linux"):
        symbol = "renameat2"
        flag = 1  # RENAME_NOREPLACE from Linux uapi/linux/fs.h.
    else:
        raise MediaStorageError("Atomic no-replace rename is unavailable")
    try:
        function = getattr(libc, symbol)
    except AttributeError as error:
        raise MediaStorageError("Atomic no-replace rename is unavailable") from error
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    return function, flag


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    function, flag = _rename_noreplace_api()
    ctypes.set_errno(0)
    result = function(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    if error_number == errno.ENOENT:
        raise FileNotFoundError(error_number, os.strerror(error_number), source)
    raise OSError(error_number, os.strerror(error_number), source)


def _allocate_quarantine_name(parent_fd: int) -> str:
    _require_entry_capacity(
        parent_fd,
        prefix=_TOMBSTONE_PREFIX,
        limit=_MAX_TOMBSTONES_PER_DIRECTORY,
        message="Media tombstone limit reached; run the explicit local reset",
    )
    for _ in range(16):
        quarantine = f"{_TOMBSTONE_PREFIX}{secrets.token_hex(16)}"
        try:
            os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return quarantine
    raise MediaStorageError("Could not allocate a private deletion quarantine")


def _quarantine_entry(
    parent_fd: int,
    name: str,
    *,
    expected: _FileIdentity,
    initial_quarantine: str | None = None,
) -> str:
    """Atomically move to a new private name without replacing any entry."""

    quarantine = initial_quarantine
    for _ in range(16):
        if quarantine is None:
            quarantine = _allocate_quarantine_name(parent_fd)
        try:
            _rename_noreplace(parent_fd, name, quarantine)
        except FileExistsError:
            quarantine = None
            continue
        except FileNotFoundError as error:
            raise UnsafeStoragePath("Storage entry disappeared before deletion") from error
        current = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != expected:
            with suppress(OSError, UnsafeStoragePath, MediaStorageError):
                _rename_noreplace(parent_fd, quarantine, name)
                os.fsync(parent_fd)
            raise UnsafeStoragePath("Storage entry changed before deletion")
        return quarantine
    raise MediaStorageError("Could not allocate a private deletion quarantine")


def _quarantine_and_destroy(
    parent_fd: int,
    name: str,
    *,
    expected: _FileIdentity,
) -> None:
    """Destroy bytes through the exact FD before detaching the public name.

    Truncating before rename means a crash leaves either the public zero-byte
    entry or a zero-byte tombstone, never a hidden tombstone with payload bytes.
    """

    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _identity(current) != expected:
        raise UnsafeStoragePath("Storage entry changed before byte removal")
    if stat.S_ISLNK(current.st_mode):
        _quarantine_entry(parent_fd, name, expected=expected)
        os.fsync(parent_fd)
        return
    if not stat.S_ISREG(current.st_mode):
        raise UnsafeStoragePath("Storage entry is not a removable file")
    flags = os.O_RDWR | os.O_NOFOLLOW
    quarantine = _allocate_quarantine_name(parent_fd)
    file_fd: int | None = None
    try:
        file_fd = os.open(name, flags, dir_fd=parent_fd)
        descriptor_metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(descriptor_metadata.st_mode)
            or _identity(descriptor_metadata) != expected
        ):
            raise UnsafeStoragePath("Storage file changed before byte removal")
        if descriptor_metadata.st_nlink != 1:
            raise UnsafeStoragePath("Storage file has multiple hard links")
        os.ftruncate(file_fd, 0)
        os.fsync(file_fd)
    except OSError as error:
        if file_fd is None:
            raise UnsafeStoragePath(
                "Storage file could not be opened safely"
            ) from error
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)
    _quarantine_entry(
        parent_fd,
        name,
        expected=expected,
        initial_quarantine=quarantine,
    )
    os.fsync(parent_fd)


def _restore_quarantine(
    parent_fd: int,
    quarantine: str,
    original: str,
    *,
    expected: _FileIdentity,
) -> None:
    """Best-effort restoration that never replaces a concurrently created name."""

    current = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    if _identity(current) != expected:
        raise UnsafeStoragePath("Quarantined entry changed before restoration")
    _rename_noreplace(parent_fd, quarantine, original)
    os.fsync(parent_fd)


def _identity_bytes(identity: _FileIdentity) -> bytes:
    _device, inode = identity
    return f"{inode:x}".encode("ascii")


def _root_marker_content(identity: _FileIdentity) -> bytes:
    return _ROOT_MARKER_PREFIX + _identity_bytes(identity) + b"\n"


def _case_marker_content(storage_name: str, identity: _FileIdentity) -> bytes:
    return (
        _CASE_MARKER_PREFIX
        + storage_name.encode("ascii")
        + b"\0"
        + _identity_bytes(identity)
        + b"\n"
    )


def _validate_marker(
    directory_fd: int,
    name: str,
    expected: bytes,
    *,
    invalid_message: str,
) -> None:
    try:
        marker_fd = os.open(name, _file_open_flags(), dir_fd=directory_fd)
    except OSError as error:
        raise UnsafeStoragePath(invalid_message) from error
    try:
        metadata = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != len(expected)
            or _read_fd(marker_fd) != expected
        ):
            raise UnsafeStoragePath(invalid_message)
    finally:
        os.close(marker_fd)


def _write_marker(
    directory_fd: int,
    name: str,
    content: bytes,
    *,
    create_message: str,
    persist_message: str,
) -> _FileIdentity:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        marker_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as error:
        raise UnsafeStoragePath(create_message) from error
    marker_identity: _FileIdentity | None = None
    try:
        try:
            marker_identity = _identity(os.fstat(marker_fd))
            view = memoryview(content)
            written = 0
            while written < len(view):
                count = os.write(marker_fd, view[written:])
                if count == 0:
                    raise MediaStorageError(persist_message)
                written += count
            os.fsync(marker_fd)
            os.fchmod(marker_fd, 0o600)
            os.fsync(directory_fd)
        finally:
            os.close(marker_fd)
    except BaseException:
        if marker_identity is not None:
            with suppress(OSError, UnsafeStoragePath, MediaStorageError):
                _quarantine_and_destroy(
                    directory_fd,
                    name,
                    expected=marker_identity,
                )
        raise
    assert marker_identity is not None
    return marker_identity


def _open_or_create_media_root(path: Path) -> tuple[int, bool]:
    """Publish a fully initialized root with one atomic no-replace rename."""

    if path == Path(path.anchor):
        raise UnsafeStoragePath("Filesystem root cannot be used as media storage")
    parent_fd, _parent_created = _open_or_create_absolute_directory(path.parent)
    try:
        try:
            root_fd = os.open(
                path.name,
                _directory_open_flags(),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            pass
        except OSError as error:
            raise UnsafeStoragePath("Media root could not be opened safely") from error
        else:
            return root_fd, False

        _require_entry_capacity(
            parent_fd,
            prefix=_CREATION_PREFIX,
            limit=_MAX_CREATION_STAGING_PER_PARENT,
            message="Media root staging limit reached; run the explicit local reset",
        )
        for _ in range(16):
            temporary_name = f"{_CREATION_PREFIX}{secrets.token_hex(16)}"
            try:
                os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            try:
                temporary_fd = os.open(
                    temporary_name,
                    _directory_open_flags(),
                    dir_fd=parent_fd,
                )
            except OSError as error:
                raise UnsafeStoragePath(
                    "Temporary media root could not be pinned safely"
                ) from error
            try:
                temporary_identity = _identity(os.fstat(temporary_fd))
                os.fchmod(temporary_fd, 0o700)
                _write_marker(
                    temporary_fd,
                    _ROOT_MARKER,
                    _root_marker_content(temporary_identity),
                    create_message="Media root ownership marker could not be created",
                    persist_message="Could not persist media root marker",
                )
                try:
                    _rename_noreplace(parent_fd, temporary_name, path.name)
                except FileExistsError:
                    try:
                        existing_fd = os.open(
                            path.name,
                            _directory_open_flags(),
                            dir_fd=parent_fd,
                        )
                    except OSError as error:
                        raise UnsafeStoragePath(
                            "Concurrent media root could not be opened safely"
                        ) from error
                    descriptor_to_close = temporary_fd
                    temporary_fd = -1
                    try:
                        os.close(descriptor_to_close)
                    except OSError:
                        os.close(existing_fd)
                        raise
                    return existing_fd, False
                published = os.stat(
                    path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(published.st_mode)
                    or _identity(published) != temporary_identity
                ):
                    raise UnsafeStoragePath(
                        "Media root changed while it was published"
                    )
                os.fsync(parent_fd)
                return temporary_fd, True
            except BaseException:
                if temporary_fd >= 0:
                    with suppress(OSError):
                        os.close(temporary_fd)
                raise
        raise MediaStorageError("Could not allocate a private media root")
    finally:
        os.close(parent_fd)


class CaseMediaStore:
    """Own a pinned media root and perform every mutation relative to its FD.

    The V2 markers bind accidental/replacement copies to the directory inode;
    they are not cryptographic authentication against another writer running
    as the same OS user.  The configured root therefore has one exclusive
    application writer within that same-user trust boundary.  Cross-filesystem
    copies/restores or filesystems without stable inode identities require the
    explicit local reset. Sanitized tombstones are retained to avoid unsafe
    name-based unlink races, bounded per directory, and removed by that reset.

    ``path_for`` remains a compatibility/diagnostic view for code that needs a
    local ``Path``. Authority checks and media reads must use ``read_bytes``:
    no path returned to a caller can stay race-free after this method returns.
    """

    def __init__(self, root: Path) -> None:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise MediaStorageError(
                "Media storage requires O_DIRECTORY and O_NOFOLLOW support"
            )
        _rename_noreplace_api()
        if type(root) is not Path:
            root = Path(root)
        absolute = Path(os.path.abspath(root))
        root_fd, root_created = _open_or_create_media_root(absolute)
        root_metadata: os.stat_result | None = None
        try:
            root_metadata = os.fstat(root_fd)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise UnsafeStoragePath("Media root must be a directory")
            self._initialize_marker(root_fd, root_created=root_created)
            os.fchmod(root_fd, 0o700)
        except BaseException:
            os.close(root_fd)
            raise

        assert root_metadata is not None
        self._root = absolute
        self._root_fd = root_fd
        self._root_identity = _identity(root_metadata)
        self._case_identities: dict[str, _FileIdentity] = {}
        self._lock = RLock()
        self._closed = False

    @staticmethod
    def _initialize_marker(root_fd: int, *, root_created: bool) -> None:
        expected_content = _root_marker_content(_identity(os.fstat(root_fd)))
        names = set(os.listdir(root_fd))
        if _LEGACY_ROOT_MARKER in names:
            raise UnsafeStoragePath(
                "Legacy V1 media root requires an explicit reset before V2 use"
            )
        if _ROOT_MARKER not in names:
            raise UnsafeStoragePath("Refusing to claim a pre-existing unowned media root")
        _validate_marker(
            root_fd,
            _ROOT_MARKER,
            expected_content,
            invalid_message="Media root ownership marker is invalid",
        )
        names = set(os.listdir(root_fd))
        if root_created and names != {_ROOT_MARKER}:
            raise UnsafeStoragePath("Media root changed while ownership was claimed")
        for storage_name in names - {_ROOT_MARKER}:
            if storage_name.startswith(_TOMBSTONE_PREFIX):
                CaseMediaStore._validate_tombstone(root_fd, storage_name)
                continue
            if _CASE_NAME.fullmatch(storage_name) is None:
                raise UnsafeStoragePath("Media root contains an unowned entry")
            try:
                metadata = os.stat(
                    storage_name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(metadata.st_mode):
                    raise UnsafeStoragePath("Owned case entry is not a directory")
                directory_fd = os.open(
                    storage_name,
                    _directory_open_flags(),
                    dir_fd=root_fd,
                )
            except OSError as error:
                raise UnsafeStoragePath(
                    "Owned case entry could not be opened safely"
                ) from error
            try:
                directory_identity = _identity(os.fstat(directory_fd))
                if directory_identity != _identity(metadata):
                    raise UnsafeStoragePath(
                        "Owned case entry changed during root validation"
                    )
                CaseMediaStore._validate_case_marker(directory_fd, storage_name)
                CaseMediaStore._validate_reopened_case_contents(directory_fd)
            finally:
                os.close(directory_fd)

    @staticmethod
    def _validate_reopened_case_contents(directory_fd: int) -> None:
        for name in os.listdir(directory_fd):
            if name == _CASE_MARKER:
                continue
            if name.startswith(_TOMBSTONE_PREFIX):
                CaseMediaStore._validate_tombstone(directory_fd, name)
                continue
            if _FILE_NAME.fullmatch(name) is None:
                raise UnsafeStoragePath("Case directory contains an unowned entry")
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise UnsafeStoragePath("Persisted case asset is not an owned regular file")

    @staticmethod
    def _validate_tombstone(
        parent_fd: int,
        name: str,
        *,
        depth: int = 0,
    ) -> None:
        if depth > _MAX_TOMBSTONE_DEPTH:
            raise UnsafeStoragePath("Media tombstone nesting is too deep")
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        expected = _identity(metadata)
        if stat.S_ISLNK(metadata.st_mode):
            return
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_size != 0 or metadata.st_nlink != 1:
                raise UnsafeStoragePath(
                    "Unsanitized media tombstone requires an explicit local reset"
                )
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeStoragePath("Media tombstone has an unsupported type")
        try:
            directory_fd = os.open(
                name,
                _directory_open_flags(),
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise UnsafeStoragePath("Media tombstone could not be opened safely") from error
        try:
            if _identity(os.fstat(directory_fd)) != expected:
                raise UnsafeStoragePath("Media tombstone changed during validation")
            for child in os.listdir(directory_fd):
                if child == _CASE_MARKER:
                    CaseMediaStore._validate_detached_case_marker(directory_fd)
                elif child.startswith(_TOMBSTONE_PREFIX):
                    CaseMediaStore._validate_tombstone(
                        directory_fd,
                        child,
                        depth=depth + 1,
                    )
                else:
                    raise UnsafeStoragePath(
                        "Unsanitized media tombstone requires an explicit local reset"
                    )
        finally:
            os.close(directory_fd)

    @staticmethod
    def _validate_detached_case_marker(directory_fd: int) -> None:
        try:
            marker_fd = os.open(_CASE_MARKER, _file_open_flags(), dir_fd=directory_fd)
        except OSError as error:
            raise UnsafeStoragePath("Detached case marker is unsafe") from error
        try:
            metadata = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > 256
            ):
                raise UnsafeStoragePath("Detached case marker is invalid")
            content = _read_fd(marker_fd)
        finally:
            os.close(marker_fd)
        if not content.startswith(_CASE_MARKER_PREFIX) or not content.endswith(b"\n"):
            raise UnsafeStoragePath("Detached case marker is invalid")
        payload = content[len(_CASE_MARKER_PREFIX) : -1]
        parts = payload.split(b"\0")
        if len(parts) != 2:
            raise UnsafeStoragePath("Detached case marker is invalid")
        try:
            storage_name = parts[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise UnsafeStoragePath("Detached case marker is invalid") from error
        if _CASE_NAME.fullmatch(storage_name) is None:
            raise UnsafeStoragePath("Detached case marker is invalid")
        expected = _case_marker_content(
            storage_name,
            _identity(os.fstat(directory_fd)),
        )
        if content != expected:
            raise UnsafeStoragePath("Detached case marker is invalid")

    @property
    @_serialized
    def root(self) -> Path:
        self._require_root_identity()
        return self._root

    @_serialized
    def close(self) -> None:
        """Release the pinned root FD; repeated calls are harmless."""

        if getattr(self, "_closed", True):
            return
        self._closed = True
        root_fd = self._root_fd
        self._root_fd = -1
        self._case_identities.clear()
        os.close(root_fd)

    def __enter__(self) -> Self:
        with self._lock:
            self._require_root_identity()
            return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    @_serialized
    def create_case(self) -> CaseHandle:
        self._require_root_identity()
        for _ in range(16):
            _require_entry_capacity(
                self._root_fd,
                prefix=_TOMBSTONE_PREFIX,
                limit=_MAX_TOMBSTONES_PER_DIRECTORY,
                message="Media tombstone limit reached; run the explicit local reset",
            )
            storage_name = f"case-{secrets.token_hex(16)}"
            staging_name = f"{_TOMBSTONE_PREFIX}{secrets.token_hex(16)}"
            try:
                os.mkdir(staging_name, mode=0o700, dir_fd=self._root_fd)
            except FileExistsError:
                continue
            try:
                directory_fd = os.open(
                    staging_name,
                    _directory_open_flags(),
                    dir_fd=self._root_fd,
                )
            except OSError as error:
                raise UnsafeStoragePath(
                    "Staged case directory could not be pinned safely"
                ) from error
            directory_identity = _identity(os.fstat(directory_fd))
            try:
                os.fchmod(directory_fd, 0o700)
                self._write_case_marker(directory_fd, storage_name)
                os.fsync(directory_fd)
                try:
                    _rename_noreplace(self._root_fd, staging_name, storage_name)
                except FileExistsError:
                    continue
                published = os.stat(
                    storage_name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(published.st_mode)
                    or _identity(published) != directory_identity
                ):
                    raise UnsafeStoragePath(
                        "Case directory changed while it was published"
                    )
                os.fsync(self._root_fd)
                self._case_identities[storage_name] = directory_identity
                return CaseHandle(storage_name=storage_name)
            finally:
                os.close(directory_fd)
        raise MediaStorageError("Could not allocate a unique case directory")

    @_serialized
    def write_bytes(
        self,
        handle: CaseHandle,
        content: bytes,
        *,
        role: AssetRole,
        suffix: str,
        media_type: str,
    ) -> StoredAssetRef:
        if type(content) is not bytes:
            raise TypeError("Stored media content must be immutable bytes")
        if role not in _ASSET_ROLES:
            raise UnsafeStoragePath("Asset role is not allowed")
        if _SUFFIX.fullmatch(suffix) is None:
            raise UnsafeStoragePath("Asset suffix is not allowed")
        directory_fd = self._open_case_directory(handle)
        try:
            for _ in range(16):
                file_id = f"{role}-{secrets.token_hex(16)}{suffix}"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    file_fd = os.open(file_id, flags, 0o600, dir_fd=directory_fd)
                except FileExistsError:
                    continue
                try:
                    metadata: os.stat_result | None = None
                    metadata = os.fstat(file_fd)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise UnsafeStoragePath("Media target must be a regular file")
                    view = memoryview(content)
                    written = 0
                    while written < len(view):
                        count = os.write(file_fd, view[written:])
                        if count == 0:
                            raise MediaStorageError(
                                "Could not persist complete media bytes"
                            )
                        written += count
                    os.fsync(file_fd)
                    os.fchmod(file_fd, 0o600)
                except BaseException:
                    if metadata is not None:
                        with suppress(
                            OSError,
                            UnsafeStoragePath,
                            MediaStorageError,
                        ):
                            _quarantine_and_destroy(
                                directory_fd,
                                file_id,
                                expected=_identity(metadata),
                            )
                    raise
                finally:
                    os.close(file_fd)
                try:
                    os.fsync(directory_fd)
                except OSError:
                    assert metadata is not None
                    with suppress(
                        OSError,
                        UnsafeStoragePath,
                        MediaStorageError,
                    ):
                        _quarantine_and_destroy(
                            directory_fd,
                            file_id,
                            expected=_identity(metadata),
                        )
                    raise
                return StoredAssetRef(
                    file_id=file_id,
                    media_type=media_type,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
        finally:
            os.close(directory_fd)
        raise MediaStorageError("Could not allocate a unique media filename")

    @_serialized
    def read_bytes(self, handle: CaseHandle, asset: StoredAssetRef) -> bytes:
        self._validate_asset(asset)
        directory_fd = self._open_case_directory(handle)
        try:
            try:
                file_fd = os.open(asset.file_id, _file_open_flags(), dir_fd=directory_fd)
            except FileNotFoundError as error:
                raise MediaStorageError("Stored media file is missing") from error
            except OSError as error:
                raise UnsafeStoragePath(
                    "Stored media file could not be opened safely"
                ) from error
            try:
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeStoragePath(
                        "Stored media reference must be a regular non-symlink file"
                    )
                content = _read_fd(file_fd)
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)
        if hashlib.sha256(content).hexdigest() != asset.sha256:
            raise MediaStorageError("Stored media digest does not match its immutable reference")
        return content

    @_serialized
    def path_for(self, handle: CaseHandle, asset: StoredAssetRef) -> Path:
        """Return a verified compatibility path, never an authority-bearing handle."""

        self._validate_asset(asset)
        directory_fd = self._open_case_directory(handle)
        try:
            try:
                file_fd = os.open(asset.file_id, _file_open_flags(), dir_fd=directory_fd)
            except FileNotFoundError as error:
                raise MediaStorageError("Stored media file is missing") from error
            except OSError as error:
                raise UnsafeStoragePath(
                    "Stored media file could not be opened safely"
                ) from error
            try:
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeStoragePath(
                        "Stored media reference must be a regular non-symlink file"
                    )
                file_identity = _identity(metadata)
            finally:
                os.close(file_fd)
            case_identity = _identity(os.fstat(directory_fd))
        finally:
            os.close(directory_fd)

        path = self._root / handle.storage_name / asset.file_id
        try:
            case_metadata = os.stat(path.parent, follow_symlinks=False)
            path_metadata = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise UnsafeStoragePath("Compatibility media path is no longer stable") from error
        if (
            not stat.S_ISDIR(case_metadata.st_mode)
            or _identity(case_metadata) != case_identity
            or not stat.S_ISREG(path_metadata.st_mode)
            or _identity(path_metadata) != file_identity
        ):
            raise UnsafeStoragePath("Compatibility media path changed during verification")
        self._require_root_identity()
        return path

    @_serialized
    def delete_asset(self, handle: CaseHandle, asset: StoredAssetRef) -> bool:
        self._validate_asset(asset)
        directory_fd = self._open_case_directory(handle)
        try:
            try:
                file_fd = os.open(
                    asset.file_id,
                    _file_open_flags(),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return False
            except OSError as error:
                raise UnsafeStoragePath(
                    "Stored media file could not be opened safely"
                ) from error
            try:
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeStoragePath(
                        "Stored media reference must be a regular non-symlink file"
                    )
                content = _read_fd(file_fd)
            finally:
                os.close(file_fd)
            if hashlib.sha256(content).hexdigest() != asset.sha256:
                raise MediaStorageError(
                    "Stored media digest does not match its immutable reference"
                )
            _quarantine_and_destroy(
                directory_fd,
                asset.file_id,
                expected=_identity(metadata),
            )
            return True
        finally:
            os.close(directory_fd)

    @_serialized
    def delete_case(self, handle: CaseHandle) -> bool:
        storage_name = self._storage_name(handle)
        self._require_root_identity()
        try:
            metadata = os.stat(
                storage_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeStoragePath(
                "Case path is not an authority-marked owned directory"
            )
        self._delete_case_directory(storage_name, expected=_identity(metadata))
        return True

    @_serialized
    def reset(self) -> int:
        self._require_root_identity()
        removed = 0
        for name in tuple(os.listdir(self._root_fd)):
            if _CASE_NAME.fullmatch(name) is None:
                continue
            try:
                metadata = os.stat(
                    name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                self._delete_case_directory(name, expected=_identity(metadata))
            else:
                raise UnsafeStoragePath(
                    "Reset target is not an authority-marked owned directory"
                )
            removed += 1
        return removed

    def _delete_case_directory(
        self,
        storage_name: str,
        *,
        expected: _FileIdentity,
    ) -> None:
        directory_fd = self._open_case_name(
            storage_name,
            remember=False,
            require_marker=True,
        )
        try:
            if _identity(os.fstat(directory_fd)) != expected:
                raise UnsafeStoragePath("Case directory changed before deletion")
            self._clear_directory(directory_fd, preserve_name=_CASE_MARKER)
            current = os.stat(
                storage_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(current.st_mode)
                or _identity(current) != expected
            ):
                raise UnsafeStoragePath("Case directory changed during deletion")
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        quarantine = _quarantine_entry(
            self._root_fd,
            storage_name,
            expected=expected,
        )
        try:
            os.fsync(self._root_fd)
        except OSError:
            with suppress(
                OSError,
                UnsafeStoragePath,
                MediaStorageError,
            ):
                _restore_quarantine(
                    self._root_fd,
                    quarantine,
                    storage_name,
                    expected=expected,
                )
            raise
        self._case_identities.pop(storage_name, None)

    def _clear_directory(
        self,
        directory_fd: int,
        *,
        preserve_name: str | None = None,
    ) -> None:
        names = tuple(os.listdir(directory_fd))
        ordered_names = tuple(name for name in names if name != preserve_name)
        for name in ordered_names:
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                expected = _identity(metadata)
                try:
                    child_fd = os.open(
                        name,
                        _directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise UnsafeStoragePath(
                        "Nested media directory could not be opened safely"
                    ) from error
                try:
                    child_metadata = os.fstat(child_fd)
                    child_identity = _identity(child_metadata)
                    if (
                        not stat.S_ISDIR(child_metadata.st_mode)
                        or child_identity != expected
                    ):
                        raise UnsafeStoragePath(
                            "Nested media directory changed before deletion"
                        )
                    self._clear_directory(child_fd)
                    current = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(current.st_mode)
                        or _identity(current) != child_identity
                    ):
                        raise UnsafeStoragePath(
                            "Nested media directory changed during deletion"
                        )
                    os.fsync(child_fd)
                finally:
                    os.close(child_fd)
                quarantine = _quarantine_entry(
                    directory_fd,
                    name,
                    expected=expected,
                )
                try:
                    os.fsync(directory_fd)
                except OSError:
                    with suppress(
                        OSError,
                        UnsafeStoragePath,
                        MediaStorageError,
                    ):
                        _restore_quarantine(
                            directory_fd,
                            quarantine,
                            name,
                            expected=expected,
                        )
                    raise
            elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                _quarantine_and_destroy(
                    directory_fd,
                    name,
                    expected=_identity(metadata),
                )
            else:
                raise UnsafeStoragePath(
                    "Media directory contains an unsupported filesystem type"
                )

    def _open_case_directory(self, handle: CaseHandle) -> int:
        return self._open_case_name(
            self._storage_name(handle),
            remember=True,
            require_marker=True,
        )

    def _open_case_name(
        self,
        storage_name: str,
        *,
        remember: bool,
        require_marker: bool,
    ) -> int:
        self._require_root_identity()
        try:
            directory_fd = os.open(
                storage_name,
                _directory_open_flags(),
                dir_fd=self._root_fd,
            )
        except FileNotFoundError as error:
            raise MediaStorageError("Case media directory is missing") from error
        except OSError as error:
            raise UnsafeStoragePath(
                "Case media directory could not be opened safely"
            ) from error
        try:
            metadata = os.fstat(directory_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsafeStoragePath("Case media directory must be a directory")
            identity = _identity(metadata)
            expected = self._case_identities.get(storage_name)
            if expected is not None and identity != expected:
                raise UnsafeStoragePath("Case media directory identity changed")
            if require_marker:
                self._validate_case_marker(directory_fd, storage_name)
            if remember and expected is None:
                self._case_identities[storage_name] = identity
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise

    @staticmethod
    def _validate_case_marker(directory_fd: int, storage_name: str) -> None:
        expected = _case_marker_content(
            storage_name,
            _identity(os.fstat(directory_fd)),
        )
        try:
            marker_fd = os.open(_CASE_MARKER, _file_open_flags(), dir_fd=directory_fd)
        except OSError as error:
            raise UnsafeStoragePath("Case ownership marker is missing or unsafe") from error
        try:
            metadata = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != len(expected)
                or _read_fd(marker_fd) != expected
            ):
                raise UnsafeStoragePath("Case ownership marker is invalid")
        finally:
            os.close(marker_fd)

    @staticmethod
    def _write_case_marker(directory_fd: int, storage_name: str) -> None:
        content = _case_marker_content(
            storage_name,
            _identity(os.fstat(directory_fd)),
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            marker_fd = os.open(_CASE_MARKER, flags, 0o600, dir_fd=directory_fd)
        except OSError as error:
            raise UnsafeStoragePath("Case ownership marker could not be created") from error
        marker_identity: _FileIdentity | None = None
        try:
            try:
                marker_identity = _identity(os.fstat(marker_fd))
                view = memoryview(content)
                written = 0
                while written < len(view):
                    count = os.write(marker_fd, view[written:])
                    if count == 0:
                        raise MediaStorageError("Could not persist case ownership marker")
                    written += count
                os.fsync(marker_fd)
                os.fchmod(marker_fd, 0o600)
            finally:
                os.close(marker_fd)
        except BaseException:
            if marker_identity is not None:
                with suppress(OSError, UnsafeStoragePath, MediaStorageError):
                    _quarantine_and_destroy(
                        directory_fd,
                        _CASE_MARKER,
                        expected=marker_identity,
                    )
            raise

    def _storage_name(self, handle: CaseHandle) -> str:
        if not isinstance(handle, CaseHandle):
            raise UnsafeStoragePath("Case handle is not canonical")
        storage_name = handle.storage_name
        if type(storage_name) is not str or _CASE_NAME.fullmatch(storage_name) is None:
            raise UnsafeStoragePath("Case handle is not an owned normalized directory name")
        return storage_name

    @staticmethod
    def _validate_asset(asset: StoredAssetRef) -> None:
        if not isinstance(asset, StoredAssetRef):
            raise UnsafeStoragePath("Asset reference is not canonical")
        if _FILE_NAME.fullmatch(asset.file_id) is None:
            raise UnsafeStoragePath("Asset reference is not an owned normalized filename")

    def _require_root_identity(self) -> None:
        if getattr(self, "_closed", True) or self._root_fd < 0:
            raise MediaStorageError("Media store is closed")
        try:
            descriptor_metadata = os.fstat(self._root_fd)
            configured_fd = _open_absolute_directory(self._root)
        except (OSError, UnsafeStoragePath) as error:
            raise UnsafeStoragePath("Configured media root identity changed") from error
        try:
            configured_metadata = os.fstat(configured_fd)
        finally:
            os.close(configured_fd)
        if (
            not stat.S_ISDIR(descriptor_metadata.st_mode)
            or not stat.S_ISDIR(configured_metadata.st_mode)
            or _identity(descriptor_metadata) != self._root_identity
            or _identity(configured_metadata) != self._root_identity
        ):
            raise UnsafeStoragePath("Configured media root identity changed")
