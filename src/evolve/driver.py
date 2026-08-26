from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import (
    EVALUATION_FIELDS,
    LEGACY_WRITE_BLOCKED_FIELDS,
    RECORD_ATTEMPT_FIELD,
    RESERVED_AUXILIARY_FIELDS,
    STAMPED_FIELDS,
    append_evaluation_record,
    append_event,
    archive_path,
    ensure_local_archive,
    eval_receipt_path,
    mirror_path,
    read_events,
    rows_by_genid,
)
from .candidate.snapshot import build_candidate_snapshot, commit_candidate_snapshot
from .config import evaluator_anchor, evaluator_sampling, experiment_id, operator_blocks, operator_runtime_config
from .doctor import ensure_evaluator_ready
from .evaluation import (
    CANONICAL_OUTCOMES,
    EvaluationRecord,
    Outcome,
    evaluation_status,
)
from .evaluation.execution import EvaluationInterrupted, evaluate
from .feedback import write_feedback_bundle
from .frozen.interfaces import (
    ArchiveView,
    PayloadValidationError,
    validate_gate_file_payload,
    validate_mutate_usage_payload,
    validate_novelty_file_payload,
    validate_record_fields_payload,
    validate_rollout_artifacts_payload,
    validate_rollout_summary_payload,
    validate_select_payload,
    validate_validate_file_payload,
)
from .git import (
    add_worktree,
    create_tag,
    dirty_paths,
    git,
    git_common_dir,
    git_stdout,
    head_commit,
    remove_worktree,
    tag_exists,
    working_tree_changed_paths,
    worktree_paths,
)
from .operators import OperatorResult, operator_timeout, run_operator
from .population import best_row, fixed_evaluation_identity, format_genid, generation_number, valid_genid
from .report import materialize_best_ever
from .splits import load_manifest, selected_task_names
from .surface import check_paths, surface_patterns

PENDING_GATE_RECORD_NOTE = "mechanism evaluation recorded before gate/record"
TERMINAL_STATUSES = {
    "complete",
    "partial",
    "invalid_proposal",
    "no_proposal",
    "operator_failed",
    "rejected_admission",
    "rejected_duplicate",
    "rejected_validation",
    Outcome.CANDIDATE_INVALID.value,
    Outcome.INFRASTRUCTURE_FAILED.value,
    Outcome.TIMEOUT.value,
    Outcome.CANCELLED.value,
}
UNRETRYABLE_STATUSES = TERMINAL_STATUSES - {"complete", "partial"}
RECORD_FORBIDDEN_FIELDS = EVALUATION_FIELDS | RESERVED_AUXILIARY_FIELDS | LEGACY_WRITE_BLOCKED_FIELDS
RECORD_ANNOTATION_FIELDS = frozenset({"note", "predicted_fixes", "verified_fixes"})


@dataclass(frozen=True)
class RunOptions:
    workspace: Path
    max_generations: int
    children_per_gen: int = 1


@dataclass(frozen=True)
class OperatorOutputError:
    path: Path
    field: str
    detail: str


@contextmanager
def workspace_run_lock(workspace: Path) -> Iterator[None]:
    lock_path = workspace / "runs" / ".driver.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            raise RuntimeError(
                f"another evolve run already owns workspace {workspace} (pid {owner}); "
                "stop it before starting a second run"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run(options: RunOptions) -> None:
    workspace = options.workspace.resolve()
    with workspace_run_lock(workspace):
        ensure_evaluator_ready(workspace)
        _run_locked(options, workspace)


def _run_locked(options: RunOptions, workspace: Path) -> None:
    if options.children_per_gen < 1:
        raise RuntimeError("children_per_gen must be at least 1")
    exp_id = experiment_id(workspace)
    ensure_local_archive(workspace, exp_id)
    evaluator_sampling(workspace)
    active_worktrees = _workspace_child_worktrees(workspace)
    if active_worktrees:
        names = ", ".join(path.name for path in active_worktrees)
        raise RuntimeError(
            f"active child worktree(s): {names}; finish the agent commit, "
            "or run evolve repair after preserving any edits"
        )
    _ensure_genesis_evaluated(workspace)
    _ensure_genesis_anchor_evaluated(workspace)
    operators_config = operator_blocks(workspace)

    for gen in range(1, options.max_generations + 1):
        genids = [
            format_genid(gen, child_index, options.children_per_gen) for child_index in range(options.children_per_gen)
        ]
        rows = rows_by_genid(workspace)
        pending_eval_gate_record = _evaluation_pending_gate_record_genids(workspace)
        pending = [
            genid for genid in genids if _generation_is_pending(rows.get(genid, {}), genid in pending_eval_gate_record)
        ]
        if not pending:
            continue

        round_number = None
        selected = _select_generation_parents(
            workspace,
            exp_id,
            gen,
            pending,
            options.children_per_gen,
            operators_config,
        )
        for genid in genids:
            row = rows_by_genid(workspace).get(genid, {})
            pending_eval_gate_record = genid in _evaluation_pending_gate_record_genids(workspace)
            if not _generation_is_pending(row, pending_eval_gate_record):
                continue
            tag = f"gen/{genid}"
            parent = selected.get(genid)
            if parent is None and tag_exists(workspace, tag):
                existing_parent = row.get("parent")
                parent = str(existing_parent) if existing_parent is not None else None
            if parent is None:
                continue
            if tag_exists(workspace, tag):
                _resume_tagged_child(workspace, exp_id, genid, parent, round_number, operators_config)
                continue
            _run_child(workspace, exp_id, genid, parent, round_number, operators_config)
    _maybe_final_anchor(workspace, options.max_generations)


