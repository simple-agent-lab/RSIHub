import fcntl
import json
import shutil
from pathlib import Path

import pytest
from conftest import git, init_fixture_workspace, init_workspace, research_method, run_evolve

from evolve.agent_driver import (
    AgentLimits,
    _operator_usage,
    audit_arm_pair,
    clone_arm,
    execute_action,
    parse_action,
    resolve_interrupted,
    seal_research,
    session_status,
    start_session,
)
from evolve.orchestration import (
    commit_agent_child,
    eval_agent_child,
    finalize_child,
    fork_agent_child,
    invoke_operator,
    record_agent_fields,
)


def test_agent_operator_usage_folds_rollout_case_receipts(tmp_path: Path) -> None:
    rollout = tmp_path / "gen-1" / "rollout"
    rollout.mkdir(parents=True)
    (rollout / "cases.json").write_text(
        json.dumps(
            [
                {"timing_s": 3.5, "usage": {"cost_usd": 0.125}},
                {"timing_s": 2, "usage": {"cost_usd": 0.25}},
                {"timing_s": "invalid", "usage": {}},
                {"timing_s": {"agent_execution": 7, "agent_setup": 1, "verifier": None}, "usage": {}},
            ]
        )
    )

    assert _operator_usage(tmp_path / "gen-1", "rollout") == {"wall_s": 13.5, "usd": 0.375}


def test_agent_arm_audit_requires_clean_independent_equal_starts(tmp_path: Path) -> None:
    left, evolve_home = init_workspace(tmp_path, "left")
    _certify_baseline(left, evolve_home)
    right = tmp_path / "right"
    shutil.copytree(left, right, symlinks=True)

    with pytest.raises(RuntimeError, match="distinct evolve workspace identities"):
        audit_arm_pair(left, right)
    (right / ".git/evolve-workspace-id").write_text("0123456789abcdef0123456789abcdef\n")
    assert audit_arm_pair(left, right)["comparable"] is True
    with (right / "archive.jsonl").open("a") as stream:
        stream.write(json.dumps({"genid": "1", "tag": "gen/1", "parent": "0", "mutated": []}) + "\n")
    with pytest.raises(RuntimeError, match="occupied generations"):
        audit_arm_pair(left, right)


def test_agent_clone_arm_rotates_workspace_identity_and_preserves_baseline(tmp_path: Path) -> None:
    source, evolve_home = init_workspace(tmp_path, "source")
    _certify_baseline(source, evolve_home)
    destination = tmp_path / "destination"

    result = clone_arm(source, destination)

    assert result["comparable"] is True
    assert result["source"] == str(source)
    assert result["destination"] == str(destination)
    assert (source / ".git/evolve-workspace-id").read_text() != (destination / ".git/evolve-workspace-id").read_text()
    assert not (destination / ".venv").exists()
    assert run_evolve("agent", "audit-arms", str(source), str(destination)).returncode == 0


