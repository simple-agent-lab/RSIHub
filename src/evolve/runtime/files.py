"""Regular-file handoffs that do not follow untrusted filesystem links."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_TREE_ENTRIES = 4096
MAX_TREE_DEPTH = 32


class FileTree(dict[str, bytes]):
    """Validated bytes with only the executable permission bit retained."""

    def __init__(self) -> None:
        super().__init__()
        self.executables: set[str] = set()
        self.directories: set[str] = set()


def read_regular_file(root: Path, name: str, *, max_bytes: int = MAX_FILE_BYTES) -> bytes:
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
            if os.fstat(stream.fileno()).st_size > max_bytes:
                raise RuntimeError("file handoff exceeds its byte limit")
            data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise RuntimeError("file handoff exceeds its byte limit")
            return data
    finally:
        os.close(directory)


def read_tree(root: Path) -> FileTree:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("file handoff requires a real directory")
    files = FileTree()
    total = 0
    count = 0
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_TREE_ENTRIES or depth >= MAX_TREE_DEPTH:
                    raise RuntimeError("file handoff exceeds its entry or depth limit")
                if entry.is_symlink():
                    raise RuntimeError("file handoff rejects symlinks")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    files.directories.add(path.relative_to(root).as_posix())
                    pending.append((path, depth + 1))
                    continue
                name = path.relative_to(root).as_posix()
                data = read_regular_file(root, name, max_bytes=min(MAX_FILE_BYTES, MAX_TREE_BYTES - total))
                total += len(data)
                files[name] = data
                if entry.stat(follow_symlinks=False).st_mode & 0o111:
                    files.executables.add(name)
    return files


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    """Write a fresh host-owned tree; callers validate the destination authority."""
    if len(files) > MAX_TREE_ENTRIES or sum(map(len, files.values())) > MAX_TREE_BYTES:
        raise RuntimeError("file handoff exceeds its tree limit")
    for name, data in files.items():
        if len(data) > MAX_FILE_BYTES or len(PurePosixPath(name).parts) > MAX_TREE_DEPTH:
            raise RuntimeError("file handoff exceeds its file or depth limit")
    root.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        path = root / name
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise RuntimeError("file handoff path escapes its tree")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o700 if name in getattr(files, "executables", ()) else 0o600)


def write_regular_file(root: Path, name: str, data: bytes) -> None:
    """Publish a response without following replaced output-directory links."""
    if not name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise RuntimeError("file handoff path escapes its tree")
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError("file handoff exceeds its byte limit")
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