def _maybe_final_anchor(workspace: Path, generation: int) -> None:
    if generation <= 0 or evaluator_anchor(workspace).get("final") is not True:
        return
    candidates = ArchiveView(workspace).valid_parents()
    candidate = best_row(workspace) or max(
        candidates,
        key=lambda row: (generation_number(str(row.get("genid"))) or -1, str(row.get("genid"))),
        default=None,
    )
    if candidate is None:
        return
    if _has_complete_anchor(candidate):
        return
    genid = str(candidate["genid"])
    _evaluate_once(
        workspace,
        str(candidate["tag"]),
        genid,
        purpose="anchor",
        metadata={
            "parent": candidate.get("parent"),
            "mutated": candidate.get("mutated", []),
            "surface_violations": candidate.get("surface_violations", []),
            "note": "sealed anchor evaluation; excluded from mutate feedback",
            "kind": "anchor",
            "round": generation,
        },
        pending_gate_on_complete=False,
    )


def _run_operator_or_fail(
    *,
    name: str,
    checkout: Path,
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str,
    run_dir: Path,
    operators_config: dict[str, Any],
    round_number: int | None,
) -> bool:
    """Run one operator guarded; on a crash or malformed output, record the
    failure and return False (the caller discards this generation). Shared by the
    required-operator loop and the optional gates so the failure-handling
    scaffold lives in one place."""
    result = _run_operator_guarded(
        name=name,
        checkout=checkout,
        workspace=workspace,
        exp_id=exp_id,
        genid=genid,
        parent=parent,
        run_dir=run_dir,
        config_block=_operator_config_block(operators_config, name),
        timeout_s=operator_timeout(operators_config, name),
        round_number=round_number,
    )
    if result.returncode != 0:
        _append_operator_failed(workspace, exp_id, genid, parent, name, note=_operator_failure_note(result))
        return False
    error = _operator_output_error(name, run_dir)
    if error is not None:
        _append_operator_failed(workspace, exp_id, genid, parent, name, note=_operator_output_note(error))
        return False
    return True


@contextmanager
def _managed_child_worktree(workspace: Path, child: Path) -> Iterator[None]:
    if child.exists():
        raise RuntimeError(f"refusing to replace existing child worktree: {child}")
    try:
        yield
    finally:
        if child.exists():
            remove_worktree(workspace, child)


def _run_child(
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str,
    round_number: int | None,
    operators_config: dict[str, Any],
) -> None:
    child = _child_worktree_path(workspace, genid)
    recorded = False

    def record_terminal_attempt(candidate_checkout: Path | None = child) -> None:
        nonlocal recorded
        if recorded:
            return
        recorded = True
        _run_terminal_record(workspace, exp_id, genid, parent, operators_config, candidate_checkout, round_number)

    with _managed_child_worktree(workspace, child):
        try:
            parent_commit = fork_child(workspace, parent, child)
        except Exception as exc:
            _append_operator_failed(
                workspace,
                exp_id,
                genid,
                parent,
                "select",
                note=str(exc),
            )
            record_terminal_attempt(None)
            return

        stages = ["rollout"]
        if _operator_present(operators_config, "analyze"):
            stages.append("analyze")
        stages.append("mutate")
        for name in stages:
            if name == "mutate" and _operator_present(operators_config, "analyze"):
                write_feedback_bundle(workspace=workspace, run_dir=_run_dir(workspace, genid))
            if not _run_operator_or_fail(
                name=name,
                checkout=child,
                workspace=workspace,
                exp_id=exp_id,
                genid=genid,
                parent=parent,
                run_dir=_run_dir(workspace, genid),
                operators_config=operators_config,
                round_number=round_number,
            ):
                record_terminal_attempt()
                return

        mutated_paths = working_tree_changed_paths(child, parent_commit)
        if not mutated_paths:
            _append_candidate_rejected(
                workspace,
                exp_id,
                genid,
                parent,
                status="no_proposal",
                reason="no changes to commit",
                mutated=[],
            )
            record_terminal_attempt()
            return
        include, exclude = surface_patterns(workspace)
        violations = check_paths(mutated_paths, include, exclude)
        if violations:
            _append_candidate_rejected(
                workspace,
                exp_id,
                genid,
                parent,
                status="invalid_proposal",
                reason="changed paths outside mutable surface",
                mutated=mutated_paths,
                violations=violations,
            )
            record_terminal_attempt()
            return

        if _operator_present(operators_config, "validate"):
            if not _run_operator_or_fail(
                name="validate",
                checkout=child,
                workspace=workspace,
                exp_id=exp_id,
                genid=genid,
                parent=parent,
                run_dir=_run_dir(workspace, genid),
                operators_config=operators_config,
                round_number=round_number,
            ):
                record_terminal_attempt()
                return
            validation, _error = _load_validate_payload(_run_dir(workspace, genid))
            if validation is not None and not validation["accept"]:
                _append_candidate_rejected(
                    workspace,
                    exp_id,
                    genid,
                    parent,
                    status="rejected_validation",
                    reason=f"candidate validation rejected: {validation['reason']}",
                    mutated=mutated_paths,
                )
                record_terminal_attempt()
                return

        # Novelty gate (mechanism 5, DESIGN §7) — optional, off unless the recipe
        # configures `operators.novelty`. Runs on the uncommitted candidate diff;
        # a near-duplicate is discarded before it is ever committed or evaluated.
        if _operator_present(operators_config, "novelty"):
            run_dir = _run_dir(workspace, genid)
            if not _run_operator_or_fail(
                name="novelty",
                checkout=child,
                workspace=workspace,
                exp_id=exp_id,
                genid=genid,
                parent=parent,
                run_dir=run_dir,
                operators_config=operators_config,
                round_number=round_number,
            ):
                record_terminal_attempt()
                return
            nov_payload, _ = _load_novelty_payload(run_dir)
            if nov_payload is not None and not nov_payload["accept"]:
                _append_novelty_rejected(workspace, exp_id, genid, parent, nov_payload)
                record_terminal_attempt()
                return

        commit_child(workspace, child, parent, genid)

    row = rows_by_genid(workspace).get(genid, {})
    if row.get("status") in {"no_proposal", "invalid_proposal", "operator_failed"}:
        return

    evaluation = eval_child(workspace, genid, round_number=round_number)
    if evaluation is None or evaluation.outcome is not Outcome.BENCHMARK_COMPLETE:
        return
    _run_gate_and_record(workspace, exp_id, genid, parent, operators_config, round_number=round_number)

    # Reflect (mechanism 2, DESIGN §7) — optional, off unless the recipe configures
    # `operators.reflect`. Reads the archive after the record operator annotates it
    # and appends playbook insights. Best-effort: a reflect failure never unwinds
    # the recorded generation. Runs against the workspace (it needs the archive,
    # not the candidate worktree).
    if _operator_present(operators_config, "reflect"):
        _run_operator_guarded(
            name="reflect",
            checkout=workspace,
            workspace=workspace,
            exp_id=exp_id,
            genid=genid,
            parent=parent,
            run_dir=_run_dir(workspace, genid),
            config_block=_operator_config_block(operators_config, "reflect"),
            timeout_s=operator_timeout(operators_config, "reflect"),
            round_number=round_number,
        )
    _maybe_quarantine(workspace, genid)


