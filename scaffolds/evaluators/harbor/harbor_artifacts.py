from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SAFE_ARTIFACTS = (
    "agent/mini-swe-agent.trajectory.json",
    "agent/mini-swe-agent.txt",
    "agent/trajectory.json",
    # Structured, verifier-owned evidence for artifact-producing evaluations.
    # Keep this explicit allowlist: arbitrary verifier files may contain secrets.
    "verifier/diagnostics.json",
    "verifier/evaluation.json",
    "verifier/judge.json",
    "verifier/poster.svg",
    "verifier/poster.png",
    "evolve-replay.json",
)
_CANDIDATE_MARKER = "EVOLVE_CANDIDATE_INVALID:"
_MISSING_TOOL_OUTPUT = "No tool output found for function call"
_VERIFIER_UV_RESOLUTION_ERROR = "No solution found when resolving tool dependencies:"
_VERIFIER_UV_OFFLINE_HINTS = (
    "was not found in the cache",
    "Packages were unavailable because the network was disabled",
)
_VERIFIER_UV_DOWNLOAD_ERROR = "error: Failed to download:"
_VERIFIER_UV_REQUEST_ERROR = "Caused by: Request failed"
_VERIFIER_CURL_SERVER_ERROR = re.compile(r"curl: \(\d+\) The requested URL returned error: 5\d\d\b")
_SAFE_ERROR_CODE = re.compile(r"[a-z0-9_]+")
_SENSITIVE_ENV_NAME = re.compile(r"(?i)(?:proxy|api[_-]?key|access[_-]?token|token|secret|password|authorization|auth)")


def _redact_environment_values(text: str, environment: Mapping[str, str] | None = None) -> str:
    configured = os.environ if environment is None else environment
    values = {value for name, value in configured.items() if _SENSITIVE_ENV_NAME.search(name) and len(value) >= 8}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def candidate_error_code(exception_info: object) -> str | None:
    if not isinstance(exception_info, dict):
        return None
    message = str(exception_info.get("exception_message") or "")
    if _MISSING_TOOL_OUTPUT in message:
        return "invalid_tool_history"
    if _CANDIDATE_MARKER not in message:
        return None
    code = message.partition(_CANDIDATE_MARKER)[2].strip().splitlines()[0].strip()
    return code if _SAFE_ERROR_CODE.fullmatch(code) else "candidate_invalid"


def collect_harbor_artifacts(jobs_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[float]]:
    trials = _load_task_trials(jobs_dir)
    return _build_task_vector(trials), _build_artifact_index(jobs_dir, trials), _scoring_rewards(trials)


