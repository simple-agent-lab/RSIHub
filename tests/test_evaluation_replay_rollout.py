import hashlib
import importlib.util
import json
import random
from pathlib import Path

import pytest

from evolve.frozen.interfaces import OperatorContext
from library.rollout._support import certified_replay

ROOT = Path(__file__).resolve().parents[1]


def _replay_module():
    path = ROOT / "library" / "rollout" / "parent_evaluation.py"
    spec = importlib.util.spec_from_file_location("parent_evaluation_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vendor_harbor_evidence(checkout: Path) -> Path:
    vendored = checkout / "library" / "_shared" / "harbor" / "evidence.py"
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_bytes((ROOT / "library" / "_shared" / "harbor" / "evidence.py").read_bytes())
    return vendored


class _Archive:
    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = rows

    def row(self, genid: str) -> dict | None:
        return self._rows.get(genid)

    def valid_parents(self) -> list[dict]:
        return [{"genid": genid, **row} for genid, row in self._rows.items()]


def _context(workspace: Path, *, parent: str | None) -> OperatorContext:
    checkout = workspace / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    return OperatorContext(
        workspace=workspace,
        checkout=checkout,
        run_dir=workspace / "runs" / "gen-5",
        genid="5",
        parent=parent,
        round=None,
        fan_out=1,
        config={"field_limit": 1200, "pass_threshold": 1.0},
        rng=random.Random(0),
    )


def _artifact(
    workspace: Path,
    *,
    generation: str = "3",
    attempt_number: int = 1,
    task_names: tuple[str, ...] = ("task-a",),
) -> tuple[Path, dict[str, str]]:
    attempt = workspace / "runs" / "evaluations" / "candidate" / f"gen-{generation}" / f"attempt-{attempt_number}"
    jobs = attempt / "jobs"
    jobs.mkdir(parents=True)
    trials = []
    for task_name in task_names:
        trial_name = f"{task_name}__trial-0"
        result = jobs / trial_name / "evolve-replay.json"
        result.parent.mkdir()
        result.write_text(
            json.dumps(
                {
                    "trial_name": trial_name,
                    "task_name": task_name,
                    "verifier_result": {"rewards": {"reward": 1.0}},
                }
            )
        )
        trajectory = result.parent / "agent" / "trajectory.json"
        trajectory.parent.mkdir()
        trajectory.write_text(json.dumps({"steps": [{"source": "agent", "message": "done"}]}))
        payload = result.read_bytes()
        trajectory_payload = trajectory.read_bytes()
        trials.append(
            {
                "task_name": task_name,
                "trial_name": trial_name,
                "cost_usd": 0.0,
                "files": [
                    {
                        "path": result.relative_to(jobs).as_posix(),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    },
                    {
                        "path": trajectory.relative_to(jobs).as_posix(),
                        "bytes": len(trajectory_payload),
                        "sha256": hashlib.sha256(trajectory_payload).hexdigest(),
                    },
                ],
            }
        )
    artifact = attempt / "evaluation_artifacts.json"
    artifact.write_text(json.dumps({"jobs_dir": str(jobs), "trials": trials}))
    return jobs, {
        "path": artifact.relative_to(workspace).as_posix(),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


def test_replay_uses_selected_parent_certified_evaluation(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    jobs, reference = _artifact(workspace)
    rows = {
        "3": {"genid": "3", "artifacts": reference, "score": 1.0},
        "4": {"genid": "4", "artifacts": {"path": "wrong", "sha256": "wrong"}, "score": 0.0},
    }
    observed: dict[str, object] = {}

    def collect(selected_jobs: Path, **options):
        result = next(selected_jobs.rglob("evolve-replay.json"))
        observed.update(
            {
                "jobs_is_certified_view": selected_jobs != jobs,
                "jobs": selected_jobs,
                "task_name": json.loads(result.read_text())["task_name"],
                **options,
            }
        )
        return [{"task_name": "task-a", "trial_name": "task-a__trial-0", "reward": 1.0, "outcome": "passed"}]

    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive(rows))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: collect)
    ctx = _context(workspace, parent="3")

    result = module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)

    cases = json.loads((ctx.run_dir / "rollout" / "cases.json").read_text())
    assert [case["task_name"] for case in cases] == ["task-a"]
    certified_jobs = Path(result.summary["jobs_dir"])
    assert observed.pop("jobs") == certified_jobs
    assert observed == {
        "jobs_is_certified_view": True,
        "task_name": "task-a",
        "field_limit": 1200,
        "pass_threshold": 1.0,
    }
    assert certified_jobs.is_dir()
    assert certified_jobs.is_relative_to(ctx.run_dir / "rollout" / "certified-jobs")
    assert result.summary["jobs_dirs"] == [str(certified_jobs)]
    assert result.summary["source_parent"] == "3"
    assert result.summary["tasks_observed"] == 1
    assert result.summary["trials_observed"] == 1
    assert result.summary["mean_observed_reward"] == 1.0


