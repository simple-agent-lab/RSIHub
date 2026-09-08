"""Safe, composable mechanism verbs for an outer orchestration agent."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import ensure_local_archive, rows_by_genid
from .candidate.snapshot import build_candidate_snapshot
from .config import experiment_id, operator_blocks
from .doctor import ensure_evaluator_ready
from .driver import (
    _assert_child_worktree_for_parent,
    _assert_valid_parent,
    _ensure_genesis_evaluated,
    _evaluate_once,
    _evaluation_pending_gate_record_genids,
    _has_complete_anchor,
    _load_novelty_payload,
    _load_validate_payload,
    _operator_config_block,
    _operator_failure_note,
    _operator_output_error,
    _operator_output_note,
    _operator_present,
    _run_gate_and_record,
    _run_operator_guarded,
    _validate_genid,
    _write_json,
    commit_child,
    eval_child,
    fork_child,
    record_fields,
    workspace_run_lock,
)
from .evaluation import CANONICAL_OUTCOMES, EvaluationRecord
from .feedback import write_feedback_bundle
from .frozen.interfaces import OPERATOR_BY_KIND
from .git import add_worktree, git_common_dir, remove_worktree
from .operators import OperatorResult, operator_timeout
from .population import best_row
from .surface import surface_patterns


@dataclass(frozen=True)
class OperatorInvocation:
    result: OperatorResult
    run_dir: Path
    config: dict[str, Any]


def invoke_operator(
    workspace: Path,
    name: str,
    genid: str,
    *,
    parent: str | None = None,
    checkout: Path | None = None,
    config_override: dict[str, Any] | None = None,
    timeout_s: float | None = None,
    round_number: int | None = None,
    operator_ref: str | None = None,
    _agent_session: bool = False,
) -> OperatorInvocation:
    """Run one configured operator without handing it mechanism-owned state."""

    workspace = workspace.resolve()
    _assert_agent_session_route(workspace, _agent_session)
    genid = _validate_genid(genid)
    _assert_invocation_route(name, parent)
    configured = operator_blocks(workspace)
    if not _operator_present(configured, name):
        raise RuntimeError(f"operator is not configured: {name}")
    reserved_overrides = sorted({"operator", "script", "timeout_s", "config"} & set(config_override or {}))
    if reserved_overrides:
        raise RuntimeError("operator invocation cannot replace implementation keys: " + ", ".join(reserved_overrides))
    config = _merge_config(_operator_config_block(configured, name), config_override or {})
    effective_timeout = _invocation_timeout(configured, name, timeout_s)
    run_dir = workspace / "runs" / f"gen-{genid}"
    exp_id = experiment_id(workspace)
    ensure_local_archive(workspace, exp_id)
    with workspace_run_lock(workspace):
        if parent is not None:
            _assert_valid_parent(workspace, parent)
        with _invocation_checkout(workspace, name, checkout, parent) as selected_checkout:
            with _operator_source_checkout(workspace, operator_ref) as operator_checkout:
                return _invoke_selected_operator(
                    workspace=workspace,
                    name=name,
                    genid=genid,
                    parent=parent,
                    selected_checkout=selected_checkout,
                    operator_checkout=operator_checkout,
                    configured=configured,
                    config=config,
                    effective_timeout=effective_timeout,
                    run_dir=run_dir,
                    exp_id=exp_id,
                    round_number=round_number,
                )


def _invoke_selected_operator(
    *,
    workspace: Path,
    name: str,
    genid: str,
    parent: str | None,
    selected_checkout: Path,
    operator_checkout: Path | None,
    configured: dict[str, Any],
    config: dict[str, Any],
    effective_timeout: float,
    run_dir: Path,
    exp_id: str,
    round_number: int | None,
) -> OperatorInvocation:
    _assert_operator_prerequisites(name, run_dir, configured)
    _archive_active_outputs(name, run_dir)
    result = _run_operator_guarded(
        name=name,
        checkout=selected_checkout,
        workspace=workspace,
        exp_id=exp_id,
        genid=genid,
        parent=parent,
        run_dir=run_dir,
        config_block=config,
        timeout_s=effective_timeout,
        round_number=round_number,
        operator_checkout=operator_checkout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"operator {name} failed with exit {result.returncode}: {_operator_failure_note(result)}")
    output_error = _operator_output_error(name, run_dir)
    if output_error is not None:
        raise RuntimeError(f"operator {name} produced invalid output: {_operator_output_note(output_error)}")
    if name in {"validate", "novelty"}:
        _write_candidate_receipt(workspace, selected_checkout, run_dir, name, str(parent))
    if name == "analyze":
        write_feedback_bundle(workspace=workspace, run_dir=run_dir)
    return OperatorInvocation(result=result, run_dir=run_dir, config=config)


@contextmanager
def _operator_source_checkout(workspace: Path, operator_ref: str | None) -> Iterator[Path | None]:
    if operator_ref is None:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="evolve-agent-operator-") as tempdir:
        checkout = Path(tempdir) / "checkout"
        add_worktree(workspace, checkout, operator_ref)
        try:
            yield checkout
        finally:
            remove_worktree(workspace, checkout)


def _assert_invocation_route(name: str, parent: str | None) -> None:
    if name not in OPERATOR_BY_KIND:
        raise RuntimeError(f"unknown operator: {name} (choose from {', '.join(OPERATOR_BY_KIND)})")
    if name in {"gate", "record", "reflect"}:
        route = "evolve finalize" if name in {"gate", "record"} else "the built-in driver"
        raise RuntimeError(f"operator {name} is mechanism-owned; use {route}")
    if name == "select" and parent is not None:
        raise RuntimeError("select chooses the parent; do not pass --parent")
    if name != "select" and parent is None:
        raise RuntimeError(f"operator {name} requires --parent")


def _invocation_timeout(
    configured: dict[str, Any],
    name: str,
    explicit: float | None,
) -> float:
    candidate = explicit if explicit is not None else operator_timeout(configured, name)
    if not isinstance(candidate, (int, float)) or isinstance(candidate, bool) or candidate <= 0:
        raise RuntimeError("operator timeout must be a positive number")
    return float(candidate)


def _assert_operator_prerequisites(name: str, run_dir: Path, configured: dict[str, Any]) -> None:
    prerequisites = ["rollout"] if name in {"analyze", "mutate"} else []
    if name == "mutate" and _operator_present(configured, "analyze"):
        prerequisites.append("analyze")
    for prerequisite in prerequisites:
        error = _operator_output_error(prerequisite, run_dir)
        if error is not None:
            raise RuntimeError(f"operator {name} requires valid {prerequisite} output: {_operator_output_note(error)}")


@contextmanager
def _invocation_checkout(
    workspace: Path,
    name: str,
    checkout: Path | None,
    parent: str | None,
) -> Iterator[Path]:
    if name == "select" and checkout is None:
        champion = best_row(workspace)
        source_ref = str(champion.get("tag")) if champion else "gen/0"
        with tempfile.TemporaryDirectory(prefix="evolve-operator-select-") as tempdir:
            selected = Path(tempdir) / "checkout"
            add_worktree(workspace, selected, source_ref)
            try:
                yield selected
            finally:
                remove_worktree(workspace, selected)
        return
    selected = (checkout or workspace).resolve()
    if not selected.is_dir():
        raise RuntimeError(f"operator checkout does not exist: {selected}")
    if parent is not None:
        parent_commit = _assert_valid_parent(workspace, parent)
        _assert_child_worktree_for_parent(workspace, selected, parent, parent_commit)
    elif git_common_dir(workspace) != git_common_dir(selected):
        raise RuntimeError("operator checkout does not belong to the workspace repository")
    yield selected


_STAGE_OUTPUTS: dict[str, tuple[Path, ...]] = {
    "select": (Path("parents.json"),),
    "rollout": (Path("rollout"),),
    "analyze": (Path("analyze"), Path("feedback")),
    "mutate": (Path("mutate"),),
    "validate": (Path("validate"),),
    "novelty": (Path("novelty.json"), Path("novelty")),
}
_STAGE_ORDER = ("select", "rollout", "analyze", "mutate", "validate", "novelty")


def _archive_active_outputs(name: str, run_dir: Path) -> None:
    """Archive the rerun stage's outputs and every downstream stage's outputs.

    Stale downstream artifacts would otherwise stay active and satisfy
    prerequisite checks with evidence derived from the archived attempt."""
    if name in _STAGE_ORDER:
        stale_stages = _STAGE_ORDER[_STAGE_ORDER.index(name) :]
    else:
        stale_stages = (name,)
    for stage in stale_stages:
        existing = [relative for relative in _STAGE_OUTPUTS.get(stage, ()) if (run_dir / relative).exists()]
        if not existing:
            continue
        attempts = run_dir / "operator-attempts" / stage
        attempt_number = 1
        while (attempts / f"attempt-{attempt_number}").exists():
            attempt_number += 1
        destination = attempts / f"attempt-{attempt_number}"
        for relative in existing:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            (run_dir / relative).replace(target)


def _write_candidate_receipt(
    workspace: Path,
    checkout: Path,
    run_dir: Path,
    name: str,
    parent: str,
) -> None:
    include, exclude = surface_patterns(workspace)
    snapshot = build_candidate_snapshot(checkout, f"gen/{parent}", include=include, exclude=exclude)
    _write_json(
        run_dir / name / "candidate.json",
        {"parent": parent, "tree": snapshot.tree, "changed_paths": list(snapshot.changed_paths)},
    )


def finalize_child(
    workspace: Path,
    genid: str,
    *,
    parent: str | None = None,
    operator_ref: str | None = None,
    _agent_session: bool = False,
) -> bool:
    workspace = workspace.resolve()
    _assert_agent_session_route(workspace, _agent_session)
    genid = _validate_genid(genid)
    with workspace_run_lock(workspace):
        exp_id = experiment_id(workspace)
        ensure_local_archive(workspace, exp_id)
        row = rows_by_genid(workspace).get(genid)
        if row is None:
            raise RuntimeError(f"unknown generation: {genid}")
        resolved_parent = str(parent or row.get("parent") or "")
        if not resolved_parent:
            raise RuntimeError(f"generation {genid} has no parent")
        if parent is not None and str(row.get("parent")) != parent:
            raise RuntimeError(f"generation {genid} belongs to parent {row.get('parent')}, not {parent}")
        if row.get("outcome") not in CANONICAL_OUTCOMES:
            raise RuntimeError(f"generation {genid} must be evaluated before finalize")
        if genid not in _evaluation_pending_gate_record_genids(workspace):
            return False
        if not _run_gate_and_record(
            workspace,
            exp_id,
            genid,
            resolved_parent,
            operator_blocks(workspace),
            operator_ref=operator_ref,
        ):
            raise RuntimeError(f"generation {genid} gate failed; fix the operator or runtime, then retry finalize")
        return True


def fork_agent_child(workspace: Path, parent: str, child_worktree: Path, *, _agent_session: bool = False) -> None:
    workspace = workspace.resolve()
    _assert_agent_session_route(workspace, _agent_session)
    with workspace_run_lock(workspace):
        fork_child(workspace, parent, child_worktree)


def commit_agent_child(
    workspace: Path,
    child_worktree: Path,
    parent: str,
    genid: str,
    *,
    _agent_session: bool = False,
) -> None:
    workspace = workspace.resolve()
    _assert_agent_session_route(workspace, _agent_session)
    with workspace_run_lock(workspace):
        parent_commit = _assert_valid_parent(workspace, parent)
        _assert_child_worktree_for_parent(workspace, child_worktree, parent, parent_commit)
        configured = operator_blocks(workspace)
        run_dir = workspace / "runs" / f"gen-{_validate_genid(genid)}"
        include, exclude = surface_patterns(workspace)
        candidate = build_candidate_snapshot(child_worktree, parent_commit, include=include, exclude=exclude)
        _assert_admission(configured, run_dir, candidate.tree)
        commit_child(workspace, child_worktree, parent, genid)
        remove_worktree(workspace, child_worktree)


def _assert_admission(configured: dict[str, Any], run_dir: Path, candidate_tree: str) -> None:
    if _operator_present(configured, "validate"):
        validation, error = _load_validate_payload(run_dir)
        if error is not None or validation is None:
            raise RuntimeError("configured validate must run before commit: " + _operator_output_note(error))
        if not validation["accept"]:
            raise RuntimeError(f"candidate validation rejected: {validation['reason']}")
        _assert_candidate_receipt(run_dir / "validate/candidate.json", candidate_tree, "validate")
    if _operator_present(configured, "novelty"):
        novelty, error = _load_novelty_payload(run_dir)
        if error is not None or novelty is None:
            raise RuntimeError("configured novelty must run before commit: " + _operator_output_note(error))
        if not novelty["accept"]:
            raise RuntimeError(f"candidate novelty rejected: {novelty['novelty']}")
        _assert_candidate_receipt(run_dir / "novelty/candidate.json", candidate_tree, "novelty")


def _assert_candidate_receipt(path: Path, expected_tree: str, name: str) -> None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"configured {name} has no valid candidate receipt; rerun it") from exc
    if not isinstance(payload, dict) or payload.get("tree") != expected_tree:
        raise RuntimeError(f"candidate changed after {name}; rerun {name} before commit")


def eval_agent_child(
    workspace: Path, genid: str, *, force: bool = False, _agent_session: bool = False
) -> EvaluationRecord | None:
    workspace = workspace.resolve()
    _assert_agent_session_route(workspace, _agent_session)
    with workspace_run_lock(workspace):
        ensure_evaluator_ready(workspace)
        return eval_child(workspace, genid, force=force)


def seal_agent_champion(workspace: Path, genid: str) -> EvaluationRecord | None:
    """Evaluate one submitted valid parent on sealed data outside the action loop."""

    workspace = workspace.resolve()
    genid = _validate_genid(genid)
    with workspace_run_lock(workspace):
        _assert_valid_parent(workspace, genid)
        row = rows_by_genid(workspace)[genid]
        if _has_complete_anchor(row):
            return None
        return _evaluate_once(
            workspace,
            f"gen/{genid}",
            genid,
            purpose="anchor",
            metadata={
                "parent": row.get("parent"),
                "mutated": row.get("mutated", []),
                "surface_violations": row.get("surface_violations", []),
                "note": "sealed evaluation after Agent Driven champion submission",
                "kind": "anchor",
            },
            pending_gate_on_complete=False,
        )


def record_agent_fields(
    workspace: Path, genid: str, fields: dict[str, object], *, _agent_session: bool = False
) -> None:
    workspace = workspace.resolve()
    _assert_agent_session_route(workspace, _agent_session)
    with workspace_run_lock(workspace):
        record_fields(workspace, genid, fields)


def _assert_agent_session_route(workspace: Path, allowed: bool) -> None:
    if not allowed and (workspace / "runs/agent-driven/ACTIVE").is_file():
        raise RuntimeError("Agent Driven session owns this workspace; use `evolve agent act`")


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        merged[key] = _merge_config(current, value) if isinstance(current, dict) and isinstance(value, dict) else value
    return merged


def prepare_agent_baseline(workspace: Path) -> None:
    """Certify the development baseline without opening sealed data."""
    workspace = workspace.resolve()
    _assert_agent_session_route(workspace, False)
    from .agent_driver import _sealed_evidence_exists

    with workspace_run_lock(workspace):
        if _sealed_evidence_exists(workspace):
            raise RuntimeError("research preparation requires a fresh workspace without sealed evaluation")
        ensure_evaluator_ready(workspace)
        _ensure_genesis_evaluated(workspace)
