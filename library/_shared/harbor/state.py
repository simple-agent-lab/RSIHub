"""Publish raw Harbor rollout state without interpreting failure causes."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _relative_path(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _task_toml(tasks_dir: Path | None, task_name: str) -> Path | None:
    if tasks_dir is None:
        return None
    leaf = task_name.rsplit("/", 1)[-1]
    for candidate in (tasks_dir / task_name / "task.toml", tasks_dir / leaf / "task.toml"):
        if candidate.is_file():
            return candidate
    return None


def _declared_task_state(tasks_dir: Path | None, task_name: str) -> dict[str, Any]:
    path = _task_toml(tasks_dir, task_name)
    if path is None:
        return {
            "status": "unavailable",
            "reason": "task.toml was not available to the Harbor collector",
        }
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {
            "status": "unavailable",
            "reason": "task.toml could not be parsed",
        }

    def section(name: str, allowed: tuple[str, ...]) -> dict[str, Any]:
        raw = payload.get(name)
        if not isinstance(raw, dict):
            return {}
        return {key: raw[key] for key in allowed if key in raw}

    return {
        "status": "available",
        "source": "task.toml",
        "agent": section("agent", ("timeout_sec",)),
        "verifier": section("verifier", ("timeout_sec",)),
        "environment": section(
            "environment",
            ("build_timeout_sec", "cpus", "memory", "storage", "gpus", "tpu", "docker_image"),
        ),
    }


def _harbor_configuration(result: dict[str, Any]) -> dict[str, Any]:
    config = result.get("config")
    if not isinstance(config, dict):
        return {"status": "unavailable", "reason": "Harbor result had no configuration"}
    environment = config.get("environment")
    environment = environment if isinstance(environment, dict) else {}
    return {
        "status": "available",
        "timeout_multipliers": {
            key: config.get(key)
            for key in (
                "timeout_multiplier",
                "agent_timeout_multiplier",
                "verifier_timeout_multiplier",
                "agent_setup_timeout_multiplier",
                "environment_build_timeout_multiplier",
            )
            if key in config
        },
        "environment": {
            key: environment.get(key)
            for key in (
                "type",
                "cpu_enforcement_policy",
                "memory_enforcement_policy",
                "override_cpus",
                "override_memory_mb",
                "override_storage_mb",
                "override_gpus",
                "override_tpu",
            )
            if key in environment
        },
    }


def _command_events(case: dict[str, Any]) -> dict[str, Any]:
    events = case.get("events")
    events = events if isinstance(events, list) else []
    commands: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_call":
            commands.append(
                {
                    "index": event.get("index"),
                    "timestamp": event.get("timestamp") or None,
                    "name": str(event.get("name") or "unknown"),
                    "arguments": event.get("arguments") or "",
                }
            )
        calls = event.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            commands.append(
                {
                    "step": event.get("step"),
                    "name": str(call.get("name") or "unknown"),
                    "arguments": call.get("arguments") or "",
                }
            )
    trajectory_events = case.get("trajectory_events")
    full_count = len(trajectory_events) if isinstance(trajectory_events, list) else len(events)
    return {
        "status": "available" if commands else "unavailable",
        "source": "rollout/cases.json events",
        "truncated": full_count > len(events),
        "events_observed": len(events),
        "events_total": full_count,
        "commands": commands,
    }


def _result_reference(case: dict[str, Any], jobs_dir: Path) -> tuple[Path | None, str | None]:
    raw = case.get("result_path")
    if not isinstance(raw, str) or not raw:
        return None, None
    path = Path(raw)
    relative = _relative_path(path, jobs_dir)
    return (path if path.is_file() else None), relative


def build_harbor_state(
    cases: list[dict[str, Any]],
    *,
    jobs_dir: Path,
    tasks_dir: Path | None,
    workspace: Path,
    generation: str,
    role: str,
    harbor_returncode: int,
) -> dict[str, Any]:
    """Build a deterministic, train-safe inventory of raw Harbor facts."""
    tasks: list[dict[str, Any]] = []
    for case in cases:
        task_name = str(case.get("task_name") or "unknown")
        result_path, result_relative = _result_reference(case, jobs_dir)
        result = _read_json(result_path) if result_path is not None else {}
        exception = case.get("exception")
        exception = exception if isinstance(exception, dict) else {}
        execution = case.get("execution")
        execution = execution if isinstance(execution, dict) else {}
        tasks.append(
            {
                "task_name": task_name,
                "trial_name": str(case.get("trial_name") or ""),
                "harbor": {
                    "result_id": result.get("id"),
                    "task_source": result.get("source"),
                    "task_checksum": result.get("task_checksum"),
                    "agent": result.get("agent_info") if isinstance(result.get("agent_info"), dict) else {},
                    "outcome": case.get("outcome"),
                    "reward": case.get("reward"),
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                    "timing_s": case.get("timing_s") if isinstance(case.get("timing_s"), dict) else {},
                    "exception": {
                        "type": str(exception.get("type") or ""),
                        "message": str(exception.get("message") or ""),
                    },
                    "configuration": _harbor_configuration(result),
                },
                "declared_task": _declared_task_state(tasks_dir, task_name),
                "container_runtime": {
                    "status": "unavailable",
                    "reason": "live cgroup and process state are not retained by current Harbor job artifacts",
                },
                "command_events": _command_events(case),
                "artifacts": {
                    "case": f"rollout/cases.json#task={task_name}",
                    "harbor_result": (
                        {"status": "available", "path": result_relative}
                        if result_path is not None and result_relative is not None
                        else {"status": "unavailable"}
                    ),
                    "trajectory": execution.get("trajectory", {"status": "missing"}),
                    "inventory": (
                        case.get("artifact_inventory") if isinstance(case.get("artifact_inventory"), dict) else {}
                    ),
                },
            }
        )
    return {
        "schema_version": 1,
        "source": {
            "kind": "harbor_rollout",
            "generation": str(generation),
            "role": role,
            "jobs_root": (
                {"status": "available", "path": relative_jobs}
                if (relative_jobs := _relative_path(jobs_dir, workspace)) is not None
                else {"status": "unavailable", "reason": "Harbor jobs are outside the workspace"}
            ),
        },
        "batch": {
            "harbor_returncode": harbor_returncode,
            "tasks_observed": len(tasks),
        },
        "semantics": {
            "facts_only": True,
            "missing_values": "unavailable means not captured; it does not mean zero",
            "score_authority": False,
        },
        "tasks": tasks,
    }
