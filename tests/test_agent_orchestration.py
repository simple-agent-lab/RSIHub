import json
from pathlib import Path

import pytest
import yaml
from conftest import git, init_fixture_workspace, init_workspace, rows_by_genid, run_evolve

from evolve.archive import RECORD_ATTEMPT_FIELD, read_events


def _last_json_line(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def _certify_baseline(workspace: Path, evolve_home: Path) -> None:
    result = run_evolve(
        "run",
        str(workspace),
        "--max-generations",
        "0",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert result.returncode == 0, result.stderr
    champion = json.loads((workspace / "best_ever.json").read_text())
    assert champion["genid"] == "0"
    assert champion["valid_parent"] is True


def _set_operator_config(workspace: Path, name: str, block: dict | None) -> None:
    path = workspace / "evolve.yaml"
    config = yaml.safe_load(path.read_text())
    if block is None:
        config["operators"].pop(name, None)
    else:
        config["operators"][name] = block
    path.write_text(yaml.safe_dump(config, sort_keys=False))


def test_operator_active_exposes_configured_capabilities(tmp_path: Path) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)

    result = run_evolve("operator", "active", str(workspace), "--json")

    assert result.returncode == 0, result.stderr
    entries = {entry["name"]: entry for entry in json.loads(result.stdout)}
    assert entries["select"] == {
        "configured": True,
        "access": "direct",
        "name": "select",
        "required": True,
        "script": str((workspace / "operators/select.py").resolve()),
        "operator": "greedy",
    }
    assert entries["analyze"]["required"] is False
    assert entries["analyze"]["implementation"] == "analyze"
    assert entries["gate"]["access"] == "finalize"
    assert entries["reflect"]["access"] == "driver"


def test_operator_run_invokes_select_and_retains_artifacts(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)

    result = run_evolve(
        "operator",
        "run",
        str(workspace),
        "select",
        "--genid",
        "1",
        "--config",
        '{"agent_note":"bounded exploration"}',
        env={"EVOLVE_HOME": str(evolve_home)},
    )

    assert result.returncode == 0, result.stderr
    summary = _last_json_line(result.stdout)
    assert summary["operator"] == "select"
    assert summary["status"] == "complete"
    assert json.loads((workspace / "runs/gen-1/parents.json").read_text()) == {"parents": ["0"]}


@pytest.mark.parametrize("reserved", ["operator", "script", "timeout_s", "config"])
def test_operator_run_rejects_framework_owned_config_overrides(tmp_path: Path, reserved: str) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)

    result = run_evolve(
        "operator",
        "run",
        str(workspace),
        "select",
        "--genid",
        "1",
        "--config",
        json.dumps({reserved: "replacement"}),
    )

    assert result.returncode == 1
    assert f"cannot replace implementation keys: {reserved}" in result.stderr