def repair(workspace: Path) -> list[str]:
    """Detect + repair interrupted state: prune stale child worktrees a crash
    left behind, and report generations pending gate/record that `run` resumes.
    Returns the actions taken/observations (empty means nothing to do)."""
    workspace = workspace.resolve()
    with workspace_run_lock(workspace):
        return _repair_locked(workspace)


def doctor(workspace: Path) -> list[str]:
    """Backward-compatible alias for the pre-0.2 repair API."""
    return repair(workspace)


def _repair_locked(workspace: Path) -> list[str]:
    actions: list[str] = []
    worktrees = workspace / "runs" / "worktrees"
    if worktrees.exists():
        for path in sorted(p for p in worktrees.iterdir() if p.is_dir()):
            changed = dirty_paths(path)
            if changed:
                actions.append(f"preserved dirty worktree {path.name}: {', '.join(changed)}")
                continue
            remove_worktree(workspace, path)
            actions.append(f"removed stale worktree {path.name}")
    managed_root = worktrees.resolve()
    evaluation_root = (workspace / "runs" / "evaluation-worktrees").resolve()
    for path in _workspace_child_worktrees(workspace):
        if path.is_relative_to(managed_root):
            continue
        if path.is_relative_to(evaluation_root):
            changed = dirty_paths(path)
            if changed:
                actions.append(f"preserved dirty evaluation worktree {path}: {', '.join(changed)}")
                continue
            parent = path.parent
            remove_worktree(workspace, path)
            if parent != evaluation_root and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
            actions.append(f"removed stale evaluation worktree {path}")
            continue
        actions.append(f"preserved linked worktree outside runs/worktrees: {path}")
    git(workspace, "worktree", "prune", check=False)
    pending = sorted(_evaluation_pending_gate_record_genids(workspace))
    if pending:
        actions.append(f"pending gate/record (run will resume): {', '.join(pending)}")
    return actions


def _workspace_child_worktrees(workspace: Path) -> list[Path]:
    root = workspace.resolve()
    return sorted(path for path in worktree_paths(root) if path != root)


def _maybe_quarantine(workspace: Path, genid: str) -> None:
    """Audit (DESIGN observability): quarantine a suspicious score jump past the
    champion by more than EVOLVE_AUDIT_JUMP (off by default). A `pending` audit
    flags the generation for human review — a huge unexplained gain is often an
    exploit of the evaluator, not real progress."""
    margin = os.environ.get("EVOLVE_AUDIT_JUMP")
    if not margin:
        return
    view = ArchiveView(workspace)
    child = view.row(genid) or {}
    score = child.get("score")
    prior = [float(r["score"]) for r in view.valid_parents() if str(r.get("genid")) != genid]
    if isinstance(score, (int, float)) and prior and float(score) - max(prior) > float(margin):
        record_fields(workspace, genid, {"audit": "pending"})


def _resume_tagged_child(
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str,
    round_number: int | None,
    operators_config: dict[str, Any],
) -> None:
    row = rows_by_genid(workspace).get(genid, {})
    needs_gate_record = genid in _evaluation_pending_gate_record_genids(workspace)
    canonical = row.get("outcome") in CANONICAL_OUTCOMES
    if not canonical:
        eval_child(workspace, genid, round_number=round_number)
        row = rows_by_genid(workspace).get(genid, {})
        needs_gate_record = genid in _evaluation_pending_gate_record_genids(workspace)
    if needs_gate_record:
        _run_gate_and_record(workspace, exp_id, genid, parent, operators_config, round_number=round_number)


def _run_gate_and_record(
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str,
    operators_config: dict[str, Any],
    *,
    round_number: int | None = None,
) -> bool:
    with tempfile.TemporaryDirectory(prefix=f"evolve-gate-{genid}-") as tempdir:
        checkout = Path(tempdir) / "checkout"
        add_worktree(workspace, checkout, f"gen/{genid}")
        try:
            run_dir = _run_dir(workspace, genid)
            _write_gate_input(workspace, run_dir, genid, parent)
            gate_result = _run_operator_guarded(
                name="gate",
                checkout=checkout,
                workspace=workspace,
                exp_id=exp_id,
                genid=genid,
                parent=parent,
                run_dir=run_dir,
                config_block=_operator_config_block(operators_config, "gate"),
                timeout_s=operator_timeout(operators_config, "gate"),
                round_number=round_number,
            )
            if gate_result.returncode != 0:
                _append_operator_failed(
                    workspace,
                    exp_id,
                    genid,
                    parent,
                    "gate",
                    note=_operator_failure_note(gate_result),
                )
                _run_terminal_record(
                    workspace,
                    exp_id,
                    genid,
                    parent,
                    operators_config,
                    checkout,
                    round_number=round_number,
                    operator_ref=f"gen/{genid}",
                )
                return False
            gate_payload, gate_error = _load_gate_payload(run_dir)
            if gate_error is not None or gate_payload is None:
                _append_operator_failed(
                    workspace,
                    exp_id,
                    genid,
                    parent,
                    "gate",
                    note=_operator_output_note(gate_error),
                )
                _run_terminal_record(
                    workspace,
                    exp_id,
                    genid,
                    parent,
                    operators_config,
                    checkout,
                    round_number=round_number,
                    operator_ref=f"gen/{genid}",
                )
                return False

            append_event(workspace, exp_id, {"genid": genid, "pending_gate_record": False, **gate_payload})
            _run_terminal_record(
                workspace,
                exp_id,
                genid,
                parent,
                operators_config,
                checkout,
                round_number=round_number,
                operator_ref=f"gen/{genid}",
                allowed_fields=RECORD_ANNOTATION_FIELDS,
            )
            return True
        finally:
            remove_worktree(workspace, checkout)
            materialize_best_ever(workspace)


