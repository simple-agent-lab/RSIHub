import json
from pathlib import Path

from conftest import git, run_evolve, write_identity_dataset, write_locked_miniswe_seed

from evolve.config import operator_blocks, surface_lists
from evolve.evaluation import ContractResolutionContext, resolve_evaluation_contract


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


def test_full_terminal_bench_recipe_freezes_all_89_tasks(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents-full-workspace"
    seed = write_locked_miniswe_seed(tmp_path / "miniswe-seed")
    dataset = write_identity_dataset(tmp_path / "terminal-bench", count=89)

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "hyperagents_tbench_full",
        "--seed",
        str(seed),
        "--dataset",
        str(dataset),
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(tmp_path / "evolve-home")},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((workspace / "evaluator/splits.json").read_text())
    assert len(manifest["tasks"]["train"]) == 89
    assert manifest["tasks"]["gate"] == []
    assert manifest["tasks"]["sealed"] == []
    config = (workspace / "evolve.yaml").read_text()
    assert "tasks_per_round: 89" in config
    assert "repetitions: 1" in config


def test_full_codex_recipe_freezes_89_tasks_and_exposes_the_plugin(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents-codex-full-workspace"
    dataset = write_identity_dataset(tmp_path / "terminal-bench", count=89)

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "hyperagents_codex_tbench_full",
        "--dataset",
        str(dataset),
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(tmp_path / "evolve-home")},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((workspace / "evaluator/splits.json").read_text())
    assert len(manifest["tasks"]["train"]) == 50
    assert len(manifest["tasks"]["gate"]) == 19
    assert len(manifest["tasks"]["sealed"]) == 20
    assert set(manifest["tasks"]["train"]).isdisjoint(manifest["tasks"]["gate"])
    assert set(manifest["tasks"]["train"]).isdisjoint(manifest["tasks"]["sealed"])
    assert set(manifest["tasks"]["gate"]).isdisjoint(manifest["tasks"]["sealed"])
    commit = git(workspace, "rev-parse", "gen/0^{commit}")
    candidate_contract = resolve_evaluation_contract(
        ContractResolutionContext(workspace=workspace, candidate_commit=commit, purpose="candidate", generation="0")
    )
    anchor_contract = resolve_evaluation_contract(
        ContractResolutionContext(workspace=workspace, candidate_commit=commit, purpose="anchor", generation="0")
    )
    assert len(candidate_contract.task_members) == 19
    assert len(anchor_contract.task_members) == 20
    assert set(candidate_contract.task_members).isdisjoint(anchor_contract.task_members)
    assert surface_lists(workspace) == (["target/**", "operators/**"], [])
    assert (workspace / "target/plugins/evolve-target/.codex-plugin/plugin.json").is_file()
    assert (workspace / "target/plugins/evolve-target/hooks/hooks.json").is_file()
    assert (workspace / "target/.agents/plugins/marketplace.json").is_file()
    mutate = operator_blocks(workspace)["mutate"]["config"]
    assert mutate["agent"] == "codex"
    assert mutate["editable_roots"] == ["target", "operators"]
    assert mutate["expose_gate_data"] is False
    rollout = operator_blocks(workspace)["rollout"]
    assert rollout["operator"] == "harbor"
    assert rollout["config"]["budget_tasks"] == 50
