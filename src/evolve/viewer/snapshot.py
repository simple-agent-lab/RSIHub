from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ..archive import RECEIPT_CERTIFIED_FIELD
from ..population import generation_number
from .benchmark import benchmark_label
from .models import (
    ArtifactReference,
    ArtifactTarget,
    ChangeSummary,
    ExperimentSummary,
    GenerationDetail,
    GenerationSummary,
    HarborTrialLink,
    PerformanceSummary,
    SnapshotBundle,
    StageState,
    StageSummary,
    TrialSummary,
    ViewerSnapshot,
    ViewerWarning,
    WorkspaceSources,
)

STAGE_ORDER = (
    "select",
    "rollout",
    "analyze",
    "mutate",
    "validate",
    "novelty",
    "canonical_evaluation",
    "gate",
    "record",
    "reflect",
)
_OPERATOR_FOR_STAGE = {
    "select": "select",
    "rollout": "rollout",
    "analyze": "analyze",
    "mutate": "mutate",
    "validate": "validate",
    "novelty": "novelty",
    "gate": "gate",
    "record": "record",
    "reflect": "reflect",
}
_LEGACY_ANALYZE = "trace" + "_analyzer"
_LEGACY_MUTATE = "meta" + "_agent"
_STAGE_FILES = {
    "select": ("select/parents.json",),
    "rollout": ("rollout/summary.json",),
    "analyze": ("analyze/summary.json", f"{_LEGACY_ANALYZE}/summary.json"),
    "mutate": (
        "mutate/changed.json",
        "mutate/patch.diff",
        f"{_LEGACY_MUTATE}/changed.json",
        f"{_LEGACY_MUTATE}/patch.diff",
    ),
    "validate": ("validate/result.json",),
    "novelty": ("novelty.json",),
    "gate": ("gate.json",),
    "record": ("record/fields.json",),
}
_TERMINAL_FAILURES = {
    "candidate_invalid",
    "infra_failed",
    "infrastructure_failed",
    "invalid_proposal",
    "no_proposal",
    "operator_failed",
    "rejected_duplicate",
    "rejected_validation",
}
_TERMINAL_SUCCESS = {"complete"}
_PREVIEWABLE_SUFFIXES = {".json", ".jsonl", ".md", ".diff", ".patch", ".txt", ".log"}


def build_snapshot(
    sources: WorkspaceSources,
    *,
    harbor_links: Mapping[tuple[str, str, str, int], HarborTrialLink] | None = None,
    now: datetime | None = None,
) -> SnapshotBundle:
    observed_at = now or datetime.now(UTC)
    artifacts, targets = _register_artifacts(sources)
    artifacts_by_path = {artifact.relative_path: artifact for artifact in artifacts}
    ordered_rows = _ordered_rows(sources.rows)
    trials = _canonical_trials(sources, harbor_links or {})
    trials_by_generation: dict[str, list[TrialSummary]] = {}
    for trial in trials:
        trials_by_generation.setdefault(trial.generation, []).append(trial)
    rows_by_id = {str(row.get("genid")): row for row in ordered_rows}
    details: dict[str, GenerationDetail] = {}
    for row in ordered_rows:
        genid = str(row.get("genid"))
        details[genid] = _generation_detail(
            sources,
            row,
            rows_by_id,
            trials_by_generation.get(genid, []),
            artifacts,
            artifacts_by_path,
        )
    experiment = _experiment_summary(sources, ordered_rows, details, observed_at)
    return SnapshotBundle(
        snapshot=ViewerSnapshot(
            experiment=experiment,
            generations=[detail.summary for detail in details.values()],
            trial_count=len(trials),
        ),
        generation_details=details,
        trials=tuple(trials),
        artifact_targets=targets,
        artifact_references={artifact.id: artifact for artifact in artifacts},
    )


def add_snapshot_warning(bundle: SnapshotBundle, warning: ViewerWarning) -> SnapshotBundle:
    snapshot = bundle.snapshot.model_copy(deep=True)
    snapshot.experiment.warnings.append(warning)
    return replace(bundle, snapshot=snapshot)