def _run_terminal_record(
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str | None,
    operators_config: dict[str, Any],
    candidate_checkout: Path | None,
    round_number: int | None = None,
    operator_ref: str | None = None,
    allowed_fields: frozenset[str] = frozenset(),
) -> None:
    if not _operator_present(operators_config, "record") or _record_attempted(workspace, genid):
        return
    ref = operator_ref or f"gen/{parent}"
    with tempfile.TemporaryDirectory(prefix=f"evolve-record-{genid}-") as tempdir:
        operator_checkout = Path(tempdir) / "operator"
        add_worktree(workspace, operator_checkout, ref)
        try:
            context_checkout = (
                candidate_checkout if candidate_checkout and candidate_checkout.exists() else operator_checkout
            )
            run_dir = _run_dir(workspace, genid)
            result = _run_operator_guarded(
                name="record",
                checkout=context_checkout,
                operator_checkout=operator_checkout,
                workspace=workspace,
                exp_id=exp_id,
                genid=genid,
                parent=parent,
                run_dir=run_dir,
                config_block=_operator_config_block(operators_config, "record"),
                timeout_s=operator_timeout(operators_config, "record"),
                round_number=round_number,
            )
            if result.returncode != 0:
                _append_record_error(workspace, exp_id, genid, _operator_failure_note(result))
                return
            fields, error = _load_record_fields(run_dir)
            if error is not None or fields is None:
                _append_record_error(workspace, exp_id, genid, _operator_output_note(error))
                return
            _record_terminal_fields(
                workspace,
                exp_id,
                genid,
                _strip_record_fields(fields, allowed_fields),
                allowed_fields=allowed_fields,
            )
        finally:
            remove_worktree(workspace, operator_checkout)


def fork_child(workspace: Path, parent: str, child_worktree: Path) -> str:
    workspace = workspace.resolve()
    parent_commit = _assert_valid_parent(workspace, parent)
    if child_worktree.exists():
        if any(child_worktree.iterdir()):
            raise RuntimeError(f"child worktree path is not empty: {child_worktree}")
        child_worktree.rmdir()
    # Use the commit certified by the evaluation row, not the mutable tag name.
    # The tag can move after parent validation; a detached worktree at this SHA
    # still starts from exactly the code whose score was certified.
    add_worktree(workspace, child_worktree, parent_commit)
    return parent_commit


def commit_child(workspace: Path, child_worktree: Path, parent: str, genid: str) -> None:
    workspace = workspace.resolve()
    genid = _validate_genid(genid)
    parent_commit = _assert_valid_parent(workspace, parent)
    if tag_exists(workspace, f"gen/{genid}") or genid in rows_by_genid(workspace):
        raise RuntimeError(f"generation already exists: {genid}")
    _assert_child_worktree_for_parent(workspace, child_worktree, parent, parent_commit)
    exp_id = experiment_id(workspace)
    ensure_local_archive(workspace, exp_id)
    tag = f"gen/{genid}"
    mutated = working_tree_changed_paths(child_worktree, parent_commit)
    if not mutated:
        append_event(
            workspace,
            exp_id,
            {
                "genid": genid,
                "parent": parent,
                "tag": tag,
                "score": None,
                "status": "no_proposal",
                "task_set_hash": None,
                "evaluator_tree": None,
                "valid_parent": False,
                "verdict": "discard",
                "reason": "no changes to commit",
                "mutated": [],
                "surface_violations": [],
                "note": "no proposal",
                "cost": {"usd": 0, "wall_s": 0},
            },
        )
        return

    include, exclude = surface_patterns(workspace)
    violations = check_paths(mutated, include, exclude)
    if violations:
        _append_candidate_rejected(
            workspace,
            exp_id,
            genid,
            parent,
            status="invalid_proposal",
            reason="changed paths outside mutable surface",
            mutated=mutated,
            violations=violations,
        )
        return

    snapshot = build_candidate_snapshot(
        child_worktree,
        parent_commit,
        include=include,
        exclude=exclude,
    )
    commit_candidate_snapshot(child_worktree, snapshot, f"evolve gen {genid}")
    create_tag(child_worktree, tag)
    append_event(
        workspace,
        exp_id,
        {
            "genid": genid,
            "parent": parent,
            "tag": tag,
            "mutated": mutated,
            "surface_violations": [],
        },
    )


def record_fields(workspace: Path, genid: str, fields: dict[str, object]) -> None:
    workspace = workspace.resolve()
    genid = _validate_genid(genid)
    forbidden = sorted(set(fields) & RECORD_FORBIDDEN_FIELDS)
    if forbidden:
        raise RuntimeError(f"record refuses protected fields: {', '.join(forbidden)}")
    exp_id = experiment_id(workspace)
    ensure_local_archive(workspace, exp_id)
    if genid not in rows_by_genid(workspace):
        raise RuntimeError(f"unknown generation: {genid}")
    append_event(workspace, exp_id, {"genid": genid, **fields})


