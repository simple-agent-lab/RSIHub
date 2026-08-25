from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve.viewer.models import JobRootReference
from evolve.viewer.reader import WorkspaceReader
from evolve.viewer.snapshot import build_snapshot


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "experiment"
    workspace.mkdir()
    (workspace / "evolve.yaml").write_text(
        """
experiment:
  id: viewer-test
  max_generations: 2
target: {}
surface: {}
operators:
  select: {}
  rollout: {}
  meta_agent: {}
  gate: {}
  record: {}
evaluator: {}
""".lstrip()
    )
    (workspace / "archive.jsonl").write_text('{"genid":"0","status":"complete","score":0.2}\n')
    return workspace


def test_reader_reduces_complete_lines_and_warns_on_partial_tail(tmp_path: Path) -> None:
    """Parsing the final partial line would make live archive appends blank the viewer."""
    workspace = _workspace(tmp_path)
    archive = workspace / "archive.jsonl"
    archive.write_text('{"genid":"0","status":"complete","score":0.2}\n{"genid":')

    sources = WorkspaceReader(workspace).refresh()

    assert [row["genid"] for row in sources.rows] == ["0"]
    assert {warning.code for warning in sources.warnings} == {"archive_partial_tail"}


def test_reader_discovers_jobs_without_reading_raw_trial_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Descending into raw job trees would make each three-second poll scale with trajectories."""
    workspace = _workspace(tmp_path)
    jobs = workspace / "runs/evaluations/candidate/gen-1/candidate-a/attempt-1/jobs"
    trial = jobs / "job-a/trial-a"
    trial.mkdir(parents=True)
    (jobs / "job-a/config.json").write_text("{}\n")
    raw = trial / "agent/huge.log"
    raw.parent.mkdir()
    raw.write_text("raw")
    original = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        assert path != raw
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)

    sources = WorkspaceReader(workspace).refresh()

    assert sources.job_roots == (JobRootReference(generation="1", purpose="candidate", path=jobs),)
    assert all("huge.log" not in name for name in sources.documents)


def test_reader_reuses_unchanged_document_instances(tmp_path: Path) -> None:
    """Dropping the stat cache would reparse every stable artifact on every browser poll."""
    workspace = _workspace(tmp_path)
    gate = workspace / "runs/gen-1/gate.json"
    gate.parent.mkdir(parents=True)
    gate.write_text(json.dumps({"verdict": "keep"}) + "\n")
    reader = WorkspaceReader(workspace)

    first = reader.refresh().documents["runs/gen-1/gate.json"]
    second = reader.refresh().documents["runs/gen-1/gate.json"]

    assert first is second
    assert first.value == {"verdict": "keep"}


def test_reader_includes_run_summary_as_experiment_level_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    summary = workspace / "runs/run-summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({"status": "passed", "requested_through": 2}) + "\n")

    sources = WorkspaceReader(workspace).refresh()

    assert sources.documents["runs/run-summary.json"].value["status"] == "passed"


def test_reader_relocates_recorded_rollout_jobs_after_workspace_move(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    jobs = workspace / "runs/harbor-rollouts/gen-1"
    jobs.mkdir(parents=True)
    summary = workspace / "runs/gen-1/rollout/summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(
        json.dumps(
            {
                "jobs_dir": "/old/location/experiment/runs/harbor-rollouts/gen-1",
                "tasks_requested": 1,
            }
        )
    )

    sources = WorkspaceReader(workspace).refresh()

    assert JobRootReference(generation="1", purpose="rollout", path=jobs.resolve()) in sources.job_roots


def test_reader_localizes_malformed_document_errors(tmp_path: Path) -> None:
    """One malformed stage artifact must not discard valid ledger rows."""
    workspace = _workspace(tmp_path)
    summary = workspace / "runs/gen-1/rollout/summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text("{bad json\n")

    sources = WorkspaceReader(workspace).refresh()

    assert [row["genid"] for row in sources.rows] == ["0"]
    assert sources.documents["runs/gen-1/rollout/summary.json"].value is None
    assert {warning.code for warning in sources.warnings} == {"artifact_parse_failed"}


def test_reader_rejects_non_workspace(tmp_path: Path) -> None:
    """Accepting arbitrary folders would make route confinement meaningless."""
    with pytest.raises(ValueError, match="evolve.yaml"):
        WorkspaceReader(tmp_path).refresh()


def test_large_snapshot_uses_summaries_without_reading_raw_trajectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viewer refresh cost must follow summary size, not raw Harbor trajectory volume."""
    workspace = _workspace(tmp_path)
    rows = []
    for generation in range(11):
        rows.append(
            {
                "genid": str(generation),
                "purpose": "genesis" if generation == 0 else "candidate",
                "status": "complete",
                "task_vector": {
                    "tasks": {
                        f"suite__task-{generation}": {
                            "trials": [
                                {"trial": repetition, "status": "complete", "reward": 1.0} for repetition in range(100)
                            ]
                        }
                    }
                },
            }
        )
    (workspace / "archive.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    raw = workspace / "runs/evaluations/candidate/gen-10/candidate-a/attempt-1/jobs/job/trial/agent/trajectory.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("[]")
    (raw.parents[2] / "config.json").write_text("{}\n")
    original = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        assert path != raw
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)

    bundle = build_snapshot(WorkspaceReader(workspace).refresh())

    assert len(bundle.trials) == 1100
