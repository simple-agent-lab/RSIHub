"""Regular-file handoffs that do not follow untrusted filesystem links."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path, PurePosixPath


def read_regular_file(root: Path, name: str, *, max_bytes: int | None = None) -> bytes:
    """Open every path component without following links, including during races."""
    if not name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise RuntimeError("file handoff path escapes its tree")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RuntimeError("candidate package must contain only regular files")
            data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
            if max_bytes is not None and len(data) > max_bytes:
                raise RuntimeError("file handoff exceeds its byte limit")
            return data
    finally:
        os.close(directory)


def read_tree(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("file handoff requires a real directory")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("file handoff rejects symlinks")
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        files[name] = read_regular_file(root, name)
    return files


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    """Write a fresh host-owned tree; callers validate the destination authority."""
    root.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        path = root / name
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise RuntimeError("file handoff path escapes its tree")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def write_regular_file(root: Path, name: str, data: bytes) -> None:
    """Publish a response without following replaced output-directory links."""
    if not name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise RuntimeError("file handoff path escapes its tree")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".response-" + uuid.uuid4().hex
    created = False
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
        created = False
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def make_directories(root: Path, name: str) -> None:
    """Create nested output directories without following existing links."""
    if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise RuntimeError("directory handoff escapes its tree")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in PurePosixPath(name).parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
    finally:
        os.close(directory)