def eval_child(
    workspace: Path,
    genid: str,
    *,
    round_number: int | None = None,
    kind: str = "eval",
    force: bool = False,
) -> EvaluationRecord | None:
    workspace = workspace.resolve()
    genid = _validate_genid(genid)
    exp_id = experiment_id(workspace)
    ensure_local_archive(workspace, exp_id)
    evaluator_sampling(workspace)
    if round_number is not None:
        raise RuntimeError("per-round evaluation sampling is not supported; use static sampling")
    rows = rows_by_genid(workspace)
    row = rows.get(genid, {})
    status = evaluation_status(row)
    if not force and round_number is None and row.get("outcome") in CANONICAL_OUTCOMES:
        return None
    if force:
        kind = "forced_eval"
    if status in UNRETRYABLE_STATUSES and not force:
        return None
    parent = str(row.get("parent") or (genid if genid == "0" else _fallback_parent_for_eval(workspace, rows)))
    tag = f"gen/{genid}"
    mutated = row.get("mutated")
    surface_violations = row.get("surface_violations")
    if isinstance(mutated, list) and isinstance(surface_violations, list):
        return _stamp_evaluation(
            workspace,
            genid,
            parent,
            tag,
            [str(path) for path in mutated],
            round_number=round_number,
            kind=kind,
        )
    return _finalize_child(workspace, exp_id, genid, parent, tag, round_number=round_number, kind=kind)


def _finalize_child(
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str,
    tag: str,
    *,
    round_number: int | None = None,
    kind: str = "eval",
) -> EvaluationRecord | None:
    parent_tag = f"gen/{parent}"
    mutated = git_stdout(workspace, "diff", "--name-only", parent_tag, tag).splitlines()
    include, exclude = surface_patterns(workspace)
    violations = check_paths(mutated, include, exclude)
    if violations:
        append_event(
            workspace,
            exp_id,
            {
                "genid": genid,
                "parent": parent,
                "tag": tag,
                "score": None,
                "status": "invalid_proposal",
                "task_set_hash": None,
                "evaluator_tree": None,
                "valid_parent": False,
                "verdict": "discard",
                "reason": "changed paths outside mutable surface",
                "mutated": mutated,
                "surface_violations": violations,
                "note": "commit rejected by surface",
                "cost": {"usd": 0, "wall_s": 0},
            },
        )
        return None
    return _stamp_evaluation(
        workspace,
        genid,
        parent,
        tag,
        mutated,
        round_number=round_number,
        kind=kind,
    )


def _stamp_evaluation(
    workspace: Path,
    genid: str,
    parent: str,
    tag: str,
    mutated: list[str],
    *,
    round_number: int | None = None,
    kind: str = "eval",
) -> EvaluationRecord:
    metadata = {
        "parent": parent,
        "mutated": mutated,
        "surface_violations": [],
    }
    if round_number is not None:
        metadata.update(kind=kind, round=round_number)
    return _evaluate_once(
        workspace,
        tag,
        genid,
        purpose="candidate",
        metadata=metadata,
        round_number=round_number,
        pending_gate_on_complete=genid != "0",
    )


def _ensure_genesis_evaluated(workspace: Path) -> None:
    row = rows_by_genid(workspace).get("0", {})
    status = evaluation_status(row)
    if row.get("outcome") == "benchmark_complete" and row.get("selection_eligible") is True:
        return
    if status in {
        Outcome.CANDIDATE_INVALID.value,
        Outcome.INFRASTRUCTURE_FAILED.value,
        Outcome.TIMEOUT.value,
        Outcome.CANCELLED.value,
    }:
        raise RuntimeError(f"genesis {status}: fix the seed or infrastructure and initialize a new workspace")
    result = _evaluate_once(
        workspace,
        "gen/0",
        "0",
        purpose="genesis",
        metadata={
            "parent": row.get("parent"),
            "mutated": row.get("mutated", []),
            "surface_violations": row.get("surface_violations", []),
            "note": "genesis evaluated",
            "pending_gate_record": False,
            "kind": "genesis_eval",
        },
    )
    if result.outcome is not Outcome.BENCHMARK_COMPLETE:
        raise RuntimeError(
            f"genesis {result.outcome.value}: fix the seed or infrastructure and initialize a new workspace"
        )


def _ensure_genesis_anchor_evaluated(workspace: Path) -> None:
    manifest = load_manifest(workspace / "evaluator" / "splits.json")
    if not selected_task_names(manifest, "sealed"):
        return
    row = rows_by_genid(workspace).get("0", {})
    if _has_complete_anchor(row):
        return
    result = _evaluate_once(
        workspace,
        "gen/0",
        "0",
        purpose="anchor",
        metadata={
            "parent": row.get("parent"),
            "mutated": row.get("mutated", []),
            "surface_violations": row.get("surface_violations", []),
            "note": "genesis sealed anchor evaluated; excluded from mutate feedback",
            "pending_gate_record": False,
            "kind": "anchor",
            "round": 0,
        },
    )
    if result.outcome is not Outcome.BENCHMARK_COMPLETE:
        raise RuntimeError(
            f"genesis sealed anchor {result.outcome.value}: fix the seed or infrastructure and resume the workspace"
        )


def _has_complete_anchor(row: dict[str, Any]) -> bool:
    return any(
        isinstance(entry, dict)
        and entry.get("kind") == "anchor"
        and entry.get("purpose") == "anchor"
        and entry.get("outcome") == Outcome.BENCHMARK_COMPLETE.value
        for entry in row.get("evals", []) or []
    )


