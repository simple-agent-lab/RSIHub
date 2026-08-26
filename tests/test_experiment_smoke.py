from __future__ import annotations

import json
from pathlib import Path

import yaml
from conftest import git, init_workspace, rows_by_genid, smoke_agent_command

from evolve.experiment_smoke import _limit_operator_workload, run_experiment_smoke


def test_experiment_smoke_limits_method_owned_task_volume() -> None:
    config = {
        "operators": {
            "rollout": {"config": {"budget_tasks": 50, "n_concurrent": 10, "field_limit": 2000}},
            "analyze": {
                "config": {
                    "max_tasks": 30,
                    "max_cases": 10,
                    "judge_max_concurrent": 15,
                }
            },
            "mutate": {"config": {"max_examples": 10}},
            "validate": {"config": {"max_concurrent": 10}},
        }
    }

    _limit_operator_workload(config)

    assert config == {
        "operators": {
            "rollout": {"config": {"budget_tasks": 1, "n_concurrent": 1, "field_limit": 2000}},
            "analyze": {"config": {"max_tasks": 1, "max_cases": 1, "judge_max_concurrent": 1}},
            "mutate": {"config": {"max_examples": 1}},
            "validate": {"config": {"max_concurrent": 1}},
        }
    }


def test_experiment_smoke_runs_in_isolated_clone_and_produces_real_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)
    split = {
        "version": 2,
        "resolved": True,
        "identity_status": "verified",
        "dataset_identity": {
            "source": "local",
            "digest": "d" * 64,
            "resolved_reference": "sha256:" + "d" * 64,
        },
        "tasks": {"train": ["task-a"], "gate": [], "sealed": []},
        "task_digests": {"task-a": "a" * 64},
    }
    (workspace / "evaluator" / "splits.json").write_text(json.dumps(split) + "\n")
    (workspace / "evaluator" / "dataset.pin").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "local",
                "digest": "d" * 64,
                "resolved_reference": "sha256:" + "d" * 64,
                "members": ["task-a"],
            }
        )
        + "\n"
    )
    git(workspace, "add", "evaluator/splits.json", "evaluator/dataset.pin")
    git(workspace, "commit", "-m", "freeze smoke task")
    git(workspace, "tag", "-f", "gen/0")
    source_rows = rows_by_genid(workspace)
    monkeypatch.setenv("EVAL_STUB", "1")
    monkeypatch.setenv("EVOLVE_AGENT_COMMAND", smoke_agent_command())

    result = run_experiment_smoke(workspace, task="task-a")

    assert result.status == "passed", result.error
    assert result.workspace.is_relative_to(workspace / "runs" / "experiment-smoke")
    assert rows_by_genid(workspace) == source_rows
    child = rows_by_genid(result.workspace)["1"]
    assert child["outcome"] == "benchmark_complete"
    assert child["selection_eligible"] is True
    assert result.result_path.is_file()
    smoke_config = yaml.safe_load((result.workspace / "evolve.yaml").read_text())
    assert smoke_config["evaluator"]["tasks_per_round"] == 1
    assert smoke_config["evaluator"]["n_concurrent"] == 1
    assert smoke_config["operators"]["rollout"]["config"]["budget_tasks"] == 1