def test_replay_keeps_certified_atif_as_workspace_relative_reference(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _jobs, reference = _artifact(workspace)
    rows = {"3": {"genid": "3", "artifacts": reference, "score": 1.0}}
    _vendor_harbor_evidence(workspace / "checkout")
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive(rows))
    ctx = _context(workspace, parent="3")

    module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)

    [case] = json.loads((ctx.run_dir / "rollout" / "cases.json").read_text())
    trajectory = case["execution"]["trajectory"]
    retained = workspace / trajectory["path"]
    assert trajectory["status"] == "available"
    assert trajectory["format"] == "atif"
    assert retained.is_relative_to(ctx.run_dir / "rollout" / "certified-jobs")
    assert hashlib.sha256(retained.read_bytes()).hexdigest() == trajectory["sha256"]


def test_replay_rejects_trajectory_reference_digest_mismatch(tmp_path: Path) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    replay_jobs = workspace / "runs" / "gen-5" / "rollout" / "certified-jobs" / "snapshot" / "source"
    trial = replay_jobs / "task-a"
    result = trial / "evolve-replay.json"
    trajectory = trial / "agent" / "trajectory.json"
    trajectory.parent.mkdir(parents=True)
    result.write_text("{}")
    trajectory.write_text(json.dumps({"steps": []}))
    certified = {
        result.relative_to(replay_jobs).as_posix(): result.read_bytes(),
        trajectory.relative_to(replay_jobs).as_posix(): trajectory.read_bytes(),
    }
    cases = [
        {
            "result_path": str(result),
            "execution": {
                "trajectory": {
                    "format": "atif",
                    "status": "available",
                    "path": trajectory.relative_to(workspace).as_posix(),
                    "sha256": "0" * 64,
                }
            },
        }
    ]

    with pytest.raises(SystemExit, match="trajectory digest mismatch"):
        module._certify_result_paths(cases, replay_jobs, certified, workspace=workspace)


def test_replay_rejects_parent_outside_current_certified_population(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _jobs, reference = _artifact(workspace)
    archive = _Archive({"3": {"genid": "3", "artifacts": reference, "score": 1.0}})
    archive.valid_parents = lambda: []  # type: ignore[method-assign]
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: archive)
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="no currently valid certified evaluation"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_merges_base_and_repair_artifacts_for_composite_evaluation(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _base_jobs, base_reference = _artifact(workspace, generation="3", task_names=("task-a", "task-b", "task-c"))
    _repair_jobs, repair_reference = _artifact(workspace, generation="3", attempt_number=2, task_names=("task-c",))
    repair_artifact = workspace / repair_reference["path"]
    repair_attempt = repair_artifact.parent
    composite = repair_attempt / "composite_evaluation_artifacts.json"
    composite.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "failed_task_repair",
                "base_artifacts": base_reference,
                "repair_artifacts": repair_reference,
                "replaced_slots": [
                    {
                        "task_id": "task-c",
                        "trial": 0,
                        "from_attempt": 1,
                        "to_attempt": 2,
                    }
                ],
            }
        )
    )
    composite_reference = {
        "path": composite.relative_to(workspace).as_posix(),
        "sha256": hashlib.sha256(composite.read_bytes()).hexdigest(),
    }
    rows = {"3": {"genid": "3", "artifacts": composite_reference, "score": 2 / 3}}

    def collect(selected_jobs: Path, **_options):
        task_names = {
            json.loads(result.read_text())["task_name"] for result in selected_jobs.rglob("evolve-replay.json")
        }
        if task_names == {"task-a", "task-b", "task-c"}:
            return [
                {
                    "task_name": "task-a",
                    "trial_name": "task-a__base",
                    "reward": 1.0,
                    "outcome": "passed",
                },
                {
                    "task_name": "task-b",
                    "trial_name": "task-b__base",
                    "reward": 0.0,
                    "outcome": "failed",
                },
                {
                    "task_name": "task-c",
                    "trial_name": "task-c__base",
                    "reward": None,
                    "outcome": "infra_error",
                },
            ]
        assert task_names == {"task-c"}
        return [
            {
                "task_name": "task-c",
                "trial_name": "task-c__repair",
                "reward": 1.0,
                "outcome": "passed",
            }
        ]

    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive(rows))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: collect)
    ctx = _context(workspace, parent="3")

    result = module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)

    cases = json.loads((ctx.run_dir / "rollout" / "cases.json").read_text())
    assert [(case["task_name"], case["trial_name"]) for case in cases] == [
        ("task-a", "task-a__base"),
        ("task-b", "task-b__base"),
        ("task-c", "task-c__repair"),
    ]
    assert result.summary["tasks_observed"] == 3
    assert result.summary["trials_observed"] == 3
    assert result.summary["mean_observed_reward"] == pytest.approx(2 / 3)
    certified_sources = [Path(path) for path in result.summary["jobs_dirs"]]
    assert len(certified_sources) == 2
    assert all(path.is_dir() for path in certified_sources)
    assert all(path.is_relative_to(ctx.run_dir / "rollout" / "certified-jobs") for path in certified_sources)
    assert result.summary["jobs_dir"] == str(certified_sources[-1])


