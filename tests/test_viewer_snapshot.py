from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from evolve.viewer.models import HarborTrialLink, SourceDocument, WorkspaceSources
from evolve.viewer.snapshot import build_snapshot


def _document(workspace: Path, relative: str, value, *, age_minutes: int = 0) -> SourceDocument:
    path = workspace / relative
    mtime = int((datetime.now(UTC) - timedelta(minutes=age_minutes)).timestamp() * 1_000_000_000)
    return SourceDocument(relative_path=relative, path=path, size=10, mtime_ns=mtime, value=value)


def _sources(
    tmp_path: Path,
    *,
    rows: list[dict],
    documents: dict[str, object] | None = None,
    operators: dict | None = None,
    document_age_minutes: int = 0,
) -> WorkspaceSources:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    docs = {
        relative: _document(workspace, relative, value, age_minutes=document_age_minutes)
        for relative, value in (documents or {}).items()
    }
    config = {
        "experiment": {"id": "snapshot-test", "recipe": "ahe"},
        "target": {},
        "surface": {},
        "operators": operators
        or {
            "select": {},
            "rollout": {},
            "trace_analyzer": {},
            "meta_agent": {},
            "validate": {},
            "gate": {},
            "record": {},
        },
        "evaluator": {},
    }
    return WorkspaceSources(
        workspace=workspace,
        config=config,
        events=tuple(),
        rows=tuple(rows),
        documents=docs,
        job_roots=tuple(),
        warnings=tuple(),
        refreshed_at=datetime.now(UTC),
    )


def _stage(bundle, genid: str, name: str):
    return next(stage for stage in bundle.generation_details[genid].stages if stage.name == name)


def test_newest_candidate_controls_overview_health(tmp_path: Path) -> None:
    """Letting an older failure or auxiliary anchor dominate would misreport current health."""
    sources = _sources(
        tmp_path,
        rows=[
            {"genid": "1", "purpose": "candidate", "status": "infrastructure_failed"},
            {
                "genid": "2",
                "purpose": "candidate",
                "status": "complete",
                "score": 0.6,
                "evals": [{"kind": "anchor", "purpose": "anchor", "status": "infrastructure_failed"}],
            },
        ],
    )

    bundle = build_snapshot(sources)

    assert bundle.snapshot.experiment.focus_generation == "2"
    assert bundle.snapshot.experiment.health == "complete"
    assert bundle.snapshot.experiment.best_score == 0.6


def test_genesis_only_reports_its_canonical_evaluation_stage(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[
            {
                "genid": "0",
                "purpose": "genesis",
                "status": "complete",
                "score": 0.5,
                "expected_trials": 2,
                "task_vector": {
                    "tasks": {
                        "task-a": {"trials": [{"status": "benchmark_complete", "reward": 1.0}]},
                        "task-b": {"trials": [{"status": "benchmark_complete", "reward": 0.0}]},
                    }
                },
            }
        ],
    )

    bundle = build_snapshot(sources)
    detail = bundle.generation_details["0"]

    assert detail.summary.current_stage is None
    assert _stage(bundle, "0", "canonical_evaluation").state == "complete"
    assert _stage(bundle, "0", "canonical_evaluation").progress_completed == 2
    assert _stage(bundle, "0", "canonical_evaluation").progress_total == 2
    assert all(stage.state == "not_applicable" for stage in detail.stages if stage.name != "canonical_evaluation")


def test_passed_run_summary_keeps_normal_final_rejection_from_failing_experiment(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[{"genid": "10", "status": "rejected_validation"}],
        documents={"runs/run-summary.json": {"status": "passed", "requested_through": 10}},
    )

    experiment = build_snapshot(sources).snapshot.experiment

    assert experiment.health == "complete"


def test_legacy_run_at_configured_limit_treats_validation_rejection_as_completion(tmp_path: Path) -> None:
    sources = _sources(tmp_path, rows=[{"genid": "10", "status": "rejected_validation"}])
    sources.config["experiment"]["max_generations"] = 10

    experiment = build_snapshot(sources).snapshot.experiment

    assert experiment.health == "complete"