def _ordered_rows(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple[int, int, str]:
        genid = str(row.get("genid", ""))
        number = generation_number(genid)
        suffix = genid.split("-", 1)[1] if "-" in genid else "0"
        return (number if number is not None else -1, int(suffix) if suffix.isdigit() else 0, genid)

    return sorted((dict(row) for row in rows if row.get("genid") is not None), key=key)


def _generation_detail(
    sources: WorkspaceSources,
    row: dict[str, Any],
    rows_by_id: dict[str, dict[str, Any]],
    trials: list[TrialSummary],
    artifacts: list[ArtifactReference],
    artifacts_by_path: dict[str, ArtifactReference],
) -> GenerationDetail:
    genid = str(row["genid"])
    stages = _stages(sources, row, trials)
    change = _change_summary(sources, genid, artifacts_by_path)
    performance = _performance_summary(row, rows_by_id, trials)
    generation_artifacts = [
        artifact for artifact in artifacts if artifact.relative_path.startswith(f"runs/gen-{genid}/")
    ]
    status = str(row.get("status") or "pending")
    current_stage = next((stage.name for stage in stages if stage.state in {"waiting", "active", "unknown"}), None)
    summary = GenerationSummary(
        genid=genid,
        parent=str(row["parent"]) if row.get("parent") is not None else None,
        status=status,
        current_stage=current_stage,
        score=_number(row.get("score")),
        selection_eligible=row.get("selection_eligible") if isinstance(row.get("selection_eligible"), bool) else None,
        change_files=len(change.changed_paths),
        insertions=change.insertions,
        deletions=change.deletions,
    )
    return GenerationDetail(
        summary=summary,
        stages=stages,
        change=change,
        performance=performance,
        artifacts=generation_artifacts,
    )


def _stages(sources: WorkspaceSources, row: dict[str, Any], trials: list[TrialSummary]) -> list[StageSummary]:
    genid = str(row["genid"])
    is_genesis = row.get("purpose") == "genesis" or (genid == "0" and row.get("parent") is None)
    prefix = f"runs/gen-{genid}/"
    raw_operators = sources.config.get("operators")
    operators = cast(dict[str, Any], raw_operators) if isinstance(raw_operators, dict) else {}
    result: list[StageSummary] = []
    pre_evaluation_rejected = False
    for name in STAGE_ORDER:
        if is_genesis and name != "canonical_evaluation":
            result.append(StageSummary(name=name, state="not_applicable"))
            continue
        operator = _OPERATOR_FOR_STAGE.get(name)
        if operator == "analyze" and operator not in operators:
            operator = _LEGACY_ANALYZE
        elif operator == "mutate" and operator not in operators:
            operator = _LEGACY_MUTATE
        if operator is not None and operator not in operators:
            result.append(StageSummary(name=name, state="not_applicable"))
            continue
        if pre_evaluation_rejected and name in {"novelty", "canonical_evaluation", "gate", "reflect"}:
            result.append(StageSummary(name=name, state="skipped"))
            continue
        state: StageState = "waiting"
        completed = total = None
        if name == "select" and row.get("parent") is not None:
            state = "complete"
        elif (
            name == "canonical_evaluation"
            and str(row.get("status") or "pending") in _TERMINAL_SUCCESS | _TERMINAL_FAILURES
        ):
            state = "failed" if str(row.get("status")) in _TERMINAL_FAILURES else "complete"
        else:
            evidence = [
                document
                for relative in _STAGE_FILES.get(name, ())
                if (document := sources.documents.get(prefix + relative)) is not None
            ]
            if evidence:
                if any(document.error is not None for document in evidence):
                    state = "unknown"
                elif _stage_not_passed(name, evidence):
                    state = "not_passed"
                else:
                    state = "complete"
        if name == "rollout":
            summary = sources.documents.get(prefix + "rollout/summary.json")
            if summary is not None and isinstance(summary.value, dict):
                completed = _integer(summary.value.get("trials_observed") or summary.value.get("tasks_observed"))
                total = _integer(summary.value.get("tasks_requested"))
        if name == "canonical_evaluation" and trials:
            completed = len([trial for trial in trials if trial.purpose != "rollout"])
            total = _integer(row.get("expected_trials"))
        result.append(StageSummary(name=name, state=state, progress_completed=completed, progress_total=total))
        if name in {"validate", "novelty"} and state == "not_passed":
            pre_evaluation_rejected = True
    return result


def _stage_not_passed(name: str, evidence: list[Any]) -> bool:
    values = [document.value for document in evidence if isinstance(document.value, dict)]
    if name in {"validate", "novelty"}:
        return any(value.get("accept") is False for value in values)
    if name == "gate":
        return any(
            value.get("accept") is False
            or value.get("valid_parent") is False
            or value.get("verdict") in {"reject", "discard"}
            for value in values
        )
    return False


def _change_summary(
    sources: WorkspaceSources, genid: str, artifacts_by_path: dict[str, ArtifactReference]
) -> ChangeSummary:
    prefixes = (f"runs/gen-{genid}/mutate/", f"runs/gen-{genid}/{_LEGACY_MUTATE}/")
    prefix = next(
        (candidate for candidate in prefixes if any(relative.startswith(candidate) for relative in sources.documents)),
        prefixes[-1],
    )
    rationale = _text_value(sources, prefix + "rationale.md")
    changed_value = _value(sources, prefix + "changed.json")
    changed_paths = [str(path) for path in changed_value] if isinstance(changed_value, list) else []
    patch_relative = prefix + "patch.diff"
    patch = _text_value(sources, patch_relative) or _text_value(sources, prefix + "model_patch.diff")
    insertions = deletions = 0
    if patch:
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                insertions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
    artifact = artifacts_by_path.get(patch_relative) or artifacts_by_path.get(prefix + "model_patch.diff")
    return ChangeSummary(
        rationale=rationale.strip() if rationale else None,
        changed_paths=changed_paths,
        insertions=insertions,
        deletions=deletions,
        patch_artifact_id=artifact.id if artifact else None,
    )


def _performance_summary(
    row: dict[str, Any], rows_by_id: dict[str, dict[str, Any]], trials: list[TrialSummary]
) -> PerformanceSummary:
    parent = rows_by_id.get(str(row.get("parent"))) if row.get("parent") is not None else None
    score = _number(row.get("score"))
    parent_score = _number(parent.get("score")) if parent else None
    task_hash = str(row["task_set_hash"]) if row.get("task_set_hash") is not None else None
    parent_hash = str(parent["task_set_hash"]) if parent and parent.get("task_set_hash") is not None else None
    comparable = task_hash is not None and task_hash == parent_hash and score is not None and parent_score is not None
    outcomes = Counter(trial.status for trial in trials if trial.purpose != "rollout")
    gepa = cast(dict[str, Any], row.get("gepa")) if isinstance(row.get("gepa"), dict) else {}
    return PerformanceSummary(
        score=score,
        sealed_score=_sealed_score(row),
        parent_score=parent_score,
        delta=score - parent_score if comparable and score is not None and parent_score is not None else None,
        comparable=comparable,
        task_set_hash=task_hash,
        expected_trials=_integer(row.get("expected_trials")),
        observed_trials=len([trial for trial in trials if trial.purpose != "rollout"]),
        outcome_counts=dict(sorted(outcomes.items())),
        contract_certified=row.get("contract_certified") if isinstance(row.get("contract_certified"), bool) else None,
        cost_usd=_number(row.get("cost_usd")),
        wall_s=_number(row.get("wall_s")),
        train_score_before=_number(gepa.get("train_score_before")),
        train_score_after=_number(gepa.get("train_score_after")),
        train_delta=_number(gepa.get("train_delta")),
    )


def _sealed_score(row: dict[str, Any]) -> float | None:
    evaluations = row.get("evals")
    if not isinstance(evaluations, list):
        return None
    anchors = [
        _number(evaluation.get("score"))
        for evaluation in evaluations
        if isinstance(evaluation, dict)
        and evaluation.get("kind") == "anchor"
        and evaluation.get("purpose") == "anchor"
        and evaluation.get("outcome") == "benchmark_complete"
        and evaluation.get(RECEIPT_CERTIFIED_FIELD) is True
    ]
    return next((score for score in reversed(anchors) if score is not None), None)


def _canonical_trials(
    sources: WorkspaceSources, harbor_links: Mapping[tuple[str, str, str, int], HarborTrialLink]
) -> list[TrialSummary]:
    trials: list[TrialSummary] = []
    for row in _ordered_rows(sources.rows):
        genid = str(row["genid"])
        purpose = str(row.get("purpose") or ("genesis" if genid == "0" else "candidate"))
        vector = row.get("task_vector")
        tasks = vector.get("tasks") if isinstance(vector, dict) else None
        if not isinstance(tasks, dict):
            continue
        for task, task_value in sorted(tasks.items()):
            entries = task_value.get("trials") if isinstance(task_value, dict) else None
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if isinstance(entry, dict):
                    trials.append(_trial(genid, purpose, str(task), index, cast(dict[str, Any], entry), harbor_links))
    for relative, document in sorted(sources.documents.items()):
        if not relative.endswith("/rollout/cases.json") or not isinstance(document.value, list):
            continue
        parts = Path(relative).parts
        genid = parts[1].removeprefix("gen-")
        repetitions: Counter[str] = Counter()
        for entry in document.value:
            if not isinstance(entry, dict):
                continue
            task = str(entry.get("task") or entry.get("task_name") or entry.get("trial_name") or "unknown-case")
            repetition = repetitions[task]
            repetitions[task] += 1
            trials.append(_trial(genid, "rollout", task, repetition, cast(dict[str, Any], entry), harbor_links))
    return sorted(
        trials, key=lambda trial: (_generation_sort_key(trial.generation), trial.purpose, trial.task, trial.repetition)
    )


def _trial(
    genid: str,
    purpose: str,
    task: str,
    index: int,
    entry: dict[str, Any],
    harbor_links: Mapping[tuple[str, str, str, int], HarborTrialLink],
) -> TrialSummary:
    repetition = _integer(entry.get("trial"))
    repetition = repetition if repetition is not None else index
    reward = _number(entry.get("reward"))
    link = harbor_links.get((genid, purpose, task, repetition))
    warnings: list[ViewerWarning] = []
    if link is not None and reward is not None and link.reward is not None and not math.isclose(reward, link.reward):
        warnings.append(
            ViewerWarning(
                code="trial_evidence_conflict",
                message=f"canonical reward {reward} differs from Harbor reward {link.reward}",
                scope=f"trial:{genid}:{purpose}:{task}:{repetition}",
            )
        )
    status = str(entry.get("status") or entry.get("outcome") or ("error" if entry.get("exception") else "unknown"))
    identifier = hashlib.sha256(f"{genid}\0{purpose}\0{task}\0{repetition}".encode()).hexdigest()[:20]
    timing_s = _number(entry.get("timing_s"))
    return TrialSummary(
        id=identifier,
        generation=genid,
        purpose=purpose,
        task=task,
        repetition=repetition,
        reward=reward,
        status=status,
        owner=str(entry["owner"]) if entry.get("owner") is not None else None,
        failure_category=str(entry["failure_category"]) if entry.get("failure_category") is not None else None,
        duration_ms=link.duration_ms
        if link and link.duration_ms is not None
        else (timing_s * 1000 if timing_s else None),
        harbor_url=link.url if link else None,
        warnings=warnings,
    )


def _register_artifacts(
    sources: WorkspaceSources,
) -> tuple[list[ArtifactReference], dict[str, ArtifactTarget]]:
    artifacts: list[ArtifactReference] = []
    targets: dict[str, ArtifactTarget] = {}
    for relative, document in sorted(sources.documents.items()):
        identifier = hashlib.sha256(relative.encode()).hexdigest()[:20]
        suffix = Path(relative).suffix.lower()
        previewable = suffix in _PREVIEWABLE_SUFFIXES or Path(relative).name in {"status", "score"}
        kind = suffix.removeprefix(".") or "text"
        artifacts.append(
            ArtifactReference(
                id=identifier,
                label=Path(relative).name,
                relative_path=relative,
                kind=kind,
                size=document.size,
                previewable=previewable,
            )
        )
        targets[identifier] = ArtifactTarget(path=document.path, media_type="text/plain", size=document.size)
    return artifacts, targets


def _experiment_summary(
    sources: WorkspaceSources,
    rows: list[dict[str, Any]],
    details: dict[str, GenerationDetail],
    now: datetime,
) -> ExperimentSummary:
    focus = rows[-1] if rows else None
    focus_id = str(focus["genid"]) if focus else None
    status = str(focus.get("status") or "pending") if focus else "unknown"
    generation_documents = [
        document
        for relative, document in sources.documents.items()
        if focus_id is not None and relative.startswith(f"runs/gen-{focus_id}/")
    ]
    last_activity = max((document.mtime_ns for document in generation_documents), default=None)
    last_activity_at = datetime.fromtimestamp(last_activity / 1_000_000_000, UTC) if last_activity else None
    run_summary = _value(sources, "runs/run-summary.json")
    run_status = str(run_summary.get("status")) if isinstance(run_summary, dict) else None
    if run_status == "passed":
        health = "complete"
    elif run_status == "failed":
        health = "failed"
    elif _legacy_run_completed(sources, focus):
        health = "complete"
    elif status in _TERMINAL_SUCCESS:
        health = "complete"
    elif status in _TERMINAL_FAILURES:
        health = "failed"
    elif last_activity_at is None:
        health = "waiting"
    elif (now - last_activity_at).total_seconds() >= 900:
        health = "possibly_interrupted"
    else:
        health = "active"
    scores = [_number(row.get("score")) for row in rows]
    numeric_scores = [score for score in scores if score is not None]
    raw_experiment = sources.config.get("experiment")
    experiment = cast(dict[str, Any], raw_experiment) if isinstance(raw_experiment, dict) else {}
    return ExperimentSummary(
        id=str(experiment.get("id") or sources.workspace.name),
        workspace=str(sources.workspace),
        benchmark=benchmark_label(sources.config),
        recipe=str(experiment["recipe"]) if experiment.get("recipe") is not None else None,
        health=health,
        focus_generation=focus_id,
        current_stage=details[focus_id].summary.current_stage if focus_id in details else None,
        best_score=max(numeric_scores, default=None),
        updated_at=sources.refreshed_at,
        last_activity_at=last_activity_at,
        warnings=list(sources.warnings),
    )


def _legacy_run_completed(sources: WorkspaceSources, focus: dict[str, Any] | None) -> bool:
    if focus is None:
        return False
    raw_experiment = sources.config.get("experiment")
    experiment = cast(dict[str, Any], raw_experiment) if isinstance(raw_experiment, dict) else {}
    max_generations = _integer(experiment.get("max_generations"))
    generation = generation_number(str(focus.get("genid") or ""))
    return (
        max_generations is not None
        and generation is not None
        and generation >= max_generations
        and focus.get("status") in {"complete", "rejected_validation"}
    )


def _value(sources: WorkspaceSources, relative: str) -> Any | None:
    document = sources.documents.get(relative)
    return document.value if document is not None else None


def _text_value(sources: WorkspaceSources, relative: str) -> str | None:
    value = _value(sources, relative)
    return value if isinstance(value, str) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _generation_sort_key(genid: str) -> tuple[int, str]:
    return (generation_number(genid) or 0, genid)
