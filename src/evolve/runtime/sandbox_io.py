"""Bounded Docker command capture and tmpfs-to-host file transport."""

from __future__ import annotations

import io
import os
import selectors
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from .files import (
    MAX_FILE_BYTES,
    MAX_TREE_BYTES,
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    FileTree,
    read_tree,
    write_regular_file,
)

ARCHIVE_LIMIT = MAX_TREE_BYTES + MAX_TREE_ENTRIES * 2048
LOG_LIMIT = 1024 * 1024
RESPONSES = ("broker/responses", "rpc/responses")


def capture(
    command: list[str], *, timeout: float = 15, limit: int = LOG_LIMIT, data: bytes | None = None
) -> tuple[int, bytes, bytes]:
    """Bound both pipes while reading; never collect arbitrary output first."""
    with tempfile.TemporaryFile() as stdin:
        if data is not None:
            stdin.write(data)
            stdin.seek(0)
        process = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                selector.register(process.stdout, selectors.EVENT_READ, stdout)
                selector.register(process.stderr, selectors.EVENT_READ, stderr)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Docker command exceeded its deadline")
                    for key, _ in selector.select(0.05):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            key.data.extend(chunk)
                            if len(stdout) + len(stderr) > limit:
                                raise RuntimeError("Docker output exceeds its byte limit")
            return process.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(stdout), bytes(stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def archive_tree(root: Path) -> bytes:
    files = read_tree(root)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        # Empty RPC/notes directories are part of the initial transport.
        directories = {"."} | files.directories
        for name in sorted(directories):
            item = tarfile.TarInfo(name)
            item.type, item.mode, item.uid, item.gid = tarfile.DIRTYPE, 0o700, os.getuid(), os.getgid()
            archive.addfile(item)
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.mode, item.uid, item.gid = (0o700 if name in files.executables else 0o600), os.getuid(), os.getgid()
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    value = stream.getvalue()
    if len(value) > ARCHIVE_LIMIT:
        raise RuntimeError("output archive exceeds its byte limit")
    return value


def unpack_tree(encoded: bytes) -> FileTree:
    files = FileTree()
    total = count = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(encoded), mode="r|") as archive:
        for member in archive:
            count += 1
            name = member.name.removeprefix("./")
            parts = PurePosixPath(name).parts
            if count > MAX_TREE_ENTRIES or len(parts) > MAX_TREE_DEPTH:
                raise RuntimeError("output archive exceeds its entry or depth limit")
            if name == "." and member.isdir():
                continue
            if (
                not parts
                or str(PurePosixPath(name)) != name
                or PurePosixPath(name).is_absolute()
                or ".." in parts
                or "\\" in name
                or name in seen
            ):
                raise RuntimeError("invalid output archive path")
            seen.add(name)
            if member.isdir():
                files.directories.add(name)
                continue
            if not member.isfile() or member.size < 0 or member.size > MAX_FILE_BYTES:
                raise RuntimeError("output archive requires bounded regular files")
            total += member.size
            if total > MAX_TREE_BYTES:
                raise RuntimeError("output archive exceeds its tree byte limit")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("missing output archive file")
            files[name] = source.read(member.size + 1)
            if len(files[name]) != member.size:
                raise RuntimeError("incomplete output archive file")
            if member.mode & 0o111:
                files.executables.add(name)
    return files


def is_response(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + "/") for prefix in RESPONSES)


def mirror_output(output: Path, files: FileTree, previous: set[str]) -> set[str]:
    """Only checked worker bytes enter the host; broker replies stay host-owned."""
    from .files import make_directories

    names = {name for name in files if not is_response(name)}
    for name in previous - names:
        (output / name).unlink(missing_ok=True)
    # Remove obsolete empty directories too, so repeated file churn cannot grow host inodes.
    directories = set(files.directories)
    for name in files:
        directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
    for path in sorted(output.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        name = path.relative_to(output).as_posix()
        if path.is_dir() and name not in directories and not is_response(name):
            try:
                path.rmdir()
            except OSError:
                pass  # Nonempty host response parents remain owned by the broker.
    for name in sorted(directories):
        if not is_response(name):
            make_directories(output, name)
    for name in names:
        make_directories(output, str(PurePosixPath(name).parent))
        write_regular_file(output, name, files[name])
        (output / name).chmod(0o700 if name in files.executables else 0o600)
    return names
