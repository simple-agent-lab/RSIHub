"""Host-owned journal for recoverable isolated file imports and action enqueueing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .agent_driver import SESSION_DIR, AgentAction, _atomic_json, _session_lock, parse_action
from .runtime.files import read_regular_file, read_tree


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _save(path: Path, receipt: dict) -> None:
    _atomic_json(path, receipt)
    _sync(path.parent)


def _digest(path: Path) -> str:
    files = read_tree(path)
    records = [
        (name, hashlib.sha256(data).hexdigest(), name in files.executables) for name, data in sorted(files.items())
    ]
    return hashlib.sha256(json.dumps([records, sorted(files.directories)]).encode()).hexdigest()


def _path(workspace: Path, relative: str) -> Path:
    path = workspace / relative
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RuntimeError("invalid handoff journal path")
    current = workspace
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError("handoff journal path cannot contain symlinks")
    return path


def _move(source: Path, destination: Path) -> None:
    source.rename(destination)
    _sync(source.parent)
    if destination.parent != source.parent:
        _sync(destination.parent)


def _read(workspace: Path) -> dict | None:
    root = workspace / SESSION_DIR
    path = root / "return-handoff.json"
    if not path.exists():
        return None
    return json.loads(read_regular_file(root, path.name))


def assert_handoff_complete(workspace: Path) -> None:
    receipt = _read(workspace)
    if receipt and receipt["status"] != "completed":
        raise RuntimeError("isolated file handoff is incomplete; resume the controller to recover it first")


def _apply(workspace: Path, receipt: dict) -> None:
    from .agent_queue import _defer_action_locked

    for entry in receipt["transfers"]:
        destination, staging, backup = (_path(workspace, entry[key]) for key in ("destination", "staging", "backup"))
        if staging.exists():
            if _digest(staging) != entry["new_digest"]:
                raise RuntimeError("handoff staging changed; preserve journal and backups for inspection")
            if destination.exists():
                if backup.exists() or entry["old_digest"] is None or _digest(destination) != entry["old_digest"]:
                    raise RuntimeError("handoff destination changed; refusing to overwrite it")
                _move(destination, backup)
            elif entry["old_digest"] is not None and not backup.exists():
                raise RuntimeError("handoff original directory and backup are both missing")
            _move(staging, destination)
        if not destination.exists() or _digest(destination) != entry["new_digest"]:
            raise RuntimeError("handoff destination does not match the accepted files")
    _defer_action_locked(workspace, parse_action(receipt["action"]))
    _sync(workspace / SESSION_DIR)
    _save(workspace / SESSION_DIR / "return-handoff.json", {**receipt, "status": "completed"})


def recover_handoff(workspace: Path) -> bool:
    workspace = workspace.resolve()
    receipt = _read(workspace)
    if receipt is None or receipt["status"] == "completed":
        return False
    with _session_lock(workspace / SESSION_DIR):
        receipt = _read(workspace)
        if receipt is None or receipt["status"] == "completed":
            return False
        _apply(workspace, receipt)
    return True


def commit_handoff(workspace: Path, attempt: Path, staged: list[tuple[Path, Path]], action: AgentAction) -> None:
    from .agent_queue import _defer_action_locked

    workspace = workspace.resolve()
    with _session_lock(workspace / SESSION_DIR):
        assert_handoff_complete(workspace)
        _defer_action_locked(workspace, action, validate_only=True)
        transfers = []
        for index, (destination, staging) in enumerate(staged):
            _path(workspace, destination.relative_to(workspace).as_posix())
            for file in staging.rglob("*"):
                if file.is_file():
                    with file.open("rb") as stream:
                        os.fsync(stream.fileno())
            for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
                _sync(directory)
            _sync(staging)
            _sync(staging.parent)
            transfers.append(
                {
                    "destination": destination.relative_to(workspace).as_posix(),
                    "staging": staging.relative_to(workspace).as_posix(),
                    "backup": (attempt / f"previous-{index}").relative_to(workspace).as_posix(),
                    "old_digest": _digest(destination) if destination.exists() else None,
                    "new_digest": _digest(staging),
                }
            )
        receipt = {
            "schema_version": 1,
            "status": "prepared",
            "transfers": transfers,
            "action": {"id": action.action_id, "type": action.kind, **action.arguments},
        }
        _save(workspace / SESSION_DIR / "return-handoff.json", receipt)
        _apply(workspace, receipt)