def test_replay_loads_collector_from_vendored_workspace_library(tmp_path: Path) -> None:
    module = _replay_module()
    checkout = tmp_path / "checkout"
    _vendor_harbor_evidence(checkout)
    jobs = tmp_path / "jobs"
    trial = jobs / "trial-a"
    trial.mkdir(parents=True)
    (trial / "evolve-replay.json").write_text(
        json.dumps(
            {
                "trial_name": "trial-a",
                "task_name": "task-a",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )

    cases = module._load_collect_cases(checkout)(jobs)

    assert [(case["task_name"], case["outcome"]) for case in cases] == [("task-a", "passed")]


@pytest.mark.parametrize(
    ("parent", "row", "message"),
    [
        (None, None, "evaluation replay requires a selected parent"),
        ("3", None, "selected parent 3 is missing from archive"),
        ("3", {"genid": "3"}, "selected parent 3 has no certified evaluation artifacts"),
        (
            "3",
            {"genid": "3", "artifacts": {"path": "missing.json", "sha256": "0" * 64}},
            "artifact path is missing",
        ),
    ],
)
def test_replay_rejects_missing_parent_evidence(tmp_path: Path, monkeypatch, parent, row, message: str) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rows = {} if row is None else {"3": row}
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive(rows))

    with pytest.raises(SystemExit, match=message):
        module.EvaluationReplayRollout().rollout(
            _context(workspace, parent=parent).checkout, _context(workspace, parent=parent)
        )


def test_replay_rejects_artifact_digest_mismatch(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _jobs, reference = _artifact(workspace)
    reference["sha256"] = "0" * 64
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="artifact digest mismatch"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_rejects_modified_indexed_result_without_changing_index_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    jobs, reference = _artifact(workspace)
    result = next(jobs.rglob("evolve-replay.json"))
    original = result.read_bytes()
    modified = original.replace(b'"reward": 1.0', b'"reward": 0.0')
    assert len(modified) == len(original)
    result.write_bytes(modified)
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: lambda *_args, **_kwargs: [])
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="evaluation artifact file digest mismatch"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_rejects_indexed_file_size_mismatch(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _jobs, reference = _artifact(workspace)
    artifact = workspace / reference["path"]
    index = json.loads(artifact.read_text())
    index["trials"][0]["files"][0]["bytes"] += 1
    artifact.write_text(json.dumps(index))
    reference["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: lambda *_args, **_kwargs: [])
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="evaluation artifact file size mismatch"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_rejects_missing_indexed_file(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    jobs, reference = _artifact(workspace)
    next(jobs.rglob("evolve-replay.json")).unlink()
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: lambda *_args, **_kwargs: [])
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="evaluation artifact file is missing"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_rejects_indexed_path_outside_jobs(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _jobs, reference = _artifact(workspace)
    artifact = workspace / reference["path"]
    index = json.loads(artifact.read_text())
    index["trials"][0]["files"][0]["path"] = "../evolve-replay.json"
    artifact.write_text(json.dumps(index))
    reference["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: lambda *_args, **_kwargs: [])
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="file path escapes jobs"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_rejects_unindexed_result_file(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    jobs, reference = _artifact(workspace)
    forged = jobs / "forged" / "evolve-replay.json"
    forged.parent.mkdir()
    forged.write_text(
        json.dumps(
            {
                "trial_name": "forged__trial-0",
                "task_name": "forged",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: lambda *_args, **_kwargs: [])
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="unindexed replay result: forged/evolve-replay.json"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_does_not_materialize_raw_verifier_reward(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    jobs, reference = _artifact(workspace)
    reward = next(jobs.iterdir()) / "verifier" / "reward.txt"
    reward.parent.mkdir()
    reward.write_text("0\n")
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))

    def collect(selected_jobs: Path, **_kwargs):
        assert not list(selected_jobs.rglob("reward.txt"))
        return [{"task_name": "task-a", "trial_name": "task-a__trial-0", "reward": 1.0, "outcome": "passed"}]

    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: collect)
    ctx = _context(workspace, parent="3")

    module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_rejects_unindexed_verifier_diagnostic_file(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    jobs, reference = _artifact(workspace)
    diagnostics = next(jobs.iterdir()) / "verifier" / "diagnostics.json"
    diagnostics.parent.mkdir()
    diagnostics.write_text('{"status":"mismatch"}\n')
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: lambda *_args, **_kwargs: [])
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="unindexed replay file: task-a__trial-0/verifier/diagnostics.json"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)


def test_replay_collector_and_returned_paths_stay_in_persistent_certified_view(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    jobs, reference = _artifact(workspace)
    unindexed = next(jobs.iterdir()) / "agent" / "sessions" / "forged.jsonl"
    unindexed.parent.mkdir(parents=True)
    unindexed.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "uncertified trace"},
            }
        )
        + "\n"
    )
    aggregate = jobs / "aggregate" / "result.json"
    aggregate.parent.mkdir()
    aggregate.write_text(json.dumps({"status": "complete"}))
    _vendor_harbor_evidence(workspace / "checkout")
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    ctx = _context(workspace, parent="3")

    replay = module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)

    cases = json.loads((ctx.run_dir / "rollout" / "cases.json").read_text())
    certified_jobs = Path(replay.summary["jobs_dir"])
    assert cases[0]["agent_messages"] == ["done"]
    assert cases[0]["artifact_inventory"] == {"agent": ["trajectory.json"], "verifier": []}
    assert certified_jobs.is_dir()
    assert certified_jobs != jobs
    assert certified_jobs.is_relative_to(ctx.run_dir / "rollout" / "certified-jobs")
    assert Path(cases[0]["result_path"]) == certified_jobs / "task-a__trial-0" / "evolve-replay.json"
    assert Path(cases[0]["result_path"]).is_file()
    assert certified_jobs.stat().st_mode & 0o222 == 0
    assert Path(cases[0]["result_path"]).stat().st_mode & 0o222 == 0
    assert replay.summary["jobs_dirs"] == [str(certified_jobs)]


def test_replay_uses_verified_index_snapshot_when_index_mutates_after_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _jobs, reference = _artifact(workspace)
    artifact = workspace / reference["path"]
    rows = {"3": {"genid": "3", "artifacts": reference, "score": 1.0}}
    real_sources = certified_replay._artifact_sources

    def mutate_after_sources(*args, **kwargs):
        sources = real_sources(*args, **kwargs)
        artifact.write_text("{mutated after digest validation")
        return sources

    def collect(selected_jobs: Path, **_options):
        result = next(selected_jobs.rglob("evolve-replay.json"))
        payload = json.loads(result.read_text())
        return [
            {
                "task_name": payload["task_name"],
                "trial_name": payload["trial_name"],
                "reward": 1.0,
                "outcome": "passed",
                "result_path": str(result),
            }
        ]

    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive(rows))
    monkeypatch.setattr(certified_replay, "_artifact_sources", mutate_after_sources)
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: collect)
    ctx = _context(workspace, parent="3")

    replay = module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)

    cases = json.loads((ctx.run_dir / "rollout" / "cases.json").read_text())
    certified_jobs = Path(replay.summary["jobs_dir"])
    assert cases[0]["task_name"] == "task-a"
    assert Path(cases[0]["result_path"]).is_relative_to(certified_jobs)


def test_replay_rejects_jobs_path_without_trial_results(tmp_path: Path, monkeypatch) -> None:
    module = _replay_module()
    workspace = tmp_path / "workspace"
    _jobs, reference = _artifact(workspace)
    monkeypatch.setattr(module, "ArchiveView", lambda _workspace: _Archive({"3": {"artifacts": reference}}))
    monkeypatch.setattr(module, "_load_collect_cases", lambda _checkout: lambda *_args, **_kwargs: [])
    ctx = _context(workspace, parent="3")

    with pytest.raises(SystemExit, match="produced no trial results"):
        module.EvaluationReplayRollout().rollout(ctx.checkout, ctx)
