import json
from pathlib import Path

from conftest import run_evolve, write_locked_miniswe_seed

from evolve.config import operator_blocks, surface_lists
from evolve.operators import operator_timeout


def _dataset(root: Path, count: int = 10) -> Path:
    root.mkdir()
    for index in range(count):
        task = root / f"task-{index}"
        task.mkdir()
        (task / "task.toml").write_text(f'version = "1.0"\nname = "task-{index}"\n')
    return root


def test_ahe_recipe_initializes_harbor_miniswe_composition(tmp_path: Path) -> None:
    workspace = tmp_path / "ahe-workspace"
    seed = write_locked_miniswe_seed(tmp_path / "miniswe-seed")
    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "ahe",
        "--dataset",
        str(_dataset(tmp_path / "tasks")),
        "--seed",
        str(seed),
        env={"EVOLVE_HOME": str(tmp_path / "evolve-home")},
    )
    assert result.returncode == 0, result.stderr
    assert (workspace / "target/pyproject.toml").is_file()
    assert (workspace / "target/uv.lock").is_file()
    assert surface_lists(workspace) == (["target/**"], [])
    assert "source=library/rollout/parent_evaluation.py" in (workspace / "operators/rollout.py").read_text()
    assert not (workspace / "library/rollout/harbor.py").exists()
    assert "source=library/analyze/ahe.py" in (workspace / "operators/analyze.py").read_text()
    assert "source=library/mutate/ahe.py" in (workspace / "operators/mutate.py").read_text()
    assert "source=library/select/ahe_latest.py" in (workspace / "operators/select.py").read_text()
    assert "source=library/gate/ahe_artifact_valid.py" in (workspace / "operators/gate.py").read_text()
    for relative in (
        "library/_shared/runners/__init__.py",
        "library/_shared/runners/local.py",
        "library/_shared/runners/harbor.py",
        "library/mutate/_support/evidence.py",
    ):
        assert (workspace / relative).is_file(), relative
    assert (workspace / ".evolve/evolve/integrations/harbor/codex_candidate.py").is_file()
    assert (workspace / ".evolve/evolve/integrations/harbor/miniswe_candidate.py").is_file()
    assert (workspace / ".evolve/evolve/integrations/harbor/miniswe_task_file.py").is_file()
    assert not (workspace / "evolve_harbor_adapter").exists()
    assert not (workspace / "evolve_harbor_agent").exists()
    assert not (workspace / "library/mutate/_support/ahe_manifest.py").exists()
    assert json.loads((workspace / ".evolve-components.json").read_text())["integrations"] == [
        "evolve.integrations.harbor.miniswe_candidate",
    ]
    assert (workspace / "evaluator/agent.env").read_text() == (
        "MINISWE_COST_LIMIT=3.0\nMINISWE_ENV_TIMEOUT=30\nMINISWE_MAX_OUTPUT_LIMIT=10000\n"
        "MINISWE_REASONING_EFFORT=high\nMINISWE_STEP_LIMIT=100\n"
    )
    config = (workspace / "evolve.yaml").read_text()
    assert "operator: ahe" in config
    assert "runner: harbor" in config
    assert "expose_gate_data: false" in config
    assert "agent: codex" in config
    assert "reasoning_effort: xhigh" in config
    assert "editable_roots:" in config
    operators = operator_blocks(workspace)
    assert "agent_env" not in operators["mutate"]["config"]
    assert {name: operator_timeout(operators, name) for name in ("rollout", "analyze", "mutate")} == {
        "rollout": 600,
        "analyze": 3600,
        "mutate": 3600,
    }
    assert operators["analyze"] == {
        "operator": "ahe",
        "timeout_s": 3600,
        "config": {
            "max_tasks": 30,
            "max_concurrent": 10,
            "timeout_per_task": 600.0,
            "retry_attempts": 1,
            "debugger_agent_kwargs": {"reasoning_effort": "high", "max_tokens": 64000},
            "field_limit": 2000,
            "pass_threshold": 1.0,
        },
    }
    config = (workspace / "evolve.yaml").read_text()
    assert "budget_usd" not in config
    assert "max_cases" not in config
    assert "  task_scope: full" in config
    assert "  evaluation_split: train" in config
    assert "  tasks_per_round: 30" in config
    assert "\n  split:" not in config
    assert "  repetitions: 1" in config
    assert "  n_concurrent: 10" in config
