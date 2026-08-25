"""Detached candidate smoke runs for ``candidate-smoke`` and preflight ``--smoke``.

Ordinary evaluation rounds use :mod:`evolve.evaluation.execution`, not this
module.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from ..config import load_config
from ..evaluation.contract import evaluation_split_name
from ..host_runtime import clean_python_env
from ..runtime import owned_attempt_id, reserve_attempt_directory, run_owned, write_private_text
from ..runtime.environment import (
    RuntimeEnvironmentResolutionError,
    resolve_evaluator_runtime_environment,
    write_harbor_environment_inputs,
)
from ..runtime.uv import prepare_candidate_runtime
from ..surface import surface_patterns
from .harbor_smoke import HarborTaskAudit, audit_harbor_results
from .snapshot import build_candidate_snapshot, materialize_snapshot

SmokeStatus = Literal["passed", "failed", "unsupported"]
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PROXY|BASE_URL|ENDPOINT", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+@")
_COMMON_SECRET_VALUES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:gh[opusr]|github_pat)_[A-Za-z0-9_]{16,}\b"),
)
_COMMON_SECRET_ASSIGNMENT = re.compile(r"(?i)(\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+")


class SmokeMode(StrEnum):
    # PR #31's evaluator boundary owns the wire values: ``full`` prepares and
    # exercises the complete candidate path, while ``single`` performs one
    # model-backed task. The names retain PR #29's caller-facing intent.
    INSTALL = "full"
    MODEL = "single"


@dataclass(frozen=True)
class SmokeResult:
    status: SmokeStatus
    attempt_dir: Path
    snapshot_tree: str
    returncode: int | None
    stdout_path: Path
    stderr_path: Path


def run_candidate_smoke(
    checkout: Path,
    *,
    workspace: Path,
    mode: SmokeMode = SmokeMode.INSTALL,
    environment: Mapping[str, str] | None = None,
) -> SmokeResult:
    source_environment = clean_python_env(environment)
    include, exclude = surface_patterns(workspace)
    snapshot = build_candidate_snapshot(checkout, "HEAD", include=include, exclude=exclude)
    attempt = reserve_attempt_directory(workspace / "runs" / "smoke")
    started = time.monotonic()
    with materialize_snapshot(checkout, snapshot) as materialized:
        script = materialized / "evaluator" / "smoke.sh"
        if not script.is_file():
            return _write_result(
                attempt,
                "unsupported",
                snapshot.tree,
                None,
                "",
                "",
                time.monotonic() - started,
                mode=mode,
            )
        config = load_config(materialized / "evolve.yaml")
        evaluator = config.get("evaluator")
        runtime = prepare_candidate_runtime(
            materialized,
            attempt,
            workspace / "runs" / "runtime",
            snapshot.tree,
            evaluator if isinstance(evaluator, dict) else {},
            env=source_environment,
        )
        if not runtime.ready:
            return _write_result(
                attempt,
                "failed",
                snapshot.tree,
                None,
                "",
                _redact(runtime.reason or "candidate runtime preparation failed", source_environment),
                time.monotonic() - started,
                mode=mode,
            )
        evaluator_config = evaluator if isinstance(evaluator, dict) else {}
        try:
            environment_plan = resolve_evaluator_runtime_environment(
                materialized,
                evaluator_config,
                source_environment,
            )
        except RuntimeEnvironmentResolutionError as error:
            return _write_result(
                attempt,
                "failed",
                snapshot.tree,
                None,
                "",
                _redact(str(error), source_environment),
                time.monotonic() - started,
                mode=mode,
            )
        write_harbor_environment_inputs(attempt, environment_plan)
        env = {
            **source_environment,
            **environment_plan.process_env(),
            "EVOLVE_RUN_DIR": str(attempt),
            "EVOLVE_ATTEMPT_ID": owned_attempt_id(workspace, attempt),
            "EVOLVE_CANDIDATE_SMOKE_MODE": mode.value,
            "EVOLVE_EVAL_SPLIT": evaluation_split_name(evaluator_config, "candidate"),
        }
        if runtime.variant is not None:
            env["EVOLVE_CANDIDATE_RUNTIME_ENV_JSON"] = runtime.environment_json()
            env["EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON"] = runtime.mounts_json()
        env.setdefault("EVOLVE_FRAMEWORK_PYTHON", sys.executable)
        completed = run_owned([str(script)], cwd=materialized, env=env)
    harbor_audit = audit_harbor_results(attempt, required=evaluator_config.get("engine") == "harbor")
    audit_failure = "; ".join(harbor_audit.failures)
    stderr = completed.stderr
    if audit_failure:
        stderr = f"{stderr.rstrip()}\nEVOLVE_HARBOR_SMOKE_FAILED: {audit_failure}\n".lstrip("\n")
    return _write_result(
        attempt,
        "passed" if completed.returncode == 0 and not audit_failure else "failed",
        snapshot.tree,
        completed.returncode,
        _redact(completed.stdout, source_environment),
        _redact(stderr, source_environment),
        time.monotonic() - started,
        mode=mode,
        harbor_audit=harbor_audit if harbor_audit.observed or harbor_audit.required else None,
    )


def _redact(text: str, environment: Mapping[str, str]) -> str:
    redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    values = {value for name, value in environment.items() if len(value) >= 4 and _SECRET_NAME.search(name)}
    for value in sorted(values, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    for pattern in _COMMON_SECRET_VALUES:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = _COMMON_SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", redacted)
    return redacted


def _write_result(
    attempt: Path,
    status: SmokeStatus,
    snapshot_tree: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    duration_s: float,
    *,
    mode: SmokeMode,
    harbor_audit: HarborTaskAudit | None = None,
) -> SmokeResult:
    stdout_path = attempt / "stdout.log"
    stderr_path = attempt / "stderr.log"
    write_private_text(stdout_path, stdout)
    write_private_text(stderr_path, stderr)
    payload = {
        "schema_version": 1,
        "mode": mode.value,
        "status": status,
        "snapshot_tree": snapshot_tree,
        "returncode": returncode,
        "duration_s": round(duration_s, 6),
        "artifacts": {
            "stdout": _log_artifact(stdout_path),
            "stderr": _log_artifact(stderr_path),
        },
    }
    if harbor_audit is not None:
        payload["harbor_task_audit"] = harbor_audit.payload()
    if failure_category := _structured_failure_category(attempt):
        payload["failure_category"] = failure_category
    write_private_text(
        attempt / "result.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return SmokeResult(status, attempt, snapshot_tree, returncode, stdout_path, stderr_path)


def _log_artifact(path: Path) -> dict[str, str]:
    return {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _structured_failure_category(attempt: Path) -> str | None:
    accepted = {"dependency_tool_unavailable", "network_unavailable"}
    for path in sorted((attempt / "jobs").glob("**/result.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("failure_category") in accepted:
            return str(payload["failure_category"])
    return None
