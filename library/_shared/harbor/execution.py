"""Build and execute Harbor rollout commands."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evolve.frozen.interfaces import OperatorContext, RolloutResult
from evolve.splits import harbor_task_pattern, select_dataset_tasks

from .evidence import _PROXY_ENV, _canonical_task_name, _load_eval_env, _redact


def _jobs_root(ctx: OperatorContext) -> Path:
    configured = ctx.config.get("jobs_dir") or os.environ.get("EVOLVE_ROLLOUT_JOBS_DIR")
    return Path(str(configured)).expanduser() if configured else ctx.workspace / "runs" / "harbor-rollouts"


_OUTCOME_ORDER = ("failed", "agent_error", "infra_error", "incomplete", "passed")


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _nonnegative_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _configured_max_retries(config: Mapping[str, Any], eval_env: Mapping[str, str]) -> int:
    value = config["max_retries"] if "max_retries" in config else eval_env.get("EVOLVE_HARBOR_MAX_RETRIES")
    return _nonnegative_int(value, 0)


def _float_value(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _run_timeout() -> float | None:
    try:
        outer = float(os.environ.get("EVOLVE_OPERATOR_TIMEOUT_S", ""))
    except ValueError:
        return None
    return max(0.1, outer - min(5.0, max(0.5, outer * 0.05)))


def _reset_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _batch_command(
    base_command: list[str],
    *,
    jobs_dir: Path,
    n_concurrent: int,
    budget_tasks: int,
    task_selectors: list[str],
    include_task: object = None,
) -> list[str]:
    command = [
        *base_command,
        "--jobs-dir",
        str(jobs_dir),
        "-n",
        str(min(n_concurrent, budget_tasks)),
        "--n-tasks",
        str(budget_tasks),
    ]
    if task_selectors:
        for task_name in task_selectors:
            command.extend(["--include-task-name", harbor_task_pattern(task_name)])
    elif include_task:
        command.extend(["--include-task-name", str(include_task)])
    return command


def _append_agent_env(command: list[str], checkout: Path, config: dict[str, Any]) -> None:
    for override, lower, upper in _PROXY_ENV:
        value = os.environ.get(override) or os.environ.get(lower) or os.environ.get(upper)
        if value:
            for key in (lower, upper):
                command.extend(["--ae", f"{key}={value}", "--ve", f"{key}={value}"])
    values = _load_eval_env(checkout)
    agent_env = checkout / "evaluator" / "agent.env"
    if agent_env.is_file():
        for line in agent_env.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value
    configured = config.get("agent_env")
    if isinstance(configured, dict):
        values.update({str(key): str(value) for key, value in configured.items()})
    for key, value in values.items():
        if key.startswith("EVOLVE_HARBOR_") or key.startswith("EVOLVE_EVALUATOR_"):
            continue
        command.extend(["--ae", f"{key}={value}"])


def _run_harbor(command: list[str], checkout: Path, log_path: Path, env: dict[str, str]) -> int:
    start = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=checkout,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        bufsize=1,
        umask=0o077,
    )
    chunks: list[str] = []

    def consume_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            chunks.append(line)
            if os.environ.get("EVOLVE_LIVE_OUTPUT") == "1":
                print(_redact(line), end="", flush=True)

    reader = threading.Thread(target=consume_output, daemon=True)
    reader.start()
    try:
        process.wait(timeout=_run_timeout())
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait()
        chunks.append("\nharbor rollout timed out\n")
    reader.join()
    output = "".join(chunks)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(_redact(f"wall_s={time.monotonic() - start:.3f}\n{output or ''}"))
    log_path.chmod(0o600)
    return process.returncode if process.returncode is not None else 1


def _select_train_tasks(
    manifest_path: Path,
    dataset: str,
    budget_tasks: int,
    requested: object = None,
    *,
    sampling: str = "head",
    sampling_key: str = "0",
) -> list[str]:
    """Resolve a bounded train batch, optionally preserving an exact prior batch."""
    all_train, _ = select_dataset_tasks(manifest_path, dataset, "train", limit=None)
    if requested is None:
        if sampling == "head":
            ordered = all_train
        elif sampling == "generation_shuffle":
            ordered = sorted(
                all_train,
                key=lambda name: hashlib.sha256(f"{sampling_key}\0{name}".encode()).hexdigest(),
            )
        else:
            raise ValueError("task_sampling must be 'head' or 'generation_shuffle'")
        return ordered[:budget_tasks]
    if (
        not isinstance(requested, list)
        or not requested
        or not all(isinstance(item, str) and item for item in requested)
    ):
        raise ValueError("task_names must be a non-empty list of task names")
    normalized: list[str] = []
    unknown: list[str] = []
    for name in requested:
        canonical = _canonical_task_name(name, all_train)
        if canonical is None:
            unknown.append(name)
        else:
            normalized.append(canonical)
    if unknown:
        raise ValueError("task_names must come from the frozen train split: " + ", ".join(unknown))
    if len(set(normalized)) != len(normalized):
        raise ValueError("task_names must not contain duplicates")
    return normalized


def _completed_rollout(ctx: OperatorContext) -> RolloutResult | None:
    if ctx.config.get("reuse_completed") is not True:
        return None
    rollout_dir = ctx.run_dir / "rollout"
    try:
        summary = json.loads((rollout_dir / "summary.json").read_text())
        artifacts = json.loads((rollout_dir / "artifacts.json").read_text())
        cases = json.loads((rollout_dir / "cases.json").read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(summary, dict)
        or summary.get("variant") != "harbor"
        or not isinstance(artifacts, list)
        or not all(isinstance(item, str) and item for item in artifacts)
        or "rollout/cases.json" not in artifacts
        or not isinstance(cases, list)
        or not cases
        or summary.get("tasks_observed") != len(cases)
    ):
        return None
    return RolloutResult(summary=summary, artifacts=artifacts)
