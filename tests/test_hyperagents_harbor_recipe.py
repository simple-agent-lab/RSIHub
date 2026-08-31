import json
from pathlib import Path

from conftest import run_evolve, write_locked_miniswe_seed

from evolve.config import operator_blocks, surface_lists


def test_hyperagents_recipe_initializes_broad_harbor_bundle(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents-workspace"
    seed = write_locked_miniswe_seed(tmp_path / "miniswe-seed")
    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "hyperagents",
        "--seed",
        str(seed),
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(tmp_path / "evolve-home")},
    )
    assert result.returncode == 0, result.stderr
    assert surface_lists(workspace) == (["target/**", "operators/**"], [])
    config = (workspace / "evolve.yaml").read_text()
    assert "operator: hyperagents" in config
    assert "runner: harbor" in config
    assert "expose_gate_data: false" in config
    assert "agent: codex" in config
    assert "reasoning_effort: xhigh" in config
    assert "editable_roots:" in config
    assert "- target" in config and "- operators" in config
    assert "agent_env" not in operator_blocks(workspace)["mutate"]["config"]
    assert json.loads((workspace / ".evolve-components.json").read_text())["integrations"] == [
        "evolve.integrations.harbor.miniswe_candidate",
    ]
    assert (workspace / "evaluator/agent.env").read_text() == (
        "MINISWE_COST_LIMIT=3.0\nMINISWE_ENV_TIMEOUT=30\nMINISWE_MAX_OUTPUT_LIMIT=10000\n"
        "MINISWE_REASONING_EFFORT=high\nMINISWE_STEP_LIMIT=100\n"
    )
    assert "task_scope: full" in config
    assert "evaluation_split: train" in config
    assert "tasks_per_round: 30" in config
    assert "\n  split:" not in config
    assert "repetitions: 1" in config
    assert "n_concurrent: 10" in config
    prompt = (workspace / "operators/mutate.py").read_text()
    assert "Strongly prefer a substantive `target/**`" in prompt
    assert "operator-only proposal is allowed" in prompt
    assert "`operators/**` remains editable" in prompt
    assert "def _install_bundle(" in (workspace / "library/_shared/runners/harbor.py").read_text()