def test_verify_detects_tampered_best_ever_cache(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    (workspace / "best_ever.json").write_text('{"genid":"forged"}\n')

    result = run_evolve("verify", str(workspace), env={"EVOLVE_HOME": str(evolve_home)})

    assert result.returncode == 1
    assert "best_ever.json does not match" in result.stderr


def test_operator_run_rejects_stale_or_missing_normative_output(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    first = run_evolve(
        "operator",
        "run",
        str(workspace),
        "rollout",
        "--genid",
        "1",
        "--parent",
        "0",
        env={"EVOLVE_HOME": str(evolve_home)},
    )
    assert first.returncode == 0, first.stderr
    assert (workspace / "runs/gen-1/rollout/summary.json").is_file()
    (workspace / "operators/rollout.py").write_text("pass\n")

    repeated = run_evolve(
        "operator",
        "run",
        str(workspace),
        "rollout",
        "--genid",
        "1",
        "--parent",
        "0",
        env={"EVOLVE_HOME": str(evolve_home)},
    )

    assert repeated.returncode == 1
    assert "produced invalid output" in repeated.stderr
    assert "rollout/summary.json" in repeated.stderr
    assert not (workspace / "runs/gen-1/rollout/summary.json").exists()
    assert (workspace / "runs/gen-1/operator-attempts/rollout/attempt-1/rollout/summary.json").is_file()


def test_operator_run_binds_candidate_checkout_to_parent(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    result = run_evolve(
        "operator",
        "run",
        str(workspace),
        "rollout",
        "--genid",
        "1",
        "--parent",
        "0",
        "--checkout",
        str(unrelated),
        env={"EVOLVE_HOME": str(evolve_home)},
    )

    assert result.returncode == 1
    assert "git" in result.stderr.lower() or "workspace repository" in result.stderr


def test_analyze_run_materializes_driver_equivalent_feedback(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    _set_operator_config(workspace, "analyze", {})
    (workspace / "operators/analyze.py").write_text(
        """
from evolve.frozen import sdk
from evolve.frozen.config import Config
from evolve.frozen.interfaces import AnalyzeOperator, AnalyzeResult

class TestAnalyzer(AnalyzeOperator):
    def analyze(self, checkout, ctx):
        root = ctx.run_dir / "analyze"
        (root / "evidence").mkdir(parents=True, exist_ok=True)
        (root / "feedback.md").write_text("failure-focused advice\\n")
        (root / "evidence" / "selected.md").write_text("selected trace\\n")
        return AnalyzeResult({"cases": 1}, ["analyze/evidence/selected.md"])

if __name__ == "__main__":
    sdk.main(TestAnalyzer, config_schema=Config({}))
""".lstrip()
    )
    rollout = run_evolve(
        "operator",
        "run",
        str(workspace),
        "rollout",
        "--genid",
        "1",
        "--parent",
        "0",
        env={"EVOLVE_HOME": str(evolve_home)},
    )
    assert rollout.returncode == 0, rollout.stderr

    analyzed = run_evolve(
        "operator",
        "run",
        str(workspace),
        "analyze",
        "--genid",
        "1",
        "--parent",
        "0",
        env={"EVOLVE_HOME": str(evolve_home)},
    )

    assert analyzed.returncode == 0, analyzed.stderr
    manifest = json.loads((workspace / "runs/gen-1/feedback/manifest.json").read_text())
    assert "feedback/evidence/selected.md" in manifest
    assert (workspace / "runs/gen-1/feedback/evidence/selected.md").read_text() == "selected trace\n"


def test_agent_commit_requires_configured_validation(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents"
    evolve_home = tmp_path / "evolve-home"
    init_fixture_workspace(workspace, "hyperagents-smoke")
    _certify_baseline(workspace, evolve_home)
    child = tmp_path / "validated-candidate"
    assert run_evolve("fork", str(workspace), "0", str(child)).returncode == 0
    target = child / "target/agent.py"
    target.write_text(target.read_text() + "\n# validated outer-agent change\n")

    rejected = run_evolve(
        "commit",
        str(workspace),
        str(child),
        "--parent",
        "0",
        "--genid",
        "1",
    )
    assert rejected.returncode == 1
    assert "configured validate must run before commit" in rejected.stderr

    validated = run_evolve(
        "operator",
        "run",
        str(workspace),
        "validate",
        "--genid",
        "1",
        "--parent",
        "0",
        "--checkout",
        str(child),
        env={"EVOLVE_HOME": str(evolve_home)},
    )
    assert validated.returncode == 0, validated.stderr
    target.write_text(target.read_text() + "# changed after validation\n")
    stale = run_evolve(
        "commit",
        str(workspace),
        str(child),
        "--parent",
        "0",
        "--genid",
        "1",
    )
    assert stale.returncode == 1
    assert "candidate changed after validate" in stale.stderr
    revalidated = run_evolve(
        "operator",
        "run",
        str(workspace),
        "validate",
        "--genid",
        "1",
        "--parent",
        "0",
        "--checkout",
        str(child),
        env={"EVOLVE_HOME": str(evolve_home)},
    )
    assert revalidated.returncode == 0, revalidated.stderr
    committed = run_evolve(
        "commit",
        str(workspace),
        str(child),
        "--parent",
        "0",
        "--genid",
        "1",
    )
    assert committed.returncode == 0, committed.stderr


def test_select_runs_the_champion_operator_version(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents"
    evolve_home = tmp_path / "evolve-home"
    init_fixture_workspace(workspace, "hyperagents-smoke")
    failed_task = json.loads((workspace / "evaluator/splits.json").read_text())["tasks"]["gate"][0]
    baseline_target = workspace / "target/agent.py"
    baseline_target.write_text(baseline_target.read_text() + f"\n# FAIL {failed_task}\n")
    git(workspace, "add", "target/agent.py")
    git(workspace, "commit", "-m", "make baseline improvable")
    git(workspace, "tag", "-f", "gen/0")
    _certify_baseline(workspace, evolve_home)

    child = tmp_path / "process-candidate"
    assert run_evolve("fork", str(workspace), "0", str(child)).returncode == 0
    target = child / "target/agent.py"
    target.write_text(target.read_text().replace(f"\n# FAIL {failed_task}\n", "\n"))
    (child / "operators/select.py").write_text(
        """
from evolve.frozen import sdk
from evolve.frozen.config import Config
from evolve.frozen.interfaces import SelectOperator, SelectResult

class ChampionSelect(SelectOperator):
    def pick(self, archive, ctx):
        (ctx.run_dir / "champion-select.marker").write_text("gen/1 operator ran\\n")
        return SelectResult(["0"])

if __name__ == "__main__":
    sdk.main(ChampionSelect, config_schema=Config({}))
""".lstrip()
    )
    validated = run_evolve(
        "operator",
        "run",
        str(workspace),
        "validate",
        "--genid",
        "1",
        "--parent",
        "0",
        "--checkout",
        str(child),
        env={"EVOLVE_HOME": str(evolve_home)},
    )
    assert validated.returncode == 0, validated.stderr
    committed = run_evolve(
        "commit",
        str(workspace),
        str(child),
        "--parent",
        "0",
        "--genid",
        "1",
    )
    assert committed.returncode == 0, committed.stderr
    evaluated = run_evolve(
        "eval",
        str(workspace),
        "1",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert evaluated.returncode == 0, evaluated.stderr
    finalized = run_evolve("finalize", str(workspace), "1", env={"EVOLVE_HOME": str(evolve_home)})
    assert finalized.returncode == 0, finalized.stderr
    assert json.loads((workspace / "best_ever.json").read_text())["genid"] == "1"

    selected = run_evolve(
        "operator",
        "run",
        str(workspace),
        "select",
        "--genid",
        "2",
        env={"EVOLVE_HOME": str(evolve_home)},
    )

    assert selected.returncode == 0, selected.stderr
    assert (workspace / "runs/gen-2/champion-select.marker").read_text() == "gen/1 operator ran\n"


def test_finalize_reports_gate_failure_and_keeps_retry_pending(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents"
    evolve_home = tmp_path / "evolve-home"
    init_fixture_workspace(workspace, "hyperagents-smoke")
    _certify_baseline(workspace, evolve_home)
    _set_operator_config(workspace, "validate", None)
    child = tmp_path / "broken-gate-candidate"
    assert run_evolve("fork", str(workspace), "0", str(child)).returncode == 0
    target = child / "target/agent.py"
    target.write_text(target.read_text() + "\n# candidate with broken gate\n")
    (child / "operators/gate.py").write_text("raise SystemExit(7)\n")
    committed = run_evolve(
        "commit",
        str(workspace),
        str(child),
        "--parent",
        "0",
        "--genid",
        "1",
    )
    assert committed.returncode == 0, committed.stderr
    evaluated = run_evolve(
        "eval",
        str(workspace),
        "1",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert evaluated.returncode == 0, evaluated.stderr

    finalized = run_evolve("finalize", str(workspace), "1", env={"EVOLVE_HOME": str(evolve_home)})

    assert finalized.returncode == 1
    assert "gate failed" in finalized.stderr
    assert rows_by_genid(workspace)["1"]["pending_gate_record"] is True
    assert json.loads((workspace / "best_ever.json").read_text())["genid"] == "0"


def test_driver_and_repair_preserve_active_agent_edits(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    child = workspace / "runs/worktrees/gen-1"
    forked = run_evolve("fork", str(workspace), "0", str(child))
    assert forked.returncode == 0, forked.stderr
    target = child / "target/agent.py"
    target.write_text(target.read_text() + "\n# irreplaceable in-progress edit\n")

    competing = run_evolve(
        "run",
        str(workspace),
        "--max-generations",
        "1",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert competing.returncode == 1
    assert "active child worktree" in competing.stderr
    assert "irreplaceable" in target.read_text()

    repair = run_evolve("repair", str(workspace), env={"EVOLVE_HOME": str(evolve_home)})
    assert repair.returncode == 0, repair.stderr
    assert "preserved dirty worktree gen-1" in repair.stdout
    assert "irreplaceable" in target.read_text()

    committed = run_evolve(
        "commit",
        str(workspace),
        str(child),
        "--parent",
        "0",
        "--genid",
        "1",
    )
    assert committed.returncode == 0, committed.stderr
    assert not child.exists()


def test_repair_removes_only_clean_stale_worktree(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    child = workspace / "runs/worktrees/gen-1"
    assert run_evolve("fork", str(workspace), "0", str(child)).returncode == 0

    repair = run_evolve("repair", str(workspace), env={"EVOLVE_HOME": str(evolve_home)})

    assert repair.returncode == 0, repair.stderr
    assert "removed stale worktree gen-1" in repair.stdout
    assert not child.exists()


def test_repair_removes_clean_interrupted_evaluation_worktree(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    parent = workspace / "runs/evaluation-worktrees/evolve-eval-interrupted"
    checkout = parent / "checkout"
    git(workspace, "worktree", "add", "--detach", str(checkout), "gen/0")

    repair = run_evolve("repair", str(workspace), env={"EVOLVE_HOME": str(evolve_home)})

    assert repair.returncode == 0, repair.stderr
    assert "removed stale evaluation worktree" in repair.stdout
    assert not checkout.exists()
    assert not parent.exists()


def test_driver_detects_agent_worktree_outside_managed_directory(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    child = tmp_path / "external-agent-worktree"
    assert run_evolve("fork", str(workspace), "0", str(child)).returncode == 0

    competing = run_evolve(
        "run",
        str(workspace),
        "--max-generations",
        "1",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    repair = run_evolve("repair", str(workspace), env={"EVOLVE_HOME": str(evolve_home)})

    assert competing.returncode == 1
    assert "external-agent-worktree" in competing.stderr
    assert repair.returncode == 0, repair.stderr
    assert "preserved linked worktree outside runs/worktrees" in repair.stdout
    assert child.exists()


def test_agent_orchestrated_candidate_can_eval_and_finalize(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    child = tmp_path / "candidate"

    forked = run_evolve("fork", str(workspace), "0", str(child))
    assert forked.returncode == 0, forked.stderr
    target = child / "target/agent.py"
    target.write_text(target.read_text() + "\n# agent-orchestrated harness change\n")
    checked = run_evolve("surface-check", str(child), "--parent", "0")
    assert checked.returncode == 0, checked.stderr

    committed = run_evolve(
        "commit",
        str(workspace),
        str(child),
        "--parent",
        "0",
        "--genid",
        "1",
    )
    assert committed.returncode == 0, committed.stderr
    evaluated = run_evolve(
        "eval",
        str(workspace),
        "1",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert evaluated.returncode == 0, evaluated.stderr
    assert rows_by_genid(workspace)["1"]["pending_gate_record"] is True

    finalized = run_evolve(
        "finalize",
        str(workspace),
        "1",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )

    assert finalized.returncode == 0, finalized.stderr
    assert "gen/1: finalized" in finalized.stdout
    row = rows_by_genid(workspace)["1"]
    assert row["pending_gate_record"] is False
    assert any(
        event.get("genid") == "1" and event.get(RECORD_ATTEMPT_FIELD) is True
        for event in read_events(workspace / "archive.jsonl")
    )

    repeated = run_evolve("finalize", str(workspace), "1", env={"EVOLVE_HOME": str(evolve_home)})
    assert repeated.returncode == 0, repeated.stderr
    assert "already finalized" in repeated.stdout


def test_rerunning_a_stage_archives_downstream_outputs(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    _certify_baseline(workspace, evolve_home)
    first = run_evolve(
        "operator",
        "run",
        str(workspace),
        "rollout",
        "--genid",
        "1",
        "--parent",
        "0",
        env={"EVOLVE_HOME": str(evolve_home)},
    )
    assert first.returncode == 0, first.stderr

    stale_analysis = workspace / "runs/gen-1/analyze"
    stale_analysis.mkdir()
    (stale_analysis / "summary.json").write_text("{}\n")
    stale_feedback = workspace / "runs/gen-1/feedback"
    stale_feedback.mkdir()
    (stale_feedback / "index.md").write_text("stale\n")

    repeated = run_evolve(
        "operator",
        "run",
        str(workspace),
        "rollout",
        "--genid",
        "1",
        "--parent",
        "0",
        env={"EVOLVE_HOME": str(evolve_home)},
    )
    assert repeated.returncode == 0, repeated.stderr

    assert not stale_analysis.exists()
    assert not stale_feedback.exists()
    attempts = workspace / "runs/gen-1/operator-attempts"
    assert (attempts / "rollout/attempt-1/rollout/summary.json").is_file()
    assert (attempts / "analyze/attempt-1/analyze/summary.json").is_file()
    assert (attempts / "analyze/attempt-1/feedback/index.md").is_file()
    assert (workspace / "runs/gen-1/rollout/summary.json").is_file()
