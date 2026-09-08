"""Target-only, digest-pinned candidate delivery independent of the research repo."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..git import git_stdout
from ..runtime.files import read_regular_file


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _publish(destination: Path, files: dict[str, tuple[bytes, int]]) -> None:
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("candidate package destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".candidate-", dir=destination.parent))
    try:
        for name, (data, mode) in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def export_candidate(workspace: Path, reference: str, destination: Path) -> dict[str, Any]:
    """Export committed target bytes, never a working tree, Git metadata or runs/."""
    workspace = workspace.resolve()
    commit = git_stdout(workspace, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}")
    tree = git_stdout(workspace, "rev-parse", f"{commit}:target")
    result = subprocess.run(["git", "-C", str(workspace), "ls-tree", "-r", "-z", tree], capture_output=True, check=True)
    files: dict[str, tuple[bytes, int]] = {}
    entries = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise RuntimeError("candidate export rejects symlinks, submodules and special files")
        name = raw_name.decode("utf-8")
        _validate_name(name)
        data = subprocess.run(
            ["git", "-C", str(workspace), "cat-file", "blob", object_id.decode("ascii")],
            capture_output=True,
            check=True,
        ).stdout
        executable = mode == b"100755"
        files[f"target/{name}"] = (data, 0o700 if executable else 0o600)
        entries[name] = {"sha256": _digest(data), "size": len(data), "executable": executable}
    if not entries:
        raise RuntimeError("candidate export requires a non-empty target")
    manifest = {"schema_version": 1, "candidate_commit": commit, "target_tree": tree, "files": entries}
    encoded = _encoded(manifest)
    files["manifest.json"] = (encoded, 0o600)
    _publish(destination, files)
    return {"path": str(destination.resolve()), "sha256": _digest(encoded), **manifest}


def _validate_name(name: object) -> None:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or "\0" in name
        or PurePosixPath(name).is_absolute()
        or str(PurePosixPath(name)) != name
        or any(part in {"..", ".git"} for part in PurePosixPath(name).parts)
    ):
        raise RuntimeError("invalid candidate package member path")


def materialize_candidate(package: Path, digest: str, destination: Path) -> dict[str, Any]:
    """Copy the exact verified bytes; expected digest comes from the trusted caller."""
    if package.is_symlink() or not package.is_dir():
        raise RuntimeError("candidate package must be a real directory")
    if not isinstance(digest, str) or len(digest) != 64:
        raise RuntimeError("candidate package requires an expected SHA-256")
    encoded = read_regular_file(package, "manifest.json")
    if _digest(encoded) != digest:
        raise RuntimeError("candidate package manifest digest mismatch")
    manifest = json.loads(encoded)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "candidate_commit", "target_tree", "files"}
        or manifest["schema_version"] != 1
        or not isinstance(manifest["files"], dict)
        or not manifest["files"]
    ):
        raise RuntimeError("invalid candidate package manifest")
    expected = {"manifest.json"}
    files = {}
    for name, entry in manifest["files"].items():
        _validate_name(name)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "size", "executable"}
            or not isinstance(entry["executable"], bool)
            or type(entry["size"]) is not int
            or entry["size"] < 0
        ):
            raise RuntimeError("invalid candidate package file entry")
        relative = f"target/{name}"
        expected.add(relative)
        data = read_regular_file(package, relative)
        if len(data) != entry["size"] or _digest(data) != entry["sha256"]:
            raise RuntimeError("candidate package file digest mismatch")
        files[name] = (data, 0o700 if entry["executable"] else 0o600)
    actual = set()
    for path in package.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("candidate package contains a symlink")
        if not path.is_dir():
            actual.add(path.relative_to(package).as_posix())
    if actual != expected:
        raise RuntimeError("candidate package contains unlisted or missing files")
    _publish(destination, files)
    return manifest
