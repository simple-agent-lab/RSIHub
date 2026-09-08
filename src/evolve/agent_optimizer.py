"""Immutable research-method bundles and exact per-attempt controller inputs."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path("runs/agent-driven")


def _files(directory: Path) -> dict[str, bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("optimizer must be a real directory")
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("optimizer symlinks are not allowed")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise RuntimeError("optimizer contains a non-regular file")
    if not files.get("instructions.md", b"").strip():
        raise RuntimeError("optimizer requires non-empty instructions.md")
    for name, data in files.items():
        if name.endswith(".py"):
            try:
                compile(data, name, "exec")
            except (SyntaxError, ValueError) as exc:
                raise RuntimeError(f"invalid optimizer Python: {name}") from exc
    return files


def _identity(files: dict[str, bytes]) -> tuple[str, dict[str, str]]:
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return digest, hashes


def freeze_optimizer(workspace: Path, source: Path) -> dict[str, Any]:
    """Snapshot bytes before publishing an event; orphan snapshots never activate."""
    files = _files(source)
    digest, hashes = _identity(files)
    parent = workspace / ROOT / "optimizer/versions"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / digest
    if destination.exists():
        if _identity(_files(destination))[0] != digest:
            raise RuntimeError("optimizer snapshot was modified")
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        try:
            for name, data in files.items():
                path = temporary / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {"digest": digest, "files": hashes}


def verify_optimizer(workspace: Path, optimizer: dict[str, Any]) -> Path:
    digest = optimizer.get("digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RuntimeError("invalid optimizer digest")
    path = workspace / ROOT / "optimizer/versions" / digest
    actual, hashes = _identity(_files(path))
    if actual != digest or hashes != optimizer.get("files"):
        raise RuntimeError("optimizer snapshot was modified")
    return path


def draft_source(workspace: Path, relative: str) -> Path:
    base = (workspace / ROOT / "optimizer/drafts").resolve()
    path = workspace / relative
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        raise RuntimeError("optimizer draft must be inside runs/agent-driven/optimizer/drafts")
    return path


def controller_input(workspace: Path, state: dict[str, Any], attempt_dir: Path) -> Path | None:
    if state.get("mode") != "continuous":
        return None
    optimizer = state["research"]["active_optimizer"]
    path = verify_optimizer(workspace, optimizer)
    payload = {
        "schema_version": 1,
        "session_id": state["session_id"],
        "optimizer": optimizer,
        "optimizer_path": str(path),
        "research_workspace": str(workspace / ROOT / "notes"),
        "objective": state["research"]["objective"],
    }
    target = attempt_dir / "controller-input.json"
    target.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return target


def validate_load(input_path: Path, load_path: Path) -> None:
    expected = json.loads(input_path.read_text())["optimizer"]
    try:
        actual = json.loads(load_path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("controller did not receipt the loaded optimizer") from exc
    if actual != expected:
        raise RuntimeError("controller loaded a different optimizer")
