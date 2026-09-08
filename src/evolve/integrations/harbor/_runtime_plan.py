"""Shared runtime input resolution for Harbor training and canonical evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


def resolve_mounts(environment: Mapping[str, str], cache: Path) -> list[dict[str, Any]]:
    raw = environment.get("EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON")
    if raw:
        try:
            mounts = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid candidate runtime mounts JSON") from exc
        if not isinstance(mounts, list):
            raise ValueError("candidate runtime mounts must be a list of objects")
    else:
        cache = cache.expanduser().resolve()
        cache.mkdir(parents=True, exist_ok=True)
        mounts = [{"type": "bind", "source": str(cache), "target": "/opt/evolve/uv/cache"}]
    targets: set[str] = set()

    def append(mount: Any) -> None:
        if (
            not isinstance(mount, dict)
            or set(mount) - {"type", "source", "target", "read_only"}
            or mount.get("type") != "bind"
            or not isinstance(mount.get("source"), str)
            or not isinstance(mount.get("target"), str)
            or not isinstance(mount.get("read_only", False), bool)
        ):
            raise ValueError("invalid candidate runtime mount")
        target = mount["target"]
        if not Path(mount["source"]).is_absolute() or not PurePosixPath(target).is_absolute():
            raise ValueError("candidate runtime mount paths must be absolute")
        if ".." in PurePosixPath(target).parts or str(PurePosixPath(target)) != target:
            raise ValueError("candidate runtime mount target must be normalized")
        if target in targets:
            raise ValueError(f"duplicate candidate runtime mount target: {target}")
        targets.add(target)
        resolved.append(dict(mount))

    resolved: list[dict[str, Any]] = []
    for mount in mounts:
        append(mount)
    python_dir = environment.get("EVOLVE_UV_PYTHON_INSTALL_DIR")
    if python_dir:
        path = Path(python_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        append({"type": "bind", "source": str(path), "target": "/installed-agent/uv-python"})
    for variable, target in (
        ("EVOLVE_CODEX_BINARY_PATH", "/usr/local/bin/codex"),
        ("EVOLVE_CODEX_RG_PATH", "/usr/local/bin/rg"),
    ):
        source = environment.get(variable)
        if not source:
            continue
        path = Path(source).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"{variable} must name an existing absolute file")
        append({"type": "bind", "source": str(path.resolve()), "target": target, "read_only": True})
    return resolved


def write_mount_plan(run_dir: Path, mounts: list[dict[str, Any]]) -> None:
    """Retain exactly what both launch paths pass to Harbor, without credentials."""
    encoded = json.dumps(mounts, sort_keys=True, separators=(",", ":"))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "candidate-runtime.mounts.json").write_text(encoded)
    receipt = {"schema_version": 1, "mounts": mounts, "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    (run_dir / "candidate-runtime.plan.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")


def write_compose_plan(run_dir: Path, environment: Mapping[str, str]) -> list[str]:
    """Validate ordered, explicit Compose overlays and retain their input digests."""
    raw = environment.get("EVOLVE_HARBOR_EXTRA_DOCKER_COMPOSE_JSON", "[]")
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid Harbor Compose overlay JSON") from exc
    if not isinstance(configured, list):
        raise ValueError("Harbor Compose overlays must be a list of absolute file paths")
    paths: list[str] = []
    entries = []
    for value in configured:
        if not isinstance(value, str) or any(char in value for char in ("\n", "\r", "\0")):
            raise ValueError("invalid Harbor Compose overlay path")
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Harbor Compose overlay must be an existing absolute file")
        resolved = str(path.resolve())
        if resolved in paths:
            raise ValueError("duplicate Harbor Compose overlay")
        paths.append(resolved)
        entries.append({"path": resolved, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "candidate-runtime.compose-paths").write_text("".join(path + "\n" for path in paths))
    (run_dir / "candidate-runtime.compose.json").write_text(
        json.dumps({"schema_version": 1, "overlays": entries}, sort_keys=True) + "\n"
    )
    return paths
