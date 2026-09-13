#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from harbor_artifacts import write_harbor_artifacts


def _load_eval_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def _write_outputs(run_dir: Path, *, status: str, metrics: dict[str, object], score: float | None = None) -> None:
    # Compatibility outputs only: the host derives its EvaluationRecord from task_vector.json.
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status").write_text(f"{status}\n")
    if score is not None:
        (run_dir / "score").write_text(f"{score}\n")
    (run_dir / "metrics.json").write_text(json.dumps({"dimensions": metrics}, indent=2, sort_keys=True) + "\n")


def _attempts(env_values: dict[str, str]) -> int:
    return max(1, int(env_values.get("EVOLVE_HARBOR_ATTEMPTS", "1")))


def _expected_trials(run_dir: Path, env_values: dict[str, str]) -> int:
    """Resolve how many trials this run owed.

    Prefer the host run plan and the limited-run env override over task-split.json.
    A smoke/limited evaluation may leave the full train split recorded while only
    running N planned trials; treating the full split as expected_trials would
    mark a successful limited run as infra_failed.
    """
    attempts = _attempts(env_values)
    plan_path = run_dir / "run-plan.json"
    if plan_path.exists():
        payload = json.loads(plan_path.read_text())
        if isinstance(payload, dict):
            planned = payload.get("expected_trials")
            if isinstance(planned, int) and not isinstance(planned, bool) and planned >= 1:
                return planned
            tasks = payload.get("tasks")
            if isinstance(tasks, list) and tasks:
                return max(1, len(tasks) * attempts)
    for raw in (
        os.environ.get("EVOLVE_HARBOR_EXPECTED_TRIALS"),
        env_values.get("EVOLVE_HARBOR_EXPECTED_TRIALS"),
        env_values.get("EVOLVE_HARBOR_N"),
    ):
        if raw is not None and str(raw).strip():
            return max(1, int(raw))
    selection = run_dir / "task-split.json"
    if selection.exists():
        payload = json.loads(selection.read_text())
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if isinstance(tasks, list) and tasks:
            return max(1, len(tasks) * attempts)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        raise SystemExit("usage: parse_score.py <jobs_dir> <run_dir> <harbor_rc>")
    jobs_dir = Path(argv[1])
    run_dir = Path(argv[2])
    harbor_rc = int(argv[3])
    env_values = _load_eval_env(Path("evaluator") / "eval.env")
    expected_trials = _expected_trials(run_dir, env_values)
    rewards = write_harbor_artifacts(jobs_dir, run_dir)
    if not rewards:
        for reward_path in sorted(jobs_dir.rglob("verifier/reward.txt")) if jobs_dir.exists() else []:
            try:
                rewards.append(float(reward_path.read_text().strip()))
            except (OSError, ValueError):
                continue
    completed_trials = len(rewards)
    missing_trials = max(expected_trials - completed_trials, 0)
    if completed_trials == 0:
        _write_outputs(
            run_dir,
            status="infra_failed",
            metrics={
                "completed_trials": 0,
                "expected_trials": expected_trials,
                "missing_trials": expected_trials,
                "pass_rate": 0.0,
                "harbor_rc": harbor_rc,
            },
        )
        return 3

    score = sum(rewards) / completed_trials
    metrics = {
        "completed_trials": completed_trials,
        "expected_trials": expected_trials,
        "missing_trials": missing_trials,
        "pass_rate": score,
        "harbor_rc": harbor_rc,
    }
    if completed_trials < expected_trials:
        _write_outputs(run_dir, status="infra_failed", metrics=metrics)
        return 3

    _write_outputs(run_dir, status="complete", metrics=metrics, score=score)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