def test_failed_run_summary_remains_authoritative(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[{"genid": "10", "status": "complete", "score": 0.7}],
        documents={"runs/run-summary.json": {"status": "failed", "requested_through": 10}},
    )

    experiment = build_snapshot(sources).snapshot.experiment

    assert experiment.health == "failed"


def test_stage_evidence_is_recipe_aware(tmp_path: Path) -> None:
    """Treating an omitted operator as pending would leave valid recipes permanently unhealthy."""
    sources = _sources(
        tmp_path,
        rows=[{"genid": "1", "parent": "0", "status": "pending"}],
        operators={"select": {}, "rollout": {}, "meta_agent": {}, "gate": {}, "record": {}},
        documents={"runs/gen-1/rollout/summary.json": {"trials_observed": 7, "tasks_requested": 10}},
    )

    bundle = build_snapshot(sources)

    assert _stage(bundle, "1", "rollout").state == "complete"
    assert _stage(bundle, "1", "rollout").progress_completed == 7
    assert _stage(bundle, "1", "analyze").state == "not_applicable"
    assert _stage(bundle, "1", "mutate").state == "waiting"


def test_validate_rejection_is_not_passed_instead_of_complete(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[{"genid": "10", "parent": "6", "status": "rejected_validation"}],
        documents={
            "runs/gen-10/validate/result.json": {
                "accept": False,
                "reason": "GEPA minibatch did not improve: delta -2",
            }
        },
    )

    bundle = build_snapshot(sources)

    assert _stage(bundle, "10", "validate").state == "not_passed"
    assert _stage(bundle, "10", "canonical_evaluation").state == "skipped"
    assert _stage(bundle, "10", "gate").state == "skipped"


def test_change_summary_uses_rationale_paths_and_patch_stats(tmp_path: Path) -> None:
    """Dropping one artifact source would make modifications impossible to understand."""
    sources = _sources(
        tmp_path,
        rows=[{"genid": "3", "parent": "2", "status": "complete", "score": 0.5}],
        documents={
            "runs/gen-3/meta_agent/rationale.md": "Retry state transitions after tool failures.\n",
            "runs/gen-3/meta_agent/changed.json": ["target/agent.py", "target/prompt.md"],
            "runs/gen-3/meta_agent/patch.diff": (
                "--- a/target/agent.py\n+++ b/target/agent.py\n@@ -1,2 +1,3 @@\n-old\n+new\n+more\n keep\n"
            ),
        },
    )

    change = build_snapshot(sources).generation_details["3"].change

    assert change.changed_paths == ["target/agent.py", "target/prompt.md"]
    assert change.insertions == 2
    assert change.deletions == 1
    assert "retry" in (change.rationale or "").lower()
    assert change.patch_artifact_id is not None


def test_parent_delta_requires_matching_task_identity(tmp_path: Path) -> None:
    """Computing a delta across different task sets would create a false performance claim."""
    sources = _sources(
        tmp_path,
        rows=[
            {"genid": "1", "status": "complete", "score": 0.4, "task_set_hash": "set-a"},
            {"genid": "2", "parent": "1", "status": "complete", "score": 0.6, "task_set_hash": "set-b"},
        ],
    )

    performance = build_snapshot(sources).generation_details["2"].performance

    assert performance.parent_score == 0.4
    assert performance.delta is None
    assert performance.comparable is False


def test_gepa_train_score_change_is_kept_separate_from_canonical_score(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[
            {
                "genid": "4",
                "status": "complete",
                "score": 0.68,
                "gepa": {
                    "train_score_before": 29,
                    "train_score_after": 34,
                    "train_delta": 5,
                },
            }
        ],
    )

    performance = build_snapshot(sources).generation_details["4"].performance

    assert performance.score == 0.68
    assert performance.train_score_before == 29
    assert performance.train_score_after == 34
    assert performance.train_delta == 5