def _evaluate_once(
    workspace: Path,
    tag: str,
    genid: str,
    *,
    purpose: str,
    metadata: dict[str, Any],
    round_number: int | None = None,
    pending_gate_on_complete: bool = False,
) -> EvaluationRecord:
    """Run one evaluation attempt and preserve its evidence.

    A later explicit resume is the operator-controlled retry boundary.
    """

    def run_attempt(**kwargs: Any) -> EvaluationRecord:
        try:
            record = evaluate(workspace, tag, genid, purpose=purpose, **kwargs)
        except EvaluationInterrupted as interrupted:
            record, cause = interrupted.args
            _append_lifecycle_evaluation(workspace, record, metadata, pending_gate_on_complete)
            raise cause
        _append_lifecycle_evaluation(workspace, record, metadata, pending_gate_on_complete)
        return record

    return run_attempt()


def _append_lifecycle_evaluation(
    workspace: Path,
    record: EvaluationRecord,
    metadata: dict[str, Any],
    pending_gate_on_complete: bool,
) -> None:
    complete = record.outcome is Outcome.BENCHMARK_COMPLETE
    gate_metadata = (
        {
            "pending_gate_record": complete,
            "note": PENDING_GATE_RECORD_NOTE if complete else f"candidate evaluation {record.status}",
        }
        if pending_gate_on_complete
        else {}
    )
    append_evaluation_record(workspace, record, metadata={**metadata, **gate_metadata})
    materialize_best_ever(workspace)


def _select_generation_parents(
    workspace: Path,
    exp_id: str,
    generation: int,
    pending_genids: list[str],
    children_per_gen: int,
    operators_config: dict[str, Any],
) -> dict[str, str]:
    best = best_row(workspace)
    select_tag = str(best.get("tag")) if best else "gen/0"
    run_dir = workspace / "runs" / f"gen-{generation}" / "select"
    with tempfile.TemporaryDirectory(prefix=f"evolve-select-{generation}-") as tempdir:
        checkout = Path(tempdir) / "checkout"
        add_worktree(workspace, checkout, select_tag)
        try:
            result = _run_operator_guarded(
                name="select",
                checkout=checkout,
                workspace=workspace,
                exp_id=exp_id,
                genid=str(generation),
                parent=None,
                run_dir=run_dir,
                config_block=_operator_config_block(operators_config, "select"),
                timeout_s=operator_timeout(operators_config, "select"),
                round_number=None,
            )
        finally:
            remove_worktree(workspace, checkout)

    if result.returncode != 0:
        for genid in pending_genids:
            _append_operator_failed(
                workspace,
                exp_id,
                genid,
                None,
                "select",
                note=_operator_failure_note(result),
            )
            _run_terminal_record(
                workspace,
                exp_id,
                genid,
                None,
                operators_config,
                candidate_checkout=None,
                round_number=None,
                operator_ref=select_tag,
            )
        return {}

    parents, parents_error = _load_parents(run_dir)
    if parents_error is not None or not parents:
        for genid in pending_genids:
            _append_operator_failed(
                workspace,
                exp_id,
                genid,
                None,
                "select",
                note=_operator_output_note(parents_error),
            )
            _run_terminal_record(
                workspace,
                exp_id,
                genid,
                None,
                operators_config,
                candidate_checkout=None,
                round_number=None,
                operator_ref=select_tag,
            )
        return {}

    selected: dict[str, str] = {}
    for child_index, genid in enumerate(
        format_genid(generation, index, children_per_gen) for index in range(children_per_gen)
    ):
        if genid not in pending_genids:
            continue
        if len(parents) == 1:
            selected[genid] = parents[0]
            continue
        if child_index < len(parents):
            selected[genid] = parents[child_index]
            continue
        _append_operator_failed(
            workspace,
            exp_id,
            genid,
            None,
            "select",
            note="parents.json missing child slot",
        )
        _run_terminal_record(
            workspace,
            exp_id,
            genid,
            None,
            operators_config,
            candidate_checkout=None,
            round_number=None,
            operator_ref=select_tag,
        )
    return selected


def _load_parents(run_dir: Path) -> tuple[list[str] | None, OperatorOutputError | None]:
    payload, error = _load_validated_json(
        run_dir,
        Path("parents.json"),
        "parents",
        validate_select_payload,
    )
    if error is not None or payload is None:
        return None, error
    return list(payload["parents"]), None


def _evaluation_pending_gate_record_genids(workspace: Path) -> set[str]:
    pending: set[str] = set()
    for event in read_events(archive_path(workspace)):
        genid = str(event.get("genid", ""))
        if _event_is_evaluation_stamp(event):
            pending.add(genid)
            continue
        if genid in pending and _event_is_gate_record_event(event):
            pending.discard(genid)
    return pending


def _event_is_evaluation_stamp(event: dict[str, Any]) -> bool:
    return event.get("outcome") in CANONICAL_OUTCOMES and _event_marks_pending_gate_record(event)


def _event_marks_pending_gate_record(event: dict[str, Any]) -> bool:
    return event.get("pending_gate_record") is True or event.get("note") == PENDING_GATE_RECORD_NOTE


def _event_is_gate_record_event(event: dict[str, Any]) -> bool:
    if STAMPED_FIELDS.isdisjoint(event) and "record_error" in event:
        return True
    return STAMPED_FIELDS.isdisjoint(event) and {"valid_parent", "verdict", "reason"} <= set(event)


def _generation_is_pending(row: dict[str, Any], needs_gate_record: bool) -> bool:
    status = evaluation_status(row)
    return status not in TERMINAL_STATUSES or needs_gate_record


def _load_gate_payload(run_dir: Path) -> tuple[dict[str, Any] | None, OperatorOutputError | None]:
    return _load_validated_json(run_dir, Path("gate.json"), "valid_parent", validate_gate_file_payload)


def _load_novelty_payload(run_dir: Path) -> tuple[dict[str, Any] | None, OperatorOutputError | None]:
    return _load_validated_json(run_dir, Path("novelty.json"), "accept", validate_novelty_file_payload)


def _load_validate_payload(run_dir: Path) -> tuple[dict[str, Any] | None, OperatorOutputError | None]:
    return _load_validated_json(
        run_dir,
        Path("validate") / "result.json",
        "accept",
        validate_validate_file_payload,
    )


