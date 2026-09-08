import json
import stat
from pathlib import Path

import pytest
from conftest import git, init_workspace, run_evolve, smoke_agent_command

from evolve.archive import MECHANISM_EVAL_FIELD, read_events, rows_by_genid
from evolve.driver import RunOptions, _maybe_final_anchor, run
from evolve.feedback import write_feedback_bundle


def _lifecycle_workspace(tmp_path: Path, outcomes: dict[str, list[str]]) -> Path:
    workspace, _evolve_home = init_workspace(tmp_path)
    script = workspace / "evaluator/eval.sh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        f"outcomes = {outcomes!r}\n"
        "run_dir = Path(os.environ['EVOLVE_RUN_DIR'])\n"
        "run_dir.mkdir(parents=True, exist_ok=True)\n"
        "purpose = os.environ['EVOLVE_EVAL_KIND']\n"
        "attempt = int(run_dir.name[len('attempt-'):])\n"
        "sequence = outcomes.get(\n"
        "    purpose, ['benchmark_complete'] if purpose == 'anchor' else outcomes.get('candidate', ['benchmark_complete'])\n"
        ")\n"
        "tasks = json.loads(Path(os.environ['EVOLVE_RUN_PLAN']).read_text())['tasks']\n"
        "outcome = sequence[min(attempt - 1, len(sequence) - 1)]\n"
        "owner = 'candidate' if outcome == 'candidate_invalid' else 'benchmark_agent'\n"
        "reward = 1.0 if outcome == 'benchmark_complete' else (0.0 if outcome == 'timeout' else None)\n"
        "vector = {'schema_version': 1, 'tasks': {task: {'trials': [{\n"
        "    'trial': 0, 'status': outcome, 'reward': reward, 'owner': owner,\n"
        "}]} for task in tasks}}\n"
        "(run_dir / 'task_vector.json').write_text(json.dumps(vector) + '\\n')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    config = workspace / "evolve.yaml"
    config.write_text(config.read_text().replace("tasks_per_round: 16", "tasks_per_round: 1"))
    splits_path = workspace / "evaluator/splits.json"
    splits = json.loads(splits_path.read_text())
    splits["gate_tasks_per_round"] = 1
    splits_path.write_text(json.dumps(splits) + "\n")
    git(workspace, "add", "evaluator/eval.sh", "evaluator/splits.json", "evolve.yaml")
    git(workspace, "commit", "-m", "configure lifecycle evaluator")
    git(workspace, "tag", "-f", "gen/0")
    return workspace


def _evaluation_events(workspace: Path, genid: str) -> list[dict[str, object]]:
    return [
        event
        for event in read_events(workspace / "archive.jsonl")
        if str(event.get("genid")) == genid and event.get(MECHANISM_EVAL_FIELD) is True
    ]


def test_run_evaluates_genesis_gate_and_private_sealed_anchor_once(tmp_path: Path) -> None:
    workspace = _lifecycle_workspace(
        tmp_path,
        {
            "genesis": ["benchmark_complete"],
            "anchor": ["benchmark_complete"],
        },
    )

    run(RunOptions(workspace, max_generations=0))
    run(RunOptions(workspace, max_generations=0))

    events = _evaluation_events(workspace, "0")
    assert [event["purpose"] for event in events] == ["genesis", "anchor"]
    splits = json.loads((workspace / "evaluator/splits.json").read_text())["tasks"]
    assert set(events[0]["task_set_members"]) == set(splits["gate"][:1])
    assert set(events[1]["task_set_members"]) == set(splits["sealed"])
    row = rows_by_genid(workspace)["0"]
    assert row["purpose"] == "genesis"
    anchors = [entry for entry in row["evals"] if entry.get("kind") == "anchor"]
    assert len(anchors) == 1
    assert anchors[0]["purpose"] == "anchor"
    assert anchors[0]["selection_eligible"] is False

    feedback_run = workspace / "runs" / "gen-privacy-check"
    write_feedback_bundle(workspace=workspace, run_dir=feedback_run)
    visible_feedback = "\n".join(
        path.read_text(errors="ignore") for path in (feedback_run / "feedback").rglob("*") if path.is_file()
    )
    assert not any(task in visible_feedback for task in splits["sealed"])
    assert '"purpose": "anchor"' not in visible_feedback