def test_certified_anchor_score_is_exposed_as_sealed_score(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[
            {
                "genid": "4",
                "status": "complete",
                "score": 0.68,
                "evals": [
                    {
                        "kind": "anchor",
                        "purpose": "anchor",
                        "outcome": "benchmark_complete",
                        "score": 0.75,
                        "_evolve_receipt_certified": True,
                    }
                ],
            }
        ],
    )

    performance = build_snapshot(sources).generation_details["4"].performance

    assert performance.score == 0.68
    assert performance.sealed_score == 0.75


def test_unverified_anchor_is_not_exposed_as_sealed_score(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[
            {
                "genid": "4",
                "status": "complete",
                "score": 0.68,
                "evals": [
                    {
                        "kind": "anchor",
                        "purpose": "anchor",
                        "outcome": "benchmark_complete",
                        "score": 0.75,
                    }
                ],
            }
        ],
    )

    performance = build_snapshot(sources).generation_details["4"].performance

    assert performance.sealed_score is None


def test_harbor_never_overrides_canonical_reward(tmp_path: Path) -> None:
    """Raw Harbor disagreement must remain a warning instead of changing the benchmark receipt."""
    task = "dataset__task-a"
    sources = _sources(
        tmp_path,
        rows=[
            {
                "genid": "1",
                "purpose": "candidate",
                "status": "complete",
                "task_vector": {
                    "schema_version": 1,
                    "tasks": {
                        task: {
                            "trials": [
                                {"trial": 0, "status": "benchmark_complete", "reward": 0.0, "owner": "benchmark"}
                            ]
                        }
                    },
                },
            }
        ],
    )
    links = {("1", "candidate", task, 0): HarborTrialLink(url="/jobs/job/trial", reward=1.0, duration_ms=1200)}

    trial = build_snapshot(sources, harbor_links=links).trials[0]

    assert trial.reward == 0.0
    assert trial.harbor_url == "/jobs/job/trial"
    assert trial.duration_ms == 1200
    assert {warning.code for warning in trial.warnings} == {"trial_evidence_conflict"}


def test_rollout_and_evaluation_trials_keep_distinct_purposes(tmp_path: Path) -> None:
    """Deduplicating by task alone would erase the train-versus-gate trust boundary."""
    sources = _sources(
        tmp_path,
        rows=[
            {
                "genid": "1",
                "purpose": "candidate",
                "status": "complete",
                "expected_trials": 1,
                "task_vector": {
                    "schema_version": 1,
                    "tasks": {"task-a": {"trials": [{"trial": 0, "status": "benchmark_complete", "reward": 1.0}]}},
                },
            }
        ],
        documents={
            "runs/gen-1/rollout/cases.json": [{"task": "task-a", "trial": 0, "status": "complete", "reward": 0.0}]
        },
    )

    bundle = build_snapshot(sources)
    trials = bundle.trials

    assert {(trial.purpose, trial.task) for trial in trials} == {("rollout", "task-a"), ("candidate", "task-a")}
    assert _stage(bundle, "1", "canonical_evaluation").progress_completed == 1
    assert _stage(bundle, "1", "canonical_evaluation").progress_total == 1


def test_rollout_repetitions_are_counted_per_task(tmp_path: Path) -> None:
    sources = _sources(
        tmp_path,
        rows=[{"genid": "1", "purpose": "candidate", "status": "complete"}],
        documents={
            "runs/gen-1/rollout/cases.json": [
                {"task_name": "task-a", "outcome": "complete", "reward": 1.0},
                {"task_name": "task-b", "outcome": "complete", "reward": 0.0},
            ]
        },
    )

    trials = build_snapshot(sources).trials

    assert {(trial.task, trial.repetition) for trial in trials} == {("task-a", 0), ("task-b", 0)}


def test_nonterminal_stale_generation_is_only_possibly_interrupted(tmp_path: Path) -> None:
    """Stale activity is advisory and must not be promoted into a terminal failure."""
    sources = _sources(
        tmp_path,
        rows=[{"genid": "1", "purpose": "candidate", "status": "pending"}],
        documents={"runs/gen-1/select/parents.json": {"parents": ["0"]}},
        document_age_minutes=20,
    )

    experiment = build_snapshot(sources, now=datetime.now(UTC)).snapshot.experiment

    assert experiment.health == "possibly_interrupted"
