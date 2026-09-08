"""Explicit development feedback projection; private logs are not public data."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .runtime.files import read_regular_file

_ROW_FIELDS = {
    "genid",
    "parent",
    "tag",
    "candidate_commit",
    "target_tree",
    "operators_tree",
    "evaluator_tree",
    "task_set_hash",
    "score",
    "cost_usd",
    "valid_parent",
    "kind",
    "round",
    "purpose",
    "status",
    "outcome",
    "eval_scope",
    "n_tasks",
    "n_trials",
}
_METRICS = {
    "score",
    "accuracy",
    "pass_rate",
    "cost_usd",
    "usd",
    "total_cost_usd",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "n_tasks",
    "total_tasks",
    "passed_tasks",
    "failed_tasks",
    "tasks_total",
    "tasks_passed",
    "tasks_failed",
    "agent_errors",
    "infra_errors",
    "incomplete_tasks",
    "agent_timeout_count",
    "n_trials",
    "expected_trials",
    "scoreable_trials",
}


def archive_row(row: dict[str, Any]) -> dict[str, Any]:
    """Exclude task vectors, free prose, verifier exceptions and arbitrary extras."""
    result = {k: v for k, v in row.items() if k in _ROW_FIELDS and (v is None or type(v) in (str, int, float, bool))}
    if isinstance(row.get("evals"), list):
        result["evals"] = [archive_row(e) for e in row["evals"] if isinstance(e, dict) and e.get("purpose") != "anchor"]
    return result


def operator_feedback(run_dir: Path) -> dict[str, bytes]:
    files = {}
    for filename in ("rollout/summary.json", "analyze/summary.json", "mutate/usage.json"):
        if not (run_dir / filename).exists():
            continue
        data = json.loads(read_regular_file(run_dir, filename))
        if not isinstance(data, dict):
            raise RuntimeError("public feedback summary must be an object")
        projected = {
            key: value
            for key, value in data.items()
            if key in _METRICS and type(value) in (int, float) and math.isfinite(value)
        }
        files[filename] = json.dumps(projected).encode()
    gate = run_dir / "gate.json"
    if gate.exists():
        data = json.loads(read_regular_file(run_dir, "gate.json"))
        verdict = data.get("verdict")
        if verdict not in {"accept", "reject", "accepted", "rejected"}:
            verdict = "accept" if data.get("valid_parent") is True else "reject"
        files["gate.json"] = json.dumps(
            {
                "valid_parent": data.get("valid_parent") is True,
                "verdict": verdict,
                "reason": "host gate decision; private diagnostic text omitted",
            }
        ).encode()
    return files