def write_harbor_artifacts(jobs_dir: Path, run_dir: Path) -> list[float]:
    _write_verifier_diagnostics(jobs_dir)
    _write_replay_envelopes(jobs_dir)
    task_vector, artifact_index, rewards = collect_harbor_artifacts(jobs_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task_vector.json").write_text(json.dumps(task_vector, indent=2, sort_keys=True) + "\n")
    (run_dir / "evaluation_artifacts.json").write_text(json.dumps(artifact_index, indent=2, sort_keys=True) + "\n")
    (run_dir / "cost.json").write_text(
        json.dumps({"usd": sum(float(trial["cost_usd"]) for trial in artifact_index["trials"])}, sort_keys=True) + "\n"
    )
    return rewards


def _write_replay_envelopes(jobs_dir: Path) -> None:
    """Project raw Harbor results onto the only verifier-safe replay boundary."""
    for result_path in sorted(jobs_dir.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict):
            continue
        task_name, trial_name = result.get("task_name"), result.get("trial_name")
        if not isinstance(task_name, str) or not isinstance(trial_name, str):
            continue
        envelope: dict[str, Any] = {"schema_version": 1, "task_name": task_name, "trial_name": trial_name}
        reward = _reward(result)
        if reward is not None:
            envelope["verifier_result"] = {"rewards": {"reward": reward}}
        exception = result.get("exception_info")
        if isinstance(exception, dict):
            safe_exception = {
                "exception_type": _safe_label(exception.get("exception_type")),
                "exception_message": _exception_message(exception.get("exception_message")),
            }
            envelope["exception_info"] = {key: value for key, value in safe_exception.items() if value is not None}
        agent = result.get("agent_result")
        if isinstance(agent, dict):
            safe_agent = {
                key: value
                for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
                if (value := _safe_number(agent.get(key))) is not None
            }
            if safe_agent:
                envelope["agent_result"] = safe_agent
        (result_path.parent / "evolve-replay.json").write_text(json.dumps(envelope, sort_keys=True) + "\n")


def _safe_number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _safe_label(value: object, limit: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    text = _redact_environment_values(value)
    return text if len(text) <= limit and re.fullmatch(r"[A-Za-z0-9_.:-]+", text) else None


def _safe_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value[:20] if (text := _safe_label(item)) is not None]


def _safe_numeric_map(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int | float] = {}
    for key, item in value.items():
        number = _safe_number(item)
        safe_key = _safe_label(str(key))
        if number is not None and safe_key is not None:
            result[safe_key] = number
    return result


def _verifier_diagnostics(verifier_result: object) -> dict[str, Any] | None:
    if not isinstance(verifier_result, dict):
        return None
    diagnostics: dict[str, Any] = {"schema_version": 1}
    for key in ("reward",):
        if (number := _safe_number(verifier_result.get(key))) is not None:
            diagnostics[key] = number
    for key in ("status", "termination_reason"):
        if (text := _safe_label(verifier_result.get(key))) is not None:
            diagnostics[key] = text
    if reward_basis := _safe_text_list(verifier_result.get("reward_basis")):
        diagnostics["reward_basis"] = reward_basis

    reward_info = verifier_result.get("reward_info")
    if isinstance(reward_info, dict):
        safe_reward_info: dict[str, Any] = {}
        action_checks = reward_info.get("action_checks")
        if isinstance(action_checks, list):
            safe_checks: list[dict[str, Any]] = []
            for check in action_checks[:50]:
                if not isinstance(check, dict):
                    continue
                safe_check: dict[str, Any] = {}
                if isinstance(check.get("action_match"), bool):
                    safe_check["action_match"] = check["action_match"]
                if (number := _safe_number(check.get("action_reward"))) is not None:
                    safe_check["action_reward"] = number
                if (text := _safe_label(check.get("tool_type"))) is not None:
                    safe_check["tool_type"] = text
                if safe_check:
                    safe_checks.append(safe_check)
            if safe_checks:
                safe_reward_info["action_checks"] = safe_checks
        db_check = reward_info.get("db_check")
        if isinstance(db_check, dict):
            safe_db_check: dict[str, Any] = {}
            if isinstance(db_check.get("db_match"), bool):
                safe_db_check["db_match"] = db_check["db_match"]
            if (number := _safe_number(db_check.get("db_reward"))) is not None:
                safe_db_check["db_reward"] = number
            if safe_db_check:
                safe_reward_info["db_check"] = safe_db_check
        if (number := _safe_number(reward_info.get("reward"))) is not None:
            safe_reward_info["reward"] = number
        if reward_basis := _safe_text_list(reward_info.get("reward_basis")):
            safe_reward_info["reward_basis"] = reward_basis
        if reward_breakdown := _safe_numeric_map(reward_info.get("reward_breakdown")):
            safe_reward_info["reward_breakdown"] = reward_breakdown
        if safe_reward_info:
            diagnostics["reward_info"] = safe_reward_info

    runtime = verifier_result.get("runtime_initialization")
    if isinstance(runtime, dict):
        safe_runtime: dict[str, Any] = {}
        accepted = runtime.get("accepted")
        if isinstance(accepted, dict):
            safe_accepted: dict[str, Any] = {}
            for key in ("max_errors", "max_steps"):
                if (number := _safe_number(accepted.get(key))) is not None:
                    safe_accepted[key] = number
            if "seed" in accepted and (
                (seed := _safe_number(accepted.get("seed"))) is not None or accepted["seed"] is None
            ):
                safe_accepted["seed"] = seed
            if safe_accepted:
                safe_runtime["accepted"] = safe_accepted
        for key in (
            "accepted_event_ordinal",
            "accepted_once_count",
            "event_ordinal",
            "idempotent_replay_count",
            "schema_version",
            "start_event_ordinal",
        ):
            if (number := _safe_number(runtime.get(key))) is not None:
                safe_runtime[key] = number
        if (text := _safe_label(runtime.get("phase"))) is not None:
            safe_runtime["phase"] = text
        if rejected := _safe_numeric_map(runtime.get("rejected_mutations")):
            safe_runtime["rejected_mutations"] = rejected
        if safe_runtime:
            diagnostics["runtime_initialization"] = safe_runtime

    return diagnostics if len(diagnostics) > 1 else None


def _write_verifier_diagnostics(jobs_dir: Path) -> None:
    for result_path in sorted(jobs_dir.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict) or not result.get("task_name") or not result.get("trial_name"):
            continue
        verifier_path = result_path.parent / "verifier" / "result.json"
        try:
            verifier_result = json.loads(verifier_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        diagnostics = _verifier_diagnostics(verifier_result)
        if diagnostics is None:
            continue
        output = result_path.parent / "verifier" / "diagnostics.json"
        output.write_text(json.dumps(diagnostics, indent=2, sort_keys=True) + "\n")


def _load_task_trials(jobs_dir: Path) -> list[dict[str, Any]]:
    if not jobs_dir.exists():
        return []
    verifier_timeout_is_final_zero = _verifier_timeout_is_final_zero(jobs_dir)
    trials: list[dict[str, Any]] = []
    for result_path in sorted(jobs_dir.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict):
            continue
        task_name = result.get("task_name")
        trial_name = result.get("trial_name")
        if not isinstance(task_name, str) or not isinstance(trial_name, str):
            continue
        trial_dir = result_path.parent
        verifier_dependency_error = None if (_reward(result) or 0.0) > 0.0 else _verifier_dependency_error(trial_dir)
        status, reward, owner = _trial_result(
            result,
            verifier_timeout_is_final_zero=verifier_timeout_is_final_zero,
            verifier_dependency_error=verifier_dependency_error,
        )
        if status == "timeout" and owner == "benchmark_agent" and _network_only_timeout(trial_dir):
            status, reward, owner = "infrastructure_failed", None, "agent_runtime"
        exception_info = result.get("exception_info")
        exception_type = None
        exception_message = None
        if isinstance(exception_info, dict):
            raw_type = exception_info.get("exception_type")
            raw_message = exception_info.get("exception_message")
            exception_type = str(raw_type) if raw_type else None
            exception_message = _exception_message(raw_message)
        if exception_type is None and verifier_dependency_error:
            exception_type = "VerifierDependencyError"
            exception_message = verifier_dependency_error
        trials.append(
            {
                "task_name": task_name,
                "trial_name": trial_name,
                "status": status,
                "reward": reward,
                "owner": owner,
                "exception_type": exception_type,
                "exception_message": exception_message,
                "cost_usd": _cost_usd(result),
                "trial_dir": trial_dir,
                "artifacts": _safe_artifacts(jobs_dir, trial_dir),
            }
        )
    return sorted(trials, key=lambda trial: (str(trial["task_name"]), str(trial["trial_name"])))


def _build_task_vector(trials: list[dict[str, Any]]) -> dict[str, Any]:
    tasks: dict[str, list[dict[str, Any]]] = {}
    for entry in trials:
        tasks.setdefault(str(entry["task_name"]), []).append(entry)
    serialized_tasks: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for task_name, task_trials in sorted(tasks.items()):
        serialized_trials: list[dict[str, Any]] = []
        for index, entry in enumerate(sorted(task_trials, key=lambda trial: str(trial["trial_name"]))):
            serialized = {
                "trial": index,
                "status": entry["status"],
                "reward": entry["reward"],
                "owner": entry["owner"],
            }
            if entry["exception_type"] is not None:
                serialized["exception_type"] = entry["exception_type"]
            if entry["exception_message"] is not None:
                serialized["exception_message"] = entry["exception_message"]
            serialized_trials.append(serialized)
        serialized_tasks[task_name] = {"trials": serialized_trials}
    return {"schema_version": 1, "tasks": serialized_tasks}


def _build_artifact_index(jobs_dir: Path, trials: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "jobs_dir": str(jobs_dir.resolve()),
        "trials": [
            {
                "task_name": entry["task_name"],
                "trial_name": entry["trial_name"],
                "cost_usd": entry["cost_usd"],
                "files": entry["artifacts"],
            }
            for entry in trials
        ],
    }


def _scoring_rewards(trials: list[dict[str, Any]]) -> list[float]:
    return [float(entry["reward"]) for entry in trials if entry["reward"] is not None]


def _reward(result: dict[str, Any]) -> float | None:
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    return float(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool) else None


def _network_only_timeout(trial_dir: Path) -> bool:
    """Require retained input-only trajectory plus repeated transport errors."""
    try:
        trajectory = json.loads((trial_dir / "agent/trajectory.json").read_text())
        steps = trajectory.get("steps")
        if not isinstance(steps, list) or not steps:
            return False
        if any(not isinstance(step, dict) or step.get("source") not in {"system", "user"} for step in steps):
            return False
        with (trial_dir / "agent/codex.txt").open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 262144))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, ValueError, AttributeError):
        return False
    errors = 0
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "error"
            and str(event.get("message", "")).startswith("Reconnecting... waiting for network (Connection failed:")
        ):
            errors += 1
    return errors >= 2