def test_ordinary_run_keeps_genesis_anchor_when_final_anchor_is_disabled(tmp_path: Path) -> None:
    workspace = _lifecycle_workspace(
        tmp_path,
        {
            "genesis": ["benchmark_complete"],
            "anchor": ["benchmark_complete"],
        },
    )
    config_path = workspace / "evolve.yaml"
    config_path.write_text(config_path.read_text().replace("final: true", "final: false"))

    run(RunOptions(workspace, max_generations=0))

    assert [event["purpose"] for event in _evaluation_events(workspace, "0")] == ["genesis", "anchor"]


def test_final_anchor_is_skipped_when_sealed_split_is_empty(tmp_path: Path) -> None:
    workspace, evolve_home = init_workspace(tmp_path)
    splits_path = workspace / "evaluator/splits.json"
    splits = json.loads(splits_path.read_text())
    splits["tasks"]["train"].extend(splits["tasks"]["sealed"])
    splits["tasks"]["sealed"] = []
    splits_path.write_text(json.dumps(splits) + "\n")
    git(workspace, "add", "evaluator/splits.json")
    git(workspace, "commit", "-m", "configure an empty sealed split")
    git(workspace, "tag", "-f", "gen/0")
    result = run_evolve(
        "eval",
        str(workspace),
        "0",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert result.returncode == 0, result.stderr

    _maybe_final_anchor(workspace, 1)

    assert [event["purpose"] for event in _evaluation_events(workspace, "0")] == ["candidate"]


def test_genesis_sealed_anchor_failure_stops_before_first_generation(tmp_path: Path) -> None:
    workspace = _lifecycle_workspace(
        tmp_path,
        {
            "genesis": ["benchmark_complete"],
            "anchor": ["infrastructure_failed"],
        },
    )

    with pytest.raises(RuntimeError, match="genesis sealed anchor infrastructure_failed"):
        run(RunOptions(workspace, max_generations=1))

    assert not (workspace / "runs/gen-1").exists()
    assert git(workspace, "tag", "--list", "gen/1") == ""


def test_candidate_infrastructure_failure_is_recorded_without_automatic_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _lifecycle_workspace(
        tmp_path,
        {
            "genesis": ["benchmark_complete"],
            "candidate": ["infrastructure_failed", "benchmark_complete"],
        },
    )
    monkeypatch.setenv("EVOLVE_AGENT_COMMAND", smoke_agent_command())

    run(RunOptions(workspace, max_generations=1, children_per_gen=1))

    run(RunOptions(workspace, max_generations=1, children_per_gen=1))

    attempts = _evaluation_events(workspace, "1")
    assert [event["attempt"] for event in attempts] == [1]
    assert attempts[0]["outcome"] == "infrastructure_failed"
    assert attempts[0]["retry_of"] is None
    assert "source_attempts" not in attempts[0]
    assert "repaired_tasks" not in attempts[0]
    assert rows_by_genid(workspace)["1"]["attempt"] == 1
    assert rows_by_genid(workspace)["1"]["status"] == "infrastructure_failed"


def test_agent_prepare_uses_development_only_then_continuous_seal_compares_baseline(tmp_path: Path) -> None:
    from evolve.agent_driver import AgentLimits, execute_action, parse_action, seal_submitted, start_session
    from evolve.orchestration import prepare_agent_baseline

    workspace = _lifecycle_workspace(tmp_path, {"genesis": ["benchmark_complete"], "anchor": ["benchmark_complete"]})
    prepare_agent_baseline(workspace)
    prepare_agent_baseline(workspace)
    assert [e["purpose"] for e in _evaluation_events(workspace, "0")] == ["genesis"]
    method = tmp_path / "method"
    method.mkdir()
    (method / "instructions.md").write_text("research")
    start_session(workspace, AgentLimits(10, 3, 3), mode="continuous", optimizer=method, objective="improve")
    with pytest.raises(RuntimeError, match="sealed evaluation requires"):
        seal_submitted(workspace)
    execute_action(workspace, parse_action({"id": "finish", "type": "finish_research", "reason": "complete"}))
    result = seal_submitted(workspace)
    assert result["baseline"]["sealed"] == "benchmark_complete"
    assert result["final"] == result["baseline"]
    assert seal_submitted(workspace)["final"]["sealed"] == "already complete"
    assert [e["purpose"] for e in _evaluation_events(workspace, "0")] == ["genesis", "anchor"]
