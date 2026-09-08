"""Harbor time-budget facts; enforcement remains with Harbor, policy with the target."""

import json
import math
import tomllib
from pathlib import Path
from typing import Any


def _positive(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid time budget")
    return float(value)


def agent_time_budget(config: dict[str, Any], task_file: Path | None = None) -> dict[str, Any]:
    """Match Harbor's min(base, cap) * phase-or-global-multiplier rule.

    Missing metadata is unknown, never a guessed default or an unlimited budget.
    Only task.toml's agent timeout is read; no task instructions or verifier data.
    """
    try:
        agent = config["agent"]
        base = agent.get("override_timeout_sec")
        if base is None:
            task_file = task_file or Path(config["task"]["path"]) / "task.toml"
            declared = tomllib.loads(task_file.read_text())
            base = declared["agent"]["timeout_sec"]
        base = _positive(base)
        cap = agent.get("max_timeout_sec")
        if cap is not None:
            base = min(base, _positive(cap))
        multiplier = config.get("agent_timeout_multiplier")
        if multiplier is None:
            # Harbor writes config.json with exclude_defaults=True.
            # An omitted global multiplier is the declared TrialConfig default.
            multiplier = config.get("timeout_multiplier", 1.0)
        limit = _positive(base * _positive(multiplier))
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return {"status": "unavailable", "reason": "agent limit metadata missing or invalid"}
    return {"status": "available", "limit_s": limit, "source": "Harbor trial config and task agent metadata"}


def read_agent_time_budget(logs_dir: Path) -> dict[str, Any]:
    try:
        config = json.loads((logs_dir.parent / "config.json").read_text())
        return agent_time_budget(config)
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "Harbor trial config unavailable"}


def execution_time_budget(
    config: dict[str, Any], task_file: Path | None, elapsed_s: float | None, exception_type: str
) -> dict[str, Any]:
    budget = agent_time_budget(config, task_file)
    budget.update(elapsed_s=elapsed_s, timed_out=exception_type == "AgentTimeoutError")
    if budget["status"] == "available" and elapsed_s is not None and math.isfinite(elapsed_s) and elapsed_s >= 0:
        budget["fraction_used"] = round(elapsed_s / budget["limit_s"], 6)
        budget["remaining_s"] = round(max(0.0, budget["limit_s"] - elapsed_s), 3)
    return budget


def summarize_time_budgets(cases: list[dict[str, Any]]) -> dict[str, Any]:
    budgets = [(case.get("execution") or {}).get("time_budget", {}) for case in cases]
    fractions = [b["fraction_used"] for b in budgets if isinstance(b.get("fraction_used"), (int, float))]
    return {
        "agent_timeout_count": sum((case.get("exception") or {}).get("type") == "AgentTimeoutError" for case in cases),
        "agent_time_budget": {
            "tasks_with_fraction": len(fractions),
            "tasks_without_fraction": len(cases) - len(fractions),
            "max_fraction_used": max(fractions, default=None),
            "tasks_at_least_80_percent": sum(f >= 0.8 for f in fractions),
        },
    }
