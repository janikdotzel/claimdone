"""Symlink-safe local storage for temporary case media."""

import hashlib
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import Literal

from .types import CaseHandle, StoredAssetRef

AssetRole = Literal["source", "model", "text", "audio", "transcript", "temp"]
_ASSET_ROLES = frozenset({"source", "model", "text", "audio", "transcript", "temp"})

_CASE_NAME = re.compile(r"^case-[a-f0-9]{32}$")
_FILE_NAME = re.compile(
    r"^(?:source|model|text|audio|transcript|temp)-[a-f0-9]{32}\.(?:jpg|png|wav|txt|bin)$"
)
_SUFFIX = re.compile(r"^\.(?:jpg|png|wav|txt|bin)$")
_ROOT_MARKER = ".claimdone-media-root-v1"
_ROOT_MARKER_CONTENT = b"ClaimDone temporary media root v1\n"


class UnsafeStoragePath(ValueError):
    """Raised when a caller supplies a non-owned or unsafe storage reference."""


class MediaStorageError(RuntimeError):
    """Raised when stored bytes are missing, altered, or not regular files."""


class CaseMediaStore:
    """Own a single local root without ever deriving paths from user filenames."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise UnsafeStoragePath("Media root must not be a symlink")
        created = False
        try:
            root.mkdir(mode=0o700, parents=True)
            created = True
        except FileExistsError:
            pass
        self._root = root.resolve(strict=True)
        root_metadata = self._root.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise UnsafeStoragePath("Media root must be a non-symlink directory")
        marker = self._root / _ROOT_MARKER
        if created:
            marker.write_bytes(_ROOT_MARKER_CONTENT)
            os.chmod(marker, 0o600)
        elif marker.exists():
            marker_metadata = marker.lstat()
            if (
                stat.S_ISLNK(marker_metadata.st_mode)
                or not stat.S_ISREG(marker_metadata.st_mode)
                or marker.read_bytes() != _ROOT_MARKER_CONTENT
            ):
                raise UnsafeStoragePath("Media root ownership marker is invalid")
        elif any(self._root.iterdir()):
            raise UnsafeStoragePath("Refusing to claim a non-empty unowned media root")
        else:
            marker.write_bytes(_ROOT_MARKER_CONTENT)
            os.chmod(marker, 0o600)
        os.chmod(self._root, 0o700)

    @property
    def root(self) -> Path:
        return self._root

    def create_case(self) -> CaseHandle:
        for _ in range(16):
            storage_name = f"case-{secrets.token_hex(16)}"
            path = self._root / storage_name
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            os.chmod(path, 0o700)
            return CaseHandle(storage_name=storage_name)
        raise MediaStorageError("Could not allocate a unique case directory")

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
        case_path = self._case_path(handle)
        directory_fd = self._open_case_directory(case_path)
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
                    view = memoryview(content)
                    written = 0
                    while written < len(view):
                        count = os.write(file_fd, view[written:])
                        if count == 0:
                            raise MediaStorageError("Could not persist complete media bytes")
                        written += count
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
                return StoredAssetRef(
                    file_id=file_id,
                    media_type=media_type,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
        finally:
            os.close(directory_fd)
        raise MediaStorageError("Could not allocate a unique media filename")

    def read_bytes(self, handle: CaseHandle, asset: StoredAssetRef) -> bytes:
        path = self.path_for(handle, asset)
        case_path = path.parent
        directory_fd = self._open_case_directory(case_path)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(path.name, flags, dir_fd=directory_fd)
            try:
                chunks: list[bytes] = []
                while chunk := os.read(file_fd, 1024 * 1024):
                    chunks.append(chunk)
                content = b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)
        if hashlib.sha256(content).hexdigest() != asset.sha256:
            raise MediaStorageError("Stored media digest does not match its immutable reference")
        return content

    def path_for(self, handle: CaseHandle, asset: StoredAssetRef) -> Path:
        if _FILE_NAME.fullmatch(asset.file_id) is None:
            raise UnsafeStoragePath("Asset reference is not an owned normalized filename")
        case_path = self._case_path(handle)
        path = case_path / asset.file_id
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise MediaStorageError("Stored media file is missing") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise UnsafeStoragePath("Stored media reference must be a regular non-symlink file")
        return path

    def delete_asset(self, handle: CaseHandle, asset: StoredAssetRef) -> bool:
        try:
            path = self.path_for(handle, asset)
        except MediaStorageError:
            return False
        directory_fd = self._open_case_directory(path.parent)
        try:
            os.unlink(path.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        return True

    def delete_case(self, handle: CaseHandle) -> bool:
        path = self._syntactic_case_path(handle)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            path.unlink()
            return True
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeStoragePath("Owned case path is not a directory")
        shutil.rmtree(path)
        return True

    def reset(self) -> int:
        removed = 0
        for path in tuple(self._root.iterdir()):
            if _CASE_NAME.fullmatch(path.name) is None:
                continue
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                path.unlink()
            elif stat.S_ISDIR(metadata.st_mode):
                shutil.rmtree(path)
            else:
                raise UnsafeStoragePath("Owned reset target has an unsupported filesystem type")
            removed += 1
        return removed

    def _case_path(self, handle: CaseHandle) -> Path:
        path = self._syntactic_case_path(handle)
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise MediaStorageError("Case media directory is missing") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeStoragePath("Case media directory must not be a symlink")
        return path

    def _syntactic_case_path(self, handle: CaseHandle) -> Path:
        if _CASE_NAME.fullmatch(handle.storage_name) is None:
            raise UnsafeStoragePath("Case handle is not an owned normalized directory name")
        path = self._root / handle.storage_name
        if path.parent != self._root:
            raise UnsafeStoragePath("Case path escaped the configured media root")
        return path

    @staticmethod
    def _open_case_directory(path: Path) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            return os.open(path, flags)
        except OSError as error:
            raise UnsafeStoragePath("Case media directory could not be opened safely") from error