def _load_record_fields(run_dir: Path) -> tuple[dict[str, Any] | None, OperatorOutputError | None]:
    return _load_validated_json(run_dir, Path("record") / "fields.json", "fields", validate_record_fields_payload)


def _write_gate_input(workspace: Path, run_dir: Path, genid: str, parent: str) -> None:
    rows = rows_by_genid(workspace)
    child = dict(rows.get(genid) or {"genid": genid, "parent": parent})
    parent_row = _matched_gate_parent(rows.get(parent), child)
    _write_json(run_dir / "gate" / "input.json", {"child": child, "parent": parent_row})


def _matched_gate_parent(parent: dict[str, Any] | None, child: dict[str, Any]) -> dict[str, Any] | None:
    task_hash = child.get("task_set_hash")
    if parent is None or task_hash is None:
        return None
    source = parent if parent.get("task_set_hash") == task_hash else None
    if source is None:
        evals = parent.get("evals", []) or []
        source = next(
            (entry for entry in evals if isinstance(entry, dict) and entry.get("task_set_hash") == task_hash), None
        )
    if source is None or source.get("score") is None:
        return None
    matched = dict(parent)
    matched["score"] = source["score"]
    if source is not parent:
        matched["_matched_from_evals"] = True
    return matched


def _operator_output_error(name: str, run_dir: Path) -> OperatorOutputError | None:
    checks: tuple[tuple[Path, str, Callable[[Any], object]], ...]
    if name == "select":
        checks = ((Path("parents.json"), "parents", validate_select_payload),)
    elif name == "rollout":
        checks = (
            (Path("rollout") / "summary.json", "summary", validate_rollout_summary_payload),
            (Path("rollout") / "artifacts.json", "artifacts", validate_rollout_artifacts_payload),
        )
    elif name == "analyze":
        checks = (
            (Path("analyze") / "summary.json", "summary", validate_rollout_summary_payload),
            (Path("analyze") / "artifacts.json", "artifacts", validate_rollout_artifacts_payload),
        )
    elif name == "mutate":
        checks = ((Path("mutate") / "usage.json", "usage", validate_mutate_usage_payload),)
    elif name == "validate":
        checks = ((Path("validate") / "result.json", "accept", validate_validate_file_payload),)
    elif name == "novelty":
        checks = ((Path("novelty.json"), "accept", validate_novelty_file_payload),)
    else:
        return None
    for relative_path, field, validator in checks:
        _payload, error = _load_validated_json(run_dir, relative_path, field, validator)
        if error is not None:
            return error
    return None


def _load_validated_json(
    run_dir: Path,
    relative_path: Path,
    field: str,
    validator: Callable[[Any], Any],
) -> tuple[Any | None, OperatorOutputError | None]:
    path = run_dir / relative_path
    if not path.exists():
        return None, OperatorOutputError(relative_path, field, "missing file")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return None, OperatorOutputError(relative_path, field, f"invalid json: {exc.msg}")
    try:
        return validator(payload), None
    except PayloadValidationError as exc:
        return None, OperatorOutputError(relative_path, exc.field, str(exc))
    except ValueError as exc:
        return None, OperatorOutputError(relative_path, field, str(exc))


def _append_operator_failed(
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str | None,
    operator_name: str,
    note: str,
) -> None:
    append_event(
        workspace,
        exp_id,
        {
            "genid": genid,
            "parent": parent,
            "tag": f"gen/{genid}",
            "score": None,
            "status": "operator_failed",
            "task_set_hash": None,
            "evaluator_tree": None,
            "valid_parent": False,
            "verdict": "discard",
            "reason": f"operator {operator_name} failed",
            "failure_stage": operator_name,
            "mutated": [],
            "surface_violations": [],
            "note": note,
            "cost": {"usd": 0, "wall_s": 0},
        },
    )


def _append_candidate_rejected(
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str,
    *,
    status: str,
    reason: str,
    mutated: list[str],
    violations: list[str] | None = None,
) -> None:
    append_event(
        workspace,
        exp_id,
        {
            "genid": genid,
            "parent": parent,
            "tag": f"gen/{genid}",
            "score": None,
            "status": status,
            "task_set_hash": None,
            "evaluator_tree": None,
            "valid_parent": False,
            "verdict": "discard",
            "reason": reason,
            "mutated": mutated,
            "surface_violations": list(violations or []),
            "note": reason,
            "cost": {"usd": 0, "wall_s": 0},
        },
    )


def _append_novelty_rejected(
    workspace: Path, exp_id: str, genid: str, parent: str | None, payload: dict[str, Any]
) -> None:
    """Record a candidate edit rejected by the novelty gate — discarded, not evaluated,
    and never committed (mechanism 5, DESIGN §7)."""
    append_event(
        workspace,
        exp_id,
        {
            "genid": genid,
            "parent": parent,
            "tag": f"gen/{genid}",
            "score": None,
            "status": "rejected_duplicate",
            "task_set_hash": None,
            "evaluator_tree": None,
            "valid_parent": False,
            "verdict": "discard",
            "reason": "novelty gate rejected a near-duplicate candidate edit",
            "mutated": [],
            "surface_violations": [],
            "novelty": payload.get("novelty"),
            "note": f"novelty={payload.get('novelty')}",
            "cost": {"usd": 0, "wall_s": 0},
        },
    )


def _append_record_error(workspace: Path, exp_id: str, genid: str, note: str) -> None:
    append_event(workspace, exp_id, {"genid": genid, "record_error": note})


def _record_terminal_fields(
    workspace: Path,
    exp_id: str,
    genid: str,
    fields: dict[str, object],
    *,
    allowed_fields: frozenset[str] = frozenset(),
) -> None:
    workspace = workspace.resolve()
    genid = _validate_genid(genid)
    forbidden = sorted(set(fields) & (RECORD_FORBIDDEN_FIELDS - allowed_fields))
    if forbidden:
        raise RuntimeError(f"record refuses protected fields: {', '.join(forbidden)}")
    if genid not in rows_by_genid(workspace):
        raise RuntimeError(f"unknown generation: {genid}")
    append_event(workspace, exp_id, {"genid": genid, **fields, RECORD_ATTEMPT_FIELD: True})