def _verifier_timeout_is_final_zero(jobs_dir: Path) -> bool:
    root_config = jobs_dir / "config.json"
    configs = [root_config] if root_config.is_file() else []
    if not configs:
        configs = [path for path in jobs_dir.glob("*/config.json") if path.parent.parent == jobs_dir]
    if len(configs) != 1:
        return False
    try:
        payload = json.loads(configs[0].read_text())
        retry = payload.get("retry") if isinstance(payload, dict) else None
        excluded = set((retry or {}).get("exclude_exceptions") or [])
        return int((retry or {}).get("max_retries") or 0) >= 1 and "VerifierTimeoutError" not in excluded
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _trial_result(
    result: dict[str, Any],
    *,
    verifier_timeout_is_final_zero: bool,
    verifier_dependency_error: str | None = None,
) -> tuple[str, float | None, str]:
    if verifier_dependency_error:
        return "infrastructure_failed", None, "evaluator"
    exception = result.get("exception_info") or {}
    exception_type = str(exception.get("exception_type") or "")
    if exception_type in {"AgentTimeoutError", "AgentExecutionTimeoutError"}:
        return "timeout", 0.0, "benchmark_agent"
    if (
        exception_type == "VerifierTimeoutError"
        and verifier_timeout_is_final_zero
        and result.get("agent_result") is not None
    ):
        return "timeout", 0.0, "benchmark_verifier"
    if exception_type:
        if candidate_error_code(exception):
            return "candidate_invalid", None, "candidate"
        return "infrastructure_failed", None, "ambiguous"
    reward = _reward(result)
    if reward is not None:
        return "benchmark_complete", reward, "benchmark"
    return "infrastructure_failed", None, "evaluator"


def _verifier_dependency_error(trial_dir: Path) -> str | None:
    output = trial_dir / "verifier" / "test-stdout.txt"
    try:
        text = output.read_text(errors="replace")
    except OSError:
        return None
    if _VERIFIER_UV_RESOLUTION_ERROR in text and any(hint in text for hint in _VERIFIER_UV_OFFLINE_HINTS):
        return "verifier uv tool dependency resolution failed"
    if _VERIFIER_UV_DOWNLOAD_ERROR in text and _VERIFIER_UV_REQUEST_ERROR in text:
        return "verifier uv tool dependency download failed"
    if _VERIFIER_CURL_SERVER_ERROR.search(text):
        return "verifier dependency bootstrap download failed"
    return None


def _cost_usd(result: dict[str, Any]) -> float:
    value = (result.get("agent_result") or {}).get("cost_usd")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _exception_message(value: object) -> str | None:
    if not value:
        return None
    message = _redact_environment_values(str(value).strip().splitlines()[0].strip())
    return None if message.startswith("Traceback") else message or None


def _safe_artifacts(jobs_dir: Path, trial_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for relative_path in SAFE_ARTIFACTS:
        path = trial_dir / relative_path
        if not path.is_file():
            continue
        payload = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(jobs_dir).as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return files