def test_agent_clone_arm_rejects_nonbaseline_source(tmp_path: Path) -> None:
    source, evolve_home = init_workspace(tmp_path, "source")
    _certify_baseline(source, evolve_home)
    with (source / "archive.jsonl").open("a") as stream:
        stream.write(json.dumps({"genid": "1", "tag": "gen/1", "parent": "0", "mutated": []}) + "\n")

    with pytest.raises(RuntimeError, match="occupied generations"):
        clone_arm(source, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def _certify_baseline(workspace: Path, evolve_home: Path) -> None:
    result = run_evolve(
        "eval",
        str(workspace),
        "0",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert result.returncode == 0, result.stderr


def _action(action_id: str, kind: str, **arguments):
    return parse_action({"id": action_id, "type": kind, **arguments})


def test_agent_session_requires_certified_parent_and_never_exposes_seal(tmp_path: Path) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)

    with pytest.raises(RuntimeError, match="certified valid parent"):
        start_session(workspace, AgentLimits(3, 1, 1), optimizer=research_method(workspace), objective="test research")
    with pytest.raises(RuntimeError, match="unknown action.type"):
        parse_action({"id": "forbidden", "type": "sealed"})
    schema = run_evolve("agent", "schema")
    assert schema.returncode == 0, schema.stderr
    assert "finish_research" in json.loads(schema.stdout)
    assert "submit_champion" not in json.loads(schema.stdout)
    assert "sealed" not in json.loads(schema.stdout)


def test_agent_rejects_controller_operator_overrides_and_mechanism_stages() -> None:
    for config in ({}, {"task_names": ["sealed-task"], "budget_tasks": 999}):
        with pytest.raises(RuntimeError, match="config overrides are disabled"):
            _action("override", "operator", stage="rollout", parent="0", genid="1", config=config)
    with pytest.raises(RuntimeError, match="not controller-accessible"):
        _action("gate", "operator", stage="gate", parent="0", genid="1")
    with pytest.raises(RuntimeError, match="timeout overrides are disabled"):
        _action("timeout", "operator", stage="rollout", parent="0", genid="1", timeout_s=99999)


def test_agent_session_refuses_workspace_with_sealed_evidence(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    sealed = run_evolve(
        "run",
        str(workspace),
        "--max-generations",
        "0",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert sealed.returncode == 0, sealed.stderr

    with pytest.raises(RuntimeError, match="before sealed evaluation exists"):
        start_session(workspace, AgentLimits(3, 1, 1), optimizer=research_method(workspace), objective="test research")


def test_agent_clean_start_refuses_prior_generation_history(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    with (workspace / "archive.jsonl").open("a") as stream:
        stream.write(json.dumps({"genid": "1", "tag": "gen/1", "parent": "0", "mutated": []}) + "\n")

    with pytest.raises(RuntimeError, match="clean start has occupied generations: 1"):
        start_session(
            workspace,
            AgentLimits(3, 1, 1),
            require_clean_start=True,
            optimizer=research_method(workspace),
            objective="test research",
        )


def test_agent_session_enforces_budget_receipts_and_driver_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    start_session(workspace, AgentLimits(1, 0, 0), optimizer=research_method(workspace), objective="test research")
    bypass = run_evolve(
        "eval",
        str(workspace),
        "0",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert bypass.returncode == 1
    assert "use `evolve agent act`" in bypass.stderr
    observed = execute_action(
        workspace,
        _action("observe-1", "observe", evidence=["runs/gen-0/eval"], hypothesis="baseline is valid"),
    )

    assert observed["phase"] == "completed"
    assert (
        execute_action(
            workspace,
            _action("observe-1", "observe", evidence=["runs/gen-0/eval"], hypothesis="baseline is valid"),
        )
        == observed
    )
    with pytest.raises(RuntimeError, match="different request"):
        execute_action(workspace, _action("observe-1", "observe", evidence=["changed"], hypothesis="changed"))
    assert session_status(workspace)["status"] == "exhausted"
    assert session_status(workspace)["occupied_generations"] == ["0"]
    with pytest.raises(RuntimeError, match="action budget"):
        execute_action(
            workspace,
            _action("observe-2", "observe", evidence=["x"], hypothesis="budget should reject"),
        )

    competing = run_evolve(
        "run",
        str(workspace),
        "--max-generations",
        "0",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert competing.returncode == 1
    assert "Agent Driven session owns this workspace" in competing.stderr

    submitted = execute_action(workspace, _action("submit", "finish_research", reason="completed"))
    assert submitted["result"]["champion"]["genid"] == "0"
    assert session_status(workspace)["status"] == "finished"
    assert not (workspace / "runs/agent-driven/ACTIVE").exists()
    monkeypatch.setenv("EVAL_STUB", "1")
    monkeypatch.setenv("EVOLVE_HOME", str(evolve_home))
    sealed = seal_research(workspace)
    assert sealed["baseline"]["genid"] == "0"
    assert sealed["final"]["sealed"] == "benchmark_complete"
    with pytest.raises(RuntimeError, match="cannot accept another action"):
        execute_action(
            workspace,
            _action("after-seal", "observe", evidence=["sealed result"], hypothesis="must not continue"),
        )
    receipts = [json.loads(line) for line in (workspace / "runs/agent-driven/actions.jsonl").read_text().splitlines()]
    assert [event["phase"] for event in receipts] == ["started", "completed", "started", "completed"]


def test_agent_seal_is_a_noop_when_sealed_split_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    splits_path = workspace / "evaluator/splits.json"
    splits = json.loads(splits_path.read_text())
    splits["tasks"]["train"].extend(splits["tasks"]["sealed"])
    splits["tasks"]["sealed"] = []
    splits_path.write_text(json.dumps(splits) + "\n")
    git(workspace, "add", "evaluator/splits.json")
    git(workspace, "commit", "-m", "configure an empty sealed split")
    git(workspace, "tag", "-f", "gen/0")
    _certify_baseline(workspace, evolve_home)
    monkeypatch.setenv("EVAL_STUB", "1")
    monkeypatch.setenv("EVOLVE_HOME", str(evolve_home))
    start_session(workspace, AgentLimits(1, 0, 0), optimizer=research_method(workspace), objective="test research")
    execute_action(workspace, _action("submit", "finish_research", reason="completed"))

    assert seal_research(workspace) == {"genid": "0", "sealed": "not_configured"}
    row = json.loads((workspace / "best_ever.json").read_text())
    assert not any(item.get("purpose") == "anchor" for item in row.get("evals", []))


def test_agent_session_blocks_every_direct_orchestration_route(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    start_session(workspace, AgentLimits(3, 1, 1), optimizer=research_method(workspace), objective="test research")
    child = workspace / "runs/worktrees/gen-1"
    calls = [
        lambda: invoke_operator(workspace, "rollout", "1", parent="0", checkout=child),
        lambda: fork_agent_child(workspace, "0", child),
        lambda: commit_agent_child(workspace, child, "0", "1"),
        lambda: eval_agent_child(workspace, "0"),
        lambda: finalize_child(workspace, "1", parent="0"),
        lambda: record_agent_fields(workspace, "0", {"note": "bypass"}),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="use `evolve agent act`"):
            call()


def test_agent_session_surfaces_and_resolves_interrupted_action(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    start_session(workspace, AgentLimits(3, 1, 1), optimizer=research_method(workspace), objective="test research")
    receipt = workspace / "runs/agent-driven/actions.jsonl"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "started",
                "seq": 1,
                "timestamp": "2026-09-03T00:00:00+00:00",
                "action_id": "crashed",
                "action": {"type": "observe", "evidence": ["x"], "hypothesis": "y"},
                "workspace_before": {},
            }
        )
        + "\n"
    )

    assert session_status(workspace)["pending_action"] == "crashed"
    with pytest.raises(RuntimeError, match="interrupted action crashed"):
        execute_action(workspace, _action("next", "observe", evidence=["x"], hypothesis="y"))
    resolved = resolve_interrupted(workspace, "crashed", "host process exited before dispatch returned")
    assert resolved["phase"] == "failed"
    assert execute_action(workspace, _action("next", "observe", evidence=["x"], hypothesis="y"))["phase"] == (
        "completed"
    )


def test_agent_status_distinguishes_running_from_interrupted(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    start_session(workspace, AgentLimits(3, 1, 1), optimizer=research_method(workspace), objective="test research")
    receipt = workspace / "runs/agent-driven/actions.jsonl"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "started",
                "seq": 1,
                "timestamp": "2026-09-03T00:00:00+00:00",
                "action_id": "live",
                "action": {"type": "observe", "evidence": ["x"], "hypothesis": "y"},
                "workspace_before": {},
            }
        )
        + "\n"
    )
    with (workspace / "runs/agent-driven/.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert session_status(workspace)["status"] == "running"
    assert session_status(workspace)["status"] == "interrupted"


def test_agent_interrupted_resolution_recovers_observed_operator_cost(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    start_session(
        workspace,
        AgentLimits(3, 1, 1, max_cost_usd=0.3),
        optimizer=research_method(workspace),
        objective="test research",
    )
    receipt = workspace / "runs/agent-driven/actions.jsonl"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "started",
                "seq": 1,
                "timestamp": "2026-09-03T00:00:00+00:00",
                "action_id": "paid",
                "action": {"type": "operator", "stage": "rollout", "parent": "0", "genid": "1"},
                "workspace_before": {},
            }
        )
        + "\n"
    )
    rollout = workspace / "runs/gen-1/rollout"
    rollout.mkdir(parents=True)
    (rollout / "cases.json").write_text(json.dumps([{"usage": {"cost_usd": 0.375}}]))

    resolved = resolve_interrupted(workspace, "paid", "host process exited")
    assert resolved["cost_usd"] == 0.375
    status = session_status(workspace)
    assert status["observed_cost_usd"] == 0.375
    assert status["status"] == "exhausted"
    assert status["exhausted_reasons"] == ["cost_usd"]
    with pytest.raises(RuntimeError, match="cost_usd"):
        execute_action(workspace, _action("next", "observe", evidence=["x"], hypothesis="y"))


def test_agent_detects_control_tree_and_archive_bypass(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    start_session(workspace, AgentLimits(3, 1, 1), optimizer=research_method(workspace), objective="test research")
    config = workspace / "evolve.yaml"
    original = config.read_text()
    config.write_text(original + "\n# controller bypass\n")
    with pytest.raises(RuntimeError, match="control tree changed"):
        execute_action(workspace, _action("blocked", "observe", evidence=["x"], hypothesis="y"))
    config.write_text(original)

    with (workspace / "archive.jsonl").open("a") as stream:
        stream.write("{}\n")
    with pytest.raises(RuntimeError, match="archive changed"):
        session_status(workspace)
    with pytest.raises(RuntimeError, match="archive changed"):
        execute_action(workspace, _action("blocked-again", "observe", evidence=["x"], hypothesis="y"))


def test_agent_wall_budget_stops_new_actions(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    start_session(
        workspace, AgentLimits(3, 1, 1, max_wall_s=1), optimizer=research_method(workspace), objective="test research"
    )
    manifest_path = workspace / "runs/agent-driven/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] = "2020-01-01T00:00:00+00:00"
    manifest_path.write_text(json.dumps(manifest))

    status = session_status(workspace)
    assert status["status"] == "exhausted"
    assert status["exhausted_reasons"] == ["wall_s"]
    with pytest.raises(RuntimeError, match="wall_s"):
        execute_action(workspace, _action("late", "observe", evidence=["x"], hypothesis="y"))


def test_agent_session_rejects_candidate_owned_gate_and_record(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents"
    evolve_home = tmp_path / "evolve-home"
    init_fixture_workspace(workspace, "hyperagents-smoke")
    _certify_baseline(workspace, evolve_home)
    start_session(workspace, AgentLimits(3, 0, 0), optimizer=research_method(workspace), objective="test research")
    execute_action(workspace, _action("fork", "fork", parent="0", genid="1"))
    child = workspace / "runs/worktrees/gen-1"
    (child / "operators/gate.py").write_text("raise SystemExit(0)\n")

    checkpoint = execute_action(workspace, _action("check", "checkpoint", parent="0", genid="1"))
    assert checkpoint["result"]["protected_process_violations"] == ["operators/gate.py"]
    with pytest.raises(RuntimeError, match="protected process paths: operators/gate.py"):
        execute_action(workspace, _action("commit", "commit", parent="0", genid="1"))
    assert child.exists()


def test_agent_process_change_activates_only_when_candidate_is_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "hyperagents"
    evolve_home = tmp_path / "evolve-home"
    init_fixture_workspace(workspace, "hyperagents-smoke")
    failed_task = json.loads((workspace / "evaluator/splits.json").read_text())["tasks"]["gate"][0]
    target = workspace / "target/agent.py"
    target.write_text(target.read_text() + f"\n# FAIL {failed_task}\n")
    git(workspace, "add", "target/agent.py")
    git(workspace, "commit", "-m", "make baseline improvable")
    git(workspace, "tag", "-f", "gen/0")
    _certify_baseline(workspace, evolve_home)
    monkeypatch.setenv("EVAL_STUB", "1")
    monkeypatch.setenv("EVOLVE_HOME", str(evolve_home))
    start_session(workspace, AgentLimits(12, 3, 2), optimizer=research_method(workspace), objective="test research")

    execute_action(workspace, _action("fork-1", "fork", parent="0", genid="1"))
    child = workspace / "runs/worktrees/gen-1"
    child_target = child / "target/agent.py"
    child_target.write_text(child_target.read_text().replace(f"\n# FAIL {failed_task}\n", "\n"))
    (child / "operators/rollout.py").write_text(
        """
from evolve.frozen import sdk
from evolve.frozen.config import Config
from evolve.frozen.interfaces import RolloutOperator, RolloutResult

class NextGenerationRollout(RolloutOperator):
    def rollout(self, checkout, ctx):
        (ctx.run_dir / "next-generation.marker").write_text("candidate operator ran\\n")
        return RolloutResult({}, [])

if __name__ == "__main__":
    sdk.main(NextGenerationRollout, config_schema=Config({}))
""".lstrip()
    )

    execute_action(
        workspace,
        _action("rollout-1", "operator", stage="rollout", parent="0", genid="1"),
    )
    assert not (workspace / "runs/gen-1/next-generation.marker").exists()
    execute_action(
        workspace,
        _action("validate-1", "operator", stage="validate", parent="0", genid="1"),
    )
    checkpoint = execute_action(workspace, _action("check-1", "checkpoint", parent="0", genid="1"))
    assert checkpoint["result"]["process_paths"] == ["operators/rollout.py"]
    assert checkpoint["result"]["target_paths"] == ["target/agent.py"]
    execute_action(workspace, _action("commit-1", "commit", parent="0", genid="1"))
    execute_action(workspace, _action("eval-1", "evaluate", genid="1"))
    execute_action(workspace, _action("finalize-1", "finalize", parent="0", genid="1"))

    assert session_status(workspace)["champion"]["genid"] == "1"
    execute_action(workspace, _action("fork-2", "fork", parent="1", genid="2"))
    second = execute_action(
        workspace,
        _action("rollout-2", "operator", stage="rollout", parent="1", genid="2"),
    )
    assert second["result"]["operator_source"] == "gen/1"
    assert (workspace / "runs/gen-2/next-generation.marker").read_text() == "candidate operator ran\n"