def _operator_output_note(error: OperatorOutputError | None) -> str:
    if error is None:
        return "operator output missing or malformed"
    return f"{error.path.as_posix()} field {error.field}: {error.detail}"


def _strip_record_fields(fields: dict[str, Any], allowed_fields: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if key not in RECORD_FORBIDDEN_FIELDS - allowed_fields}


def _operator_config_block(operators_config: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(operator_runtime_config(operators_config, name))


def _operator_present(operators_config: dict[str, Any], name: str) -> bool:
    """An optional operator is enabled when its key is present — even as `{}`
    (an empty config block is still an opt-in, not `off`)."""
    return isinstance(operators_config.get(name), dict)


def _record_attempted(workspace: Path, genid: str) -> bool:
    return any(
        str(event.get("genid")) == genid and (event.get(RECORD_ATTEMPT_FIELD) is True or "record_error" in event)
        for event in read_events(archive_path(workspace))
    )


def _run_operator_guarded(
    *,
    name: str,
    checkout: Path,
    operator_checkout: Path | None = None,
    workspace: Path,
    exp_id: str,
    genid: str,
    parent: str | None,
    run_dir: Path,
    config_block: dict[str, Any],
    timeout_s: float,
    round_number: int | None = None,
) -> OperatorResult:
    before = _archive_line_snapshots(workspace, exp_id)
    try:
        if checkout.resolve() != workspace.resolve():
            checkout_archive = archive_path(checkout)
            checkout_receipts = eval_receipt_path(checkout_archive)
            checkout_best = checkout / "best_ever.json"
            before[checkout_archive] = _archive_lines(checkout_archive)
            before[checkout_receipts] = _archive_lines(checkout_receipts)
            before[checkout_best] = _archive_lines(checkout_best)
            _write_archive_lines(checkout_archive, before[archive_path(workspace)])
            _write_archive_lines(
                checkout_receipts,
                before[eval_receipt_path(archive_path(workspace))],
            )
            _write_archive_lines(checkout_best, before[workspace / "best_ever.json"])
        return run_operator(
            name=name,
            checkout=checkout,
            workspace=workspace,
            genid=genid,
            parent=parent,
            run_dir=run_dir,
            config_block=config_block,
            timeout_s=timeout_s,
            round_number=round_number,
            operator_checkout=operator_checkout,
        )
    finally:
        _restore_operator_archive_writes(before)


def _archive_line_snapshots(workspace: Path, exp_id: str) -> dict[Path, list[str]]:
    archives = (archive_path(workspace), mirror_path(exp_id, workspace))
    paths = (*archives, *(eval_receipt_path(path) for path in archives), workspace / "best_ever.json")
    return {path: _archive_lines(path) for path in paths}


def _archive_lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _restore_operator_archive_writes(before: dict[Path, list[str]]) -> None:
    for path, original in before.items():
        _write_archive_lines(path, original)


def _write_archive_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _run_dir(workspace: Path, genid: str) -> Path:
    return workspace / "runs" / f"gen-{genid}"


def _operator_failure_note(result: OperatorResult) -> str:
    return (
        _tail_text(result.stderr) or _tail_text(result.stdout) or f"operator {result.name} exited {result.returncode}"
    )


def _tail_text(text: str, limit: int = 240) -> str:
    stripped = text.strip()
    return "" if not stripped else stripped if len(stripped) <= limit else stripped[-limit:]


def _child_worktree_path(workspace: Path, genid: str) -> Path:
    return workspace / "runs" / "worktrees" / f"gen-{genid}"


def _fallback_parent_for_eval(
    workspace: Path,
    rows: dict[str, dict[str, object]],
    max_generation: int | None = None,
) -> str:
    if fixed_evaluation_identity(workspace) is None:
        raise RuntimeError(
            "fixed evaluation identity is unavailable; legacy v1 local split manifests do not bind task "
            "contents, so start a new experiment with a v2 split manifest"
        )
    filtered_rows = []
    for genid, row in rows.items():
        generation = generation_number(genid)
        if generation is None:
            continue
        if max_generation is not None and generation > max_generation:
            continue
        filtered_rows.append(row)
    best = best_row(workspace, filtered_rows)
    if best is None:
        raise RuntimeError("no valid parent available")
    return str(best["genid"])


def _validate_genid(genid: str) -> str:
    if not valid_genid(genid):
        raise RuntimeError(f"generation id must be numeric: {genid}")
    return genid


def _assert_valid_parent(workspace: Path, parent: str) -> str:
    parent = _validate_genid(parent)
    view = ArchiveView(workspace)
    row = view.row(parent)
    if row is None:
        raise RuntimeError(f"unknown parent: {parent}")
    certified = next(
        (candidate for candidate in view.valid_parents() if str(candidate.get("genid")) == parent),
        None,
    )
    if certified is None:
        raise RuntimeError(f"parent gen/{parent} is not a valid parent")
    if not tag_exists(workspace, f"gen/{parent}"):
        raise RuntimeError(f"missing tag for parent gen/{parent}")
    candidate_commit = certified.get("candidate_commit")
    if not isinstance(candidate_commit, str) or not candidate_commit:
        raise RuntimeError(f"parent gen/{parent} has no certified candidate commit")
    return candidate_commit


def _assert_child_worktree_for_parent(
    workspace: Path,
    child_worktree: Path,
    parent: str,
    parent_commit: str,
) -> None:
    if git_common_dir(workspace) != git_common_dir(child_worktree):
        raise RuntimeError("child worktree does not belong to the workspace repository")
    if head_commit(child_worktree) != parent_commit:
        raise RuntimeError(f"child worktree is not based on gen/{parent}")
