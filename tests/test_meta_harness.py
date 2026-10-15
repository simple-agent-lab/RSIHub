import hashlib
import importlib.util
import json
import random
import subprocess
from pathlib import Path

from evolve.frozen.interfaces import OperatorContext

ROOT = Path(__file__).resolve().parents[1]


def _load(stage: str):
    spec = importlib.util.spec_from_file_location(
        f"meta_harness_{stage}_under_test",
        ROOT / "library" / stage / "meta_harness.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class _Archive:
    def __init__(self, rows: list[dict], certified: set[str]) -> None:
        self._rows = rows
        self._certified = certified

    def rows(self) -> list[dict]:
        return self._rows

    def valid_parents(self) -> list[dict]:
        return [row for row in self._rows if row["genid"] in self._certified]


def _artifact(workspace: Path, generation: str, rewards: dict[str, float]) -> dict[str, str]:
    attempt = workspace / "runs" / "evaluations" / "candidate" / f"gen-{generation}" / "attempt-1"
    jobs = attempt / "jobs"
    jobs.mkdir(parents=True)
    trials = []
    for task_name, reward in rewards.items():
        trial_name = f"{task_name}__trial-0"
        result = jobs / trial_name / "evolve-replay.json"
        result.parent.mkdir()
        result.write_text(json.dumps({"trial_name": trial_name, "task_name": task_name, "reward": reward}))
        payload = result.read_bytes()
        trials.append(
            {
                "task_name": task_name,
                "trial_name": trial_name,
                "files": [
                    {
                        "path": result.relative_to(jobs).as_posix(),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        )
    artifact = attempt / "evaluation_artifacts.json"
    artifact.write_text(json.dumps({"jobs_dir": str(jobs), "trials": trials}))
    return {
        "path": artifact.relative_to(workspace).as_posix(),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


def _collect(jobs: Path, **_options) -> list[dict]:
    cases = []
    for result in sorted(jobs.rglob("evolve-replay.json")):
        payload = json.loads(result.read_text())
        cases.append(
            {
                "task_name": payload["task_name"],
                "trial_name": payload["trial_name"],
                "reward": payload["reward"],
                "outcome": "passed" if payload["reward"] >= 1.0 else "failed",
                "result_path": str(result),
            }
        )
    return cases


def _workspace(tmp_path: Path) -> tuple[Path, list[dict]]:
    workspace = tmp_path / "workspace"
    (workspace / "target").mkdir(parents=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "config", "user.email", "test@example.invalid")
    for genid, body in (("0", "seed\n"), ("1", "seed\nretry on timeout\n"), ("2", "seed\nbroken\n")):
        if genid == "2":
            _git(workspace, "checkout", "-q", "gen/0")
        (workspace / "target" / "agent.py").write_text(body)
        _git(workspace, "add", "target")
        _git(workspace, "commit", "-qm", f"gen {genid}")
        _git(workspace, "tag", f"gen/{genid}")
    rows = [
        {"genid": "0", "parent": None, "score": 0.5, "status": "evaluated", "tag": "gen/0"},
        {"genid": "1", "parent": "0", "score": 0.75, "status": "evaluated", "tag": "gen/1"},
        {"genid": "2", "parent": "0", "score": None, "status": "operator_failed", "tag": "gen/2"},
    ]
    rows[0]["artifacts"] = _artifact(workspace, "0", {"task-a": 1.0, "task-b": 0.0})
    rows[1]["artifacts"] = _artifact(workspace, "1", {"task-a": 0.0, "task-b": 1.0})
    handoff = workspace / "artifacts" / "generations" / "1" / "handoff.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("HYPOTHESIS: retries fix timeouts\n")
    return workspace, rows


def _ctx(workspace: Path, genid: str, parent: str, config: dict | None = None) -> OperatorContext:
    checkout = workspace.parent / f"checkout-{genid}"
    checkout.mkdir(exist_ok=True)
    return OperatorContext(
        workspace=workspace,
        checkout=checkout,
        run_dir=workspace / "runs" / f"gen-{genid}",
        genid=genid,
        parent=parent,
        round=None,
        fan_out=1,
        config=config or {"field_limit": 2000, "pass_threshold": 1.0},
        rng=random.Random(0),
    )


def _rollout(module, monkeypatch, workspace: Path, rows: list[dict], genid: str, parent: str):
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive(rows, {"0", "1"}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: _collect)
    ctx = _ctx(workspace, genid, parent)
    return ctx, module.MetaHarnessRollout().rollout(ctx.checkout, ctx)


def test_rollout_builds_a_store_of_every_candidate(tmp_path: Path, monkeypatch) -> None:
    module = _load("rollout")
    workspace, rows = _workspace(tmp_path)

    ctx, result = _rollout(module, monkeypatch, workspace, rows, genid="3", parent="1")

    store = ctx.run_dir / "rollout" / "candidates"
    parent_cases = json.loads((ctx.run_dir / "rollout" / "cases.json").read_text())
    assert {case["task_name"]: case["reward"] for case in parent_cases} == {"task-a": 0.0, "task-b": 1.0}
    index = {entry["genid"]: entry for entry in json.loads((store / "index.json").read_text())["candidates"]}
    assert index["1"]["cases"] == "runs/gen-3/rollout/cases.json"
    assert index["0"]["cases"] == "runs/gen-3/rollout/candidates/gen-0/cases.json"
    assert index["0"]["per_task"] == {"task-a": 1.0, "task-b": 0.0}
    assert "cases" not in index["2"] and index["2"]["certified"] is False
    assert index["1"]["handoff"] == "artifacts/generations/1/handoff.md"
    assert "+retry on timeout" in (workspace / index["1"]["diff_vs_parent"]).read_text()
    assert "+broken" in (workspace / index["2"]["diff_vs_seed"]).read_text()
    assert "diff_vs_seed" not in index["0"]
    frontier = json.loads((store / "frontier.json").read_text())
    assert frontier["best"] == {"genid": "1", "score": 0.75}
    assert frontier["tasks"]["task-a"] == {"reward": 1.0, "candidates": ["0"]}
    assert frontier["tasks"]["task-b"] == {"reward": 1.0, "candidates": ["1"]}
    assert "| 2 | 0 | None | operator_failed | - | not replayed |" in (store / "index.md").read_text()
    assert result.summary["variant"] == "meta_harness"
    assert result.summary["source_parent"] == "1"
    assert result.summary["candidates_replayed"] == ["0"]


def test_rollout_reuses_earlier_replays_instead_of_copying_traces_again(tmp_path: Path, monkeypatch) -> None:
    module = _load("rollout")
    workspace, rows = _workspace(tmp_path)
    _rollout(module, monkeypatch, workspace, rows, genid="3", parent="1")

    ctx, result = _rollout(module, monkeypatch, workspace, rows, genid="4", parent="1")

    index = {
        entry["genid"]: entry
        for entry in json.loads((ctx.run_dir / "rollout" / "candidates" / "index.json").read_text())["candidates"]
    }
    assert index["0"]["cases"] == "runs/gen-3/rollout/candidates/gen-0/cases.json"
    assert index["0"]["per_task"] == {"task-a": 1.0, "task-b": 0.0}
    assert result.summary["candidates_replayed"] == []
    assert not (ctx.run_dir / "rollout" / "candidates" / "gen-0" / "cases.json").exists()


def test_mutate_prompt_points_to_the_candidate_store_and_proposer_rules(tmp_path: Path) -> None:
    module = _load("mutate")
    workspace = tmp_path / "workspace"
    checkout = tmp_path / "checkout"
    (checkout / "target").mkdir(parents=True)
    (checkout / "target" / "agent.py").write_text("print('parent')\n")
    (checkout / "evolve.yaml").write_text("surface:\n  include:\n    - target/**\n  exclude: []\n")
    _git(checkout, "init", "-q")
    workspace.mkdir()
    ctx = _ctx(workspace, "3", "1", {"runner": "harbor", "timeout_s": 30})

    prompt = module.build_prompt(checkout, "fallback observation", ctx)

    assert prompt.startswith("# Meta-Harness Proposer")
    assert "/app/task/workspace/runs/gen-3/rollout/candidates/index.md" in prompt
    assert "/app/task/workspace/runs/gen-3/rollout/candidates/frontier.json" in prompt
    assert "/app/task/workspace/artifacts/generations/3/handoff.md" in prompt
    assert "Deep-read failed AND successful traces across several candidates" in prompt
    assert "One mechanism per candidate" in prompt
    assert "Never mention task names" in prompt
    assert "You CAN modify any file under `target/`" in prompt
    assert "fallback observation" not in prompt
