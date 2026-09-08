"""Explicit recovery of stopped Harbor jobs; never stamps an evaluation score."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from evolve.runtime.process import reserve_attempt_directory, run_owned, write_private_text


def resume_job(
    job: Path,
    *,
    config_sha256: str,
    owner_status: Path,
    harbor_command: list[str],
    cwd: Path,
    error_types: list[str],
    timeout_s: float,
) -> dict[str, Any]:
    """Back up all evidence before Harbor's destructive native resume operation.

    The caller must have stopped the original owner. The advisory lock excludes
    other recoveries through this entry; direct Harbor commands must not race it.
    Configuration identity covers the saved JSON, not mutable external resources.
    """
    job = job.resolve()
    if not job.is_dir() or not harbor_command or timeout_s <= 0:
        raise ValueError("recovery requires an existing job, Harbor command, and positive timeout")
    if not error_types or any(not value or value.startswith("-") for value in error_types):
        raise ValueError("explicit nonempty Harbor exception types are required")
    root = job.parent / f".{job.name}-recovery"
    root.mkdir(mode=0o700, exist_ok=True)
    with (root / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another recovery owns this Harbor job") from exc
        for previous in root.glob("attempt-*/receipt.json"):
            if json.loads(previous.read_text()).get("status") in {"running", "backing_up"}:
                raise RuntimeError("unresolved recovery attempt; inspect its process and evidence before retrying")
        status = json.loads(owner_status.read_text())
        if status.get("status") not in {"cancelled", "timed_out", "failed", "completed"}:
            raise ValueError("original Harbor owner has not recorded a terminal status")
        config = (job / "config.json").read_bytes()
        if hashlib.sha256(config).hexdigest() != config_sha256:
            raise ValueError("Harbor config differs from the expected digest; use a new job/revision")
        parsed = json.loads(config)
        configured_job = Path(parsed["jobs_dir"]).expanduser() / parsed["job_name"]
        if not configured_job.is_absolute():
            configured_job = cwd / configured_job
        if configured_job.resolve() != job:
            raise ValueError("saved Harbor config points at a different job directory")
        # Harbor resumes in place and can remove partial/error trial directories.
        # Refuse link-bearing trees rather than copying or following external data.
        if any(path.is_symlink() for path in job.rglob("*")):
            raise ValueError("Harbor evidence contains symlinks; inspect and preserve it manually")
        attempt = reserve_attempt_directory(root)
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "status": "backing_up",
            "job": str(job),
            "config_sha256": config_sha256,
            "error_types": error_types,
            "backup": str(attempt / "before-resume"),
            "receipt": str(attempt / "receipt.json"),
        }

        def persist() -> None:
            write_private_text(attempt / "receipt.json", json.dumps(receipt, indent=2) + "\n")

        persist()
        try:
            shutil.copytree(job, attempt / "before-resume")
            if (job / "config.json").read_bytes() != config:
                raise RuntimeError("Harbor configuration changed during backup; recovery refused")
            receipt["status"] = "running"
            persist()
            command = [*harbor_command, "jobs", "resume", "--job-path", str(job)]
            for value in error_types:
                command.extend(["--filter-error-type", value])
            result = run_owned(command, cwd=cwd, env=dict(os.environ), timeout_s=timeout_s)
            write_private_text(attempt / "stdout.log", result.stdout)
            write_private_text(attempt / "stderr.log", result.stderr)
            receipt.update(
                status="timed_out" if result.timed_out else "completed" if result.returncode == 0 else "failed",
                returncode=result.returncode,
                wall_s=result.wall_s,
            )
        except BaseException:
            receipt["status"] = "interrupted"
            persist()
            raise
        persist()
        return receipt
