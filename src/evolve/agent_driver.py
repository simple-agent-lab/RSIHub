"""Durable, typed control sessions for an outer evolution agent."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import agent_research
from .agent_optimizer import freeze_optimizer
from .archive import archive_path, rows_by_genid
from .candidate.snapshot import build_candidate_snapshot
from .git import git_common_dir, git_stdout, working_tree_changed_paths, worktree_paths
from .orchestration import (
    commit_agent_child,
    eval_agent_child,
    finalize_child,
    fork_agent_child,
    invoke_operator,
    seal_agent_champion,
)
from .population import best_row, valid_genid, valid_parent_rows
from .preflight import PreflightStatus, run_preflight
from .splits import load_manifest, selected_task_names
from .surface import check_paths, surface_patterns

SCHEMA_VERSION = 1
SESSION_DIR = Path("runs/agent-driven")
_ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROTECTED_PROCESS_PATHS = {"operators/gate.py", "operators/record.py"}
_CONTROLLER_OPERATORS = {"select", "rollout", "analyze", "mutate", "validate", "novelty"}
_ACTION_FIELDS = {
    **agent_research.FIELDS,
    "observe": ({"evidence", "hypothesis"}, {"decision", "claims"}),
    "fork": ({"parent", "genid"}, set()),
    "operator": ({"stage", "genid"}, {"parent", "config", "timeout_s"}),
    "checkpoint": ({"parent", "genid"}, set()),
    "commit": ({"parent", "genid"}, set()),
    "evaluate": ({"genid"}, set()),
    "finalize": ({"genid"}, {"parent"}),
    "submit_champion": ({"genid"}, {"stop_reason"}),
}


@dataclass(frozen=True)
class AgentLimits:
    max_actions: int
    max_operator_calls: int
    max_evaluations: int
    max_cost_usd: float | None = None
    max_wall_s: float | None = None

    def validate(self) -> None:
        for name in ("max_actions", "max_operator_calls", "max_evaluations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(f"{name} must be a non-negative integer")
        if self.max_actions < 1:
            raise RuntimeError("max_actions must be at least 1")
        for name in ("max_cost_usd", "max_wall_s"):
            value = getattr(self, name)
            if value is not None and (_number(value) <= 0 or isinstance(value, bool)):
                raise RuntimeError(f"{name} must be a positive finite number")


@dataclass(frozen=True)
class AgentAction:
    action_id: str
    kind: str
    arguments: dict[str, Any]


def audit_arm_pair(left: Path, right: Path) -> dict[str, Any]:
    workspaces = [left.resolve(), right.resolve()]
    common_dirs = [git_common_dir(workspace) for workspace in workspaces]
    if workspaces[0] == workspaces[1] or common_dirs[0] == common_dirs[1]:
        raise RuntimeError("comparison arms must use independent Git repositories")
    workspace_ids = [(Path(common) / "evolve-workspace-id").read_text().strip() for common in common_dirs]
    if workspace_ids[0] == workspace_ids[1]:
        raise RuntimeError("comparison arms must use distinct evolve workspace identities")
    summaries = []
    for workspace in workspaces:
        champion = best_row(workspace)
        if champion is None or not isinstance(champion.get("candidate_commit"), str):
            raise RuntimeError(f"comparison arm has no certified parent: {workspace}")
        occupied = sorted(set(rows_by_genid(workspace)) - {str(champion.get("genid"))})
        if occupied:
            raise RuntimeError(f"comparison arm has occupied generations at {workspace}: {', '.join(occupied)}")
        commit = str(champion["candidate_commit"])
        summaries.append(
            {
                "workspace": str(workspace),
                "genid": str(champion.get("genid")),
                "candidate_commit": commit,
                "target_tree": git_stdout(workspace, "rev-parse", f"{commit}:target"),
                "operators_tree": git_stdout(workspace, "rev-parse", f"{commit}:operators"),
                "evaluator_tree": git_stdout(workspace, "rev-parse", f"{commit}:evaluator"),
            }
        )
    comparable = {
        key: summaries[0][key] == summaries[1][key]
        for key in ("candidate_commit", "target_tree", "operators_tree", "evaluator_tree")
    }
    if not all(comparable.values()):
        mismatch = ", ".join(key for key, matches in comparable.items() if not matches)
        raise RuntimeError(f"comparison arms do not share the same frozen start: {mismatch}")
    return {"comparable": True, "arms": summaries}


def clone_arm(source: Path, destination: Path) -> dict[str, Any]:
    """Clone one certified baseline into an independent comparison arm."""

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or not (source / ".git").is_dir():
        raise RuntimeError("comparison source must be a standalone Git workspace")
    if destination.exists():
        raise RuntimeError(f"comparison destination already exists: {destination}")
    if _is_relative_to(destination, source):
        raise RuntimeError("comparison destination cannot be inside the source workspace")
    if (source / SESSION_DIR).exists():
        raise RuntimeError("comparison source must not contain an Agent Driven session")
    if _sealed_evidence_exists(source):
        raise RuntimeError("comparison source must not contain sealed evaluation evidence")
    champion = best_row(source)
    if champion is None or not isinstance(champion.get("candidate_commit"), str):
        raise RuntimeError("comparison source has no certified parent")
    occupied = sorted(set(rows_by_genid(source)) - {str(champion.get("genid"))})
    if occupied:
        raise RuntimeError("comparison source has occupied generations: " + ", ".join(occupied))
    if git_stdout(source, "status", "--porcelain"):
        raise RuntimeError("comparison source has uncommitted changes")
    active_worktrees = [path for path in worktree_paths(source) if path != source]
    if active_worktrees:
        raise RuntimeError("comparison source has active child worktrees")

    try:
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "*.pyc"),
        )
        marker = Path(git_common_dir(destination)) / "evolve-workspace-id"
        marker.write_text(f"{uuid.uuid4().hex}\n")
        audit = audit_arm_pair(source, destination)
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination)
        raise
    return {"source": str(source), "destination": str(destination), **audit}


def start_session(
    workspace: Path,
    limits: AgentLimits,
    *,
    require_clean_start: bool = False,
    mode: str = "batch",
    optimizer: Path | None = None,
    objective: str = "",
) -> dict[str, Any]:
    workspace = workspace.resolve()
    limits.validate()
    if mode not in {"batch", "continuous"}:
        raise RuntimeError("mode must be batch or continuous")
    if mode == "continuous" and (optimizer is None or not objective.strip()):
        raise RuntimeError("continuous research requires optimizer and objective")
    if mode == "batch" and (optimizer is not None or objective):
        raise RuntimeError("optimizer and objective require continuous mode")
    champion = best_row(workspace)
    if champion is None:
        raise RuntimeError("Agent Driven mode requires a certified valid parent; run evolve agent prepare first")
    if _sealed_evidence_exists(workspace):
        raise RuntimeError("Agent Driven mode must start before sealed evaluation exists; use a fresh workspace")
    occupied = sorted(set(rows_by_genid(workspace)) - {str(champion.get("genid"))})
    if require_clean_start and occupied:
        raise RuntimeError("Agent Driven clean start has occupied generations: " + ", ".join(occupied))
    root = workspace / SESSION_DIR
    root.mkdir(parents=True, exist_ok=True)
    preflight = run_preflight(
        workspace,
        candidate_commit=str(champion["candidate_commit"]),
        receipt_path=root / "preflight.json",
    )
    if preflight.status is PreflightStatus.FAILED:
        raise RuntimeError(f"Agent Driven environment preflight failed: {preflight.failure_message}")
    with _session_lock(root):
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            manifest = _read_json_object(manifest_path)
            if manifest.get("mode", "batch") != mode or (mode == "continuous" and manifest["objective"] != objective):
                raise RuntimeError("session already exists with a different research mode or objective")
            recorded_limits = manifest.get("limits")
            if isinstance(recorded_limits, dict):
                recorded_limits = {"max_cost_usd": None, "max_wall_s": None, **recorded_limits}
            if recorded_limits != asdict(limits):
                raise RuntimeError("Agent Driven session already exists with different limits")
            if manifest.get("require_clean_start", False) != require_clean_start:
                raise RuntimeError("Agent Driven session already exists with a different clean-start policy")
            return session_status(workspace)
        active_worktrees = [
            path
            for path in worktree_paths(workspace)
            if path != workspace and _is_relative_to(path, workspace / "runs" / "worktrees")
        ]
        if active_worktrees:
            names = ", ".join(path.name for path in active_worktrees)
            raise RuntimeError(f"cannot start Agent Driven session with active child worktree(s): {names}")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "session_id": uuid.uuid4().hex,
            "created_at": _timestamp(),
            "baseline": _candidate_summary(champion),
            "archive_sha256": _archive_digest(workspace),
            "control_tree_sha256": _tracked_tree_digest(workspace),
            "require_clean_start": require_clean_start,
            "limits": asdict(limits),
        }
        if mode == "continuous":
            assert optimizer is not None
            frozen = freeze_optimizer(workspace, optimizer)
            manifest.update(
                schema_version=2,
                mode=mode,
                objective=objective,
                initial_optimizer={**frozen, "activation_id": "initial"},
            )
            (root / "notes").mkdir(exist_ok=True)
            (root / "optimizer/drafts").mkdir(exist_ok=True)
        _atomic_json(manifest_path, manifest)
        _atomic_text(root / "ACTIVE", f"{manifest['session_id']}\n")
    return session_status(workspace)


def parse_action(payload: Mapping[str, Any]) -> AgentAction:
    if not isinstance(payload, Mapping):
        raise RuntimeError("action must be a JSON object")
    action_id = payload.get("id")
    kind = payload.get("type")
    if not isinstance(action_id, str) or _ACTION_ID.fullmatch(action_id) is None:
        raise RuntimeError("action.id must use 1-64 letters, digits, dots, underscores, or hyphens")
    if not isinstance(kind, str) or kind not in _ACTION_FIELDS:
        raise RuntimeError(f"unknown action.type; choose from {', '.join(sorted(_ACTION_FIELDS))}")
    required, optional = _ACTION_FIELDS[kind]
    arguments = {str(key): value for key, value in payload.items() if key not in {"id", "type"}}
    missing = sorted(required - set(arguments))
    unknown = sorted(set(arguments) - required - optional)
    if missing:
        raise RuntimeError(f"action {kind} is missing fields: {', '.join(missing)}")
    if unknown:
        raise RuntimeError(f"action {kind} has unknown fields: {', '.join(unknown)}")
    _validate_action_arguments(kind, arguments)
    return AgentAction(action_id, kind, arguments)


def execute_action(workspace: Path, action: AgentAction) -> dict[str, Any]:
    from .agent_handoff import assert_handoff_complete

    workspace = workspace.resolve()
    root = workspace / SESSION_DIR
    with _session_lock(root):
        assert_handoff_complete(workspace)
        manifest = _read_json_object(root / "manifest.json")
        events = _read_events(root / "actions.jsonl")
        duplicate = _terminal_event(events, action.action_id)
        if duplicate is not None:
            original = next(e for e in events if e.get("action_id") == action.action_id and e.get("phase") == "started")
            if original["action"] != {"type": action.kind, **action.arguments}:
                raise RuntimeError("action ID already records a different request")
            return duplicate
        pending = _pending_action(events)
        if pending is not None:
            raise RuntimeError(
                f"Agent Driven session has interrupted action {pending['action_id']}; resolve it before continuing"
            )
        if _submitted_event(events) is not None:
            raise RuntimeError("Agent Driven session is submitted; it cannot accept another action")
        _assert_workspace_integrity(workspace, manifest, events)
        state = _derive_status(workspace, manifest, events)
        if state["status"] not in {"active", "exhausted"} and not (
            state["status"] == "paused" and action.kind in {"resume_research", "finish_research"}
        ):
            raise RuntimeError(f"Agent Driven session is {state['status']}; it cannot accept another action")
        limits = AgentLimits(**manifest["limits"])
        agent_research.check_mode(action.kind, state)
        if state["status"] == "exhausted" and action.kind not in {"submit_champion", "finish_research"}:
            reasons = ", ".join(state["exhausted_reasons"])
            raise RuntimeError(f"Agent Driven action budget is exhausted: {reasons}")
        _assert_sub_budget(action, state, limits)
        started = {
            "schema_version": SCHEMA_VERSION,
            "phase": "started",
            "seq": len(events) + 1,
            "timestamp": _timestamp(),
            "action_id": action.action_id,
            "action": {"type": action.kind, **action.arguments},
            "workspace_before": _workspace_fingerprint(workspace),
        }
        _append_event(root / "actions.jsonl", started)
        started_at = time.monotonic()
        try:
            result, cost_usd = _dispatch(workspace, action, manifest)
        except Exception as exc:
            failed = {
                "schema_version": SCHEMA_VERSION,
                "phase": "failed",
                "seq": len(events) + 2,
                "timestamp": _timestamp(),
                "action_id": action.action_id,
                "error": str(exc)[:1000],
                "wall_s": round(time.monotonic() - started_at, 6),
                "cost_usd": _observed_action_cost(workspace, action),
                "workspace_after": _workspace_fingerprint(workspace),
            }
            _append_event(root / "actions.jsonl", failed)
            raise
        completed = {
            "schema_version": SCHEMA_VERSION,
            "phase": "completed",
            "seq": len(events) + 2,
            "timestamp": _timestamp(),
            "action_id": action.action_id,
            "result": result,
            "wall_s": round(time.monotonic() - started_at, 6),
            "cost_usd": cost_usd,
            "workspace_after": _workspace_fingerprint(workspace),
        }
        _append_event(root / "actions.jsonl", completed)
        if action.kind in {"submit_champion", "finish_research"}:
            (root / "ACTIVE").unlink(missing_ok=True)
        return completed


def resolve_interrupted(workspace: Path, action_id: str, reason: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = workspace / SESSION_DIR
    with _session_lock(root):
        events = _read_events(root / "actions.jsonl")
        pending = _pending_action(events)
        if pending is None or pending.get("action_id") != action_id:
            raise RuntimeError(f"action {action_id} is not the interrupted action")
        action_payload = pending.get("action")
        observed_cost = 0.0
        if isinstance(action_payload, dict):
            try:
                interrupted = parse_action({"id": action_id, **action_payload})
            except RuntimeError:
                pass
            else:
                observed_cost = _observed_action_cost(workspace, interrupted)
        event = {
            "schema_version": SCHEMA_VERSION,
            "phase": "failed",
            "seq": len(events) + 1,
            "timestamp": _timestamp(),
            "action_id": action_id,
            "error": f"operator-confirmed interrupted action: {reason}"[:1000],
            "wall_s": 0,
            "cost_usd": observed_cost,
            "workspace_after": _workspace_fingerprint(workspace),
        }
        _append_event(root / "actions.jsonl", event)
        return event


def session_status(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = workspace / SESSION_DIR
    manifest = _read_json_object(root / "manifest.json")
    events = _read_events(root / "actions.jsonl")
    finished = (
        manifest.get("mode") == "continuous" and agent_research.project(manifest, events)["lifecycle"] == "finished"
    )
    if not finished and _submitted_event(events) is None and _pending_action(events) is None:
        _assert_workspace_integrity(workspace, manifest, events)
    return _derive_status(workspace, manifest, events)


def seal_submitted(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    state = session_status(workspace)
    if state["status"] not in {"submitted", "finished"}:
        raise RuntimeError("sealed evaluation requires a submitted Agent Driven champion")
    submitted = (
        state["research"]["finish"]["champion"] if state.get("mode") == "continuous" else state["submitted_champion"]
    )
    if not isinstance(submitted, dict) or not isinstance(submitted.get("genid"), str):
        raise RuntimeError("Agent Driven submission receipt has no champion generation")
    genid = submitted["genid"]
    manifest = load_manifest(workspace / "evaluator" / "splits.json")
    if not selected_task_names(manifest, "sealed"):
        return {"genid": genid, "sealed": "not_configured"}

    def evaluate(selected: str) -> dict[str, Any]:
        record = seal_agent_champion(workspace, selected)
        if record is None:
            return {"genid": selected, "sealed": "already complete"}
        return {
            "genid": selected,
            "sealed": record.outcome.value,
            "score": record.score,
            "cost_usd": record.cost_usd,
            "wall_s": record.wall_s,
        }

    if state.get("mode") == "continuous":
        session = _read_json_object(workspace / SESSION_DIR / "manifest.json")
        baseline = evaluate(session["baseline"]["genid"])
        if baseline["sealed"] not in {"already complete", "benchmark_complete"}:
            return {"baseline": baseline, "final": {"genid": genid, "sealed": "not_started"}}
        final = baseline if session["baseline"]["genid"] == genid else evaluate(genid)
        return {"baseline": baseline, "final": final}
    return evaluate(genid)


def _derive_status(workspace: Path, manifest: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    from .agent_observability import session_health

    started = [event for event in events if event.get("phase") == "started"]
    budgeted = [
        event for event in started if event.get("action", {}).get("type") not in {"submit_champion", "finish_research"}
    ]
    operator_calls = [event for event in started if event.get("action", {}).get("type") == "operator"]
    evaluations = [event for event in started if event.get("action", {}).get("type") == "evaluate"]
    pending = _pending_action(events)
    submitted = _submitted_event(events)
    limits = AgentLimits(**manifest["limits"])
    observed_cost = round(
        sum(float(event.get("cost_usd", 0)) for event in events if event.get("phase") in {"completed", "failed"}),
        9,
    )
    elapsed_s = max(0.0, (datetime.now(UTC) - datetime.fromisoformat(str(manifest["created_at"]))).total_seconds())
    from .agent_observability import unpriced_candidate_runs

    unpriced = unpriced_candidate_runs(workspace)
    exhausted_reasons = ["unpriced_candidate_cost"] if unpriced else []
    if len(budgeted) >= limits.max_actions:
        exhausted_reasons.append("actions")
    if limits.max_cost_usd is not None and observed_cost >= limits.max_cost_usd:
        exhausted_reasons.append("cost_usd")
    if limits.max_wall_s is not None and elapsed_s >= limits.max_wall_s:
        exhausted_reasons.append("wall_s")
    status = (
        "submitted"
        if submitted
        else "running"
        if pending and _session_is_locked(workspace)
        else "interrupted"
        if pending
        else "active"
    )
    if status == "active" and exhausted_reasons:
        status = "exhausted"
    research = agent_research.project(manifest, events) if manifest.get("mode") == "continuous" else None
    if research and research["lifecycle"] != "active" and pending is None:
        status = research["lifecycle"]
    champion = best_row(workspace)
    occupied_generations = sorted(
        rows_by_genid(workspace),
        key=lambda value: (1, value) if not value.isdigit() else (0, int(value)),
    )
    return {
        "schema_version": manifest.get("schema_version", SCHEMA_VERSION),
        "mode": manifest.get("mode", "batch"),
        "research": research,
        "session_id": manifest["session_id"],
        "status": status,
        "baseline": manifest["baseline"],
        "champion": _candidate_summary(champion) if champion else None,
        "occupied_generations": occupied_generations,
        "limits": manifest["limits"],
        "actions_used": len(budgeted),
        "operator_calls_used": len(operator_calls),
        "evaluations_used": len(evaluations),
        "actions_remaining": max(0, limits.max_actions - len(budgeted)),
        "operator_calls_remaining": max(0, limits.max_operator_calls - len(operator_calls)),
        "evaluations_remaining": max(0, limits.max_evaluations - len(evaluations)),
        "observed_cost_usd": observed_cost,
        "wall_elapsed_s": round(elapsed_s, 6),
        "cost_remaining_usd": (
            None if limits.max_cost_usd is None else round(max(0.0, limits.max_cost_usd - observed_cost), 9)
        ),
        "wall_remaining_s": None if limits.max_wall_s is None else round(max(0.0, limits.max_wall_s - elapsed_s), 6),
        "exhausted_reasons": exhausted_reasons,
        "unpriced_candidate_runs": unpriced,
        "health": session_health(workspace, events, status),
        "pending_action": pending.get("action_id") if pending else None,
        "submitted_champion": submitted.get("result") if submitted else None,
        "receipt_path": str((workspace / SESSION_DIR / "actions.jsonl").resolve()),
    }


def _dispatch(workspace: Path, action: AgentAction, manifest: dict[str, Any]) -> tuple[dict[str, Any], float]:
    args = action.arguments
    if action.kind in agent_research.FIELDS:
        return agent_research.dispatch(workspace, action, session_status(workspace)), 0
    if action.kind == "observe":
        from .agent_evidence import verify_claims

        if "claims" in args:
            return verify_claims(workspace, args["claims"], args["evidence"]), 0
        return {"recorded": True, "verified_claims": [], "prose_verified": False}, 0
    if action.kind == "fork":
        child = _child_path(workspace, args["genid"])
        fork_agent_child(workspace, args["parent"], child, _agent_session=True)
        return {"child_worktree": str(child), "parent_commit": git_stdout(child, "rev-parse", "HEAD")}, 0
    if action.kind == "checkpoint":
        return _candidate_changes(workspace, args["parent"], args["genid"]), 0
    if action.kind == "operator":
        parent = args.get("parent")
        checkout = None if args["stage"] == "select" else _child_path(workspace, args["genid"])
        operator_commit = (
            _certified_parent_commit(workspace, parent) if parent is not None else _current_champion_commit(workspace)
        )
        invocation = invoke_operator(
            workspace,
            args["stage"],
            args["genid"],
            parent=parent,
            checkout=checkout,
            config_override=None,
            timeout_s=None,
            operator_ref=operator_commit,
            _agent_session=True,
        )
        usage = _operator_usage(invocation.run_dir, args["stage"])
        cost = _number(usage.get("usd"))
        return {
            "stage": args["stage"],
            "artifacts": str(invocation.run_dir),
            "operator_source": f"gen/{parent}" if parent is not None else "current champion",
            "usage": usage,
        }, cost
    if action.kind == "commit":
        changes = _candidate_changes(workspace, args["parent"], args["genid"])
        protected = changes["protected_process_violations"]
        if protected:
            raise RuntimeError("Agent Driven candidates cannot change protected process paths: " + ", ".join(protected))
        commit_agent_child(
            workspace,
            _child_path(workspace, args["genid"]),
            args["parent"],
            args["genid"],
            _agent_session=True,
        )
        row = rows_by_genid(workspace).get(args["genid"], {})
        candidate = _candidate_summary(row) or {"genid": args["genid"]}
        candidate["candidate_commit"] = git_stdout(workspace, "rev-parse", f"gen/{args['genid']}^{{commit}}")
        return {**changes, "candidate": candidate}, 0
    if action.kind == "evaluate":
        from .agent_observability import evaluation_execution

        record = eval_agent_child(workspace, args["genid"], _agent_session=True)
        if record is None:
            return {"genid": args["genid"], "evaluation": "already terminal"}, 0
        return {
            "genid": args["genid"],
            "evaluation": record.outcome.value,
            "score": record.score,
            "cost_usd": record.cost_usd,
            "execution": evaluation_execution(record),
        }, float(record.cost_usd)
    if action.kind == "finalize":
        baseline = manifest.get("baseline")
        operator_ref = baseline.get("candidate_commit") if isinstance(baseline, dict) else None
        if not isinstance(operator_ref, str) or not operator_ref:
            raise RuntimeError("Agent Driven manifest has no immutable mechanism commit")
        changed = finalize_child(
            workspace,
            args["genid"],
            parent=args.get("parent"),
            operator_ref=operator_ref,
            _agent_session=True,
        )
        return {"genid": args["genid"], "finalized": changed}, 0
    if action.kind == "submit_champion":
        matches = [row for row in valid_parent_rows(workspace) if str(row.get("genid")) == args["genid"]]
        if not matches:
            raise RuntimeError(f"gen/{args['genid']} is not a certified valid parent")
        summary = _candidate_summary(matches[-1])
        assert summary is not None
        if "stop_reason" in args:
            summary["stop_reason"] = args["stop_reason"]
        return summary, 0
    raise AssertionError(f"unhandled action: {action.kind}")


def _candidate_changes(workspace: Path, parent: str, genid: str) -> dict[str, Any]:
    child = _child_path(workspace, genid)
    if not child.is_dir():
        raise RuntimeError(f"candidate worktree does not exist: {child}")
    matches = [row for row in valid_parent_rows(workspace) if str(row.get("genid")) == parent]
    if not matches:
        raise RuntimeError(f"gen/{parent} is not a certified valid parent")
    parent_commit = str(matches[-1]["candidate_commit"])
    if git_stdout(child, "rev-parse", "HEAD") != parent_commit:
        raise RuntimeError(f"candidate gen/{genid} is not forked from certified parent gen/{parent}")
    paths = working_tree_changed_paths(child, parent_commit)
    include, exclude = surface_patterns(workspace)
    violations = check_paths(paths, include, exclude)
    snapshot = None if violations else build_candidate_snapshot(child, parent_commit, include=include, exclude=exclude)
    return {
        "candidate_tree": snapshot.tree if snapshot else None,
        "changed_paths": paths,
        "target_paths": [path for path in paths if path == "target" or path.startswith("target/")],
        "process_paths": [path for path in paths if path == "operators" or path.startswith("operators/")],
        "protected_process_violations": sorted(_PROTECTED_PROCESS_PATHS & set(paths)),
        "surface_violations": violations,
    }


def _certified_parent_commit(workspace: Path, parent: str) -> str:
    matches = [row for row in valid_parent_rows(workspace) if str(row.get("genid")) == parent]
    if not matches or not isinstance(matches[-1].get("candidate_commit"), str):
        raise RuntimeError(f"gen/{parent} is not a certified valid parent")
    return str(matches[-1]["candidate_commit"])


def _current_champion_commit(workspace: Path) -> str:
    champion = best_row(workspace)
    if champion is None or not isinstance(champion.get("candidate_commit"), str):
        raise RuntimeError("Agent Driven session has no certified current champion")
    return str(champion["candidate_commit"])


def _validate_action_arguments(kind: str, args: dict[str, Any]) -> None:
    if kind in agent_research.FIELDS:
        agent_research.validate(kind, args)
    for key in ("parent", "genid"):
        if key not in args:
            continue
        value = args[key]
        if not isinstance(value, str) or not valid_genid(value):
            raise RuntimeError(f"action.{key} must be a generation id")
    if kind == "operator":
        if not isinstance(args["stage"], str):
            raise RuntimeError("action.stage must be a string")
        if args["stage"] not in _CONTROLLER_OPERATORS:
            raise RuntimeError("action.stage is not controller-accessible")
        if args["stage"] != "select" and "parent" not in args:
            raise RuntimeError("non-select operator actions require parent")
        if args["stage"] == "select" and "parent" in args:
            raise RuntimeError("select operator actions must not provide parent")
        if "config" in args:
            raise RuntimeError("Agent Driven operator config overrides are disabled; freeze them in the recipe")
        if "timeout_s" in args:
            raise RuntimeError("Agent Driven operator timeout overrides are disabled; freeze them in the recipe")
    if kind == "submit_champion" and "stop_reason" in args:
        if args["stop_reason"] not in ("completed", "budget", "infrastructure", "no_improvement", "cancelled"):
            raise RuntimeError("invalid champion stop_reason")
    if kind == "observe":
        evidence = args["evidence"]
        if not isinstance(evidence, list) or not evidence or len(evidence) > 20:
            raise RuntimeError("action.evidence must be a non-empty list with at most 20 entries")
        if not all(isinstance(item, str) and 0 < len(item) <= 500 for item in evidence):
            raise RuntimeError("each action.evidence entry must be a non-empty string of at most 500 characters")
        for key in ("hypothesis", "decision"):
            if key in args and (not isinstance(args[key], str) or not 0 < len(args[key]) <= 2000):
                raise RuntimeError(f"action.{key} must be a non-empty string of at most 2000 characters")


def _assert_sub_budget(action: AgentAction, state: dict[str, Any], limits: AgentLimits) -> None:
    if action.kind == "operator" and state["operator_calls_used"] >= limits.max_operator_calls:
        raise RuntimeError("Agent Driven operator-call budget is exhausted")
    if action.kind == "evaluate" and state["evaluations_used"] >= limits.max_evaluations:
        raise RuntimeError("Agent Driven evaluation budget is exhausted")


def _candidate_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "genid": str(row.get("genid")),
        "candidate_commit": row.get("candidate_commit"),
        "score": row.get("score"),
        "valid_parent": row.get("valid_parent") is True,
    }


def _sealed_evidence_exists(workspace: Path) -> bool:
    return any(
        isinstance(evaluation, dict) and evaluation.get("purpose") == "anchor"
        for row in rows_by_genid(workspace).values()
        for evaluation in row.get("evals", [])
    )


def _workspace_fingerprint(workspace: Path) -> dict[str, Any]:
    champion = best_row(workspace)
    return {"archive_sha256": _archive_digest(workspace), "champion": _candidate_summary(champion)}


def _assert_workspace_integrity(workspace: Path, manifest: dict[str, Any], events: list[dict[str, Any]]) -> None:
    expected_control = manifest.get("control_tree_sha256")
    if expected_control is not None and _tracked_tree_digest(workspace) != expected_control:
        raise RuntimeError("Agent Driven control tree changed outside the action boundary")
    terminal = next(
        (event for event in reversed(events) if event.get("phase") in {"completed", "failed"}),
        None,
    )
    before = terminal.get("workspace_after") if isinstance(terminal, dict) else None
    expected_archive = before.get("archive_sha256") if isinstance(before, dict) else manifest.get("archive_sha256")
    if expected_archive != _archive_digest(workspace):
        raise RuntimeError("Agent Driven archive changed outside the action boundary")


def _archive_digest(workspace: Path) -> str | None:
    archive = archive_path(workspace)
    return hashlib.sha256(archive.read_bytes()).hexdigest() if archive.is_file() else None


def _tracked_tree_digest(workspace: Path) -> str:
    digest = hashlib.sha256()
    for relative in sorted(filter(None, git_stdout(workspace, "ls-files").splitlines())):
        path = workspace / relative
        digest.update(relative.encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _terminal_event(events: list[dict[str, Any]], action_id: str) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in reversed(events)
            if event.get("action_id") == action_id and event.get("phase") in {"completed", "failed"}
        ),
        None,
    )


def _pending_action(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    terminal = {str(event.get("action_id")) for event in events if event.get("phase") in {"completed", "failed"}}
    return next(
        (
            event
            for event in reversed(events)
            if event.get("phase") == "started" and str(event.get("action_id")) not in terminal
        ),
        None,
    )


def _submitted_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in reversed(events)
            if event.get("phase") == "completed"
            and _started_kind(events, str(event.get("action_id"))) == "submit_champion"
        ),
        None,
    )


def _started_kind(events: list[dict[str, Any]], action_id: str) -> str | None:
    for event in events:
        if event.get("phase") == "started" and event.get("action_id") == action_id:
            action = event.get("action")
            return str(action.get("type")) if isinstance(action, dict) else None
    return None


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid Agent Driven receipt at line {line_number}") from exc
        if not isinstance(event, dict):
            raise RuntimeError(f"invalid Agent Driven receipt at line {line_number}")
        events.append(event)
    return events


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"missing or invalid Agent Driven session manifest: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid Agent Driven session manifest: {path}")
    return value


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _session_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another Agent Driven action is already running") from exc
        yield


def _session_is_locked(workspace: Path) -> bool:
    lock = workspace / SESSION_DIR / ".lock"
    with lock.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return False


def _child_path(workspace: Path, genid: str) -> Path:
    return workspace / "runs" / "worktrees" / f"gen-{genid}"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return 0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0
    return result if math.isfinite(result) else 0


def _operator_usage(run_dir: Path, stage: str) -> dict[str, Any]:
    path = run_dir / stage / "usage.json"
    if path.is_file():
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {"wall_s": 0, "usd": 0}
        return value if isinstance(value, dict) else {"wall_s": 0, "usd": 0}
    cases_path = run_dir / stage / "cases.json"
    if not cases_path.is_file():
        return {"wall_s": 0, "usd": 0}
    try:
        cases = json.loads(cases_path.read_text())
    except json.JSONDecodeError:
        return {"wall_s": 0, "usd": 0}
    if not isinstance(cases, list):
        return {"wall_s": 0, "usd": 0}
    return {
        "wall_s": sum(
            sum(_number(v) for v in case["timing_s"].values())
            if isinstance(case.get("timing_s"), dict)
            else _number(case.get("timing_s"))
            for case in cases
            if isinstance(case, dict)
        ),
        "usd": sum(
            _number(case.get("usage", {}).get("cost_usd"))
            for case in cases
            if isinstance(case, dict) and isinstance(case.get("usage"), dict)
        ),
    }


def _observed_action_cost(workspace: Path, action: AgentAction) -> float:
    if action.kind == "operator":
        return _number(
            _operator_usage(workspace / "runs" / f"gen-{action.arguments['genid']}", action.arguments["stage"]).get(
                "usd"
            )
        )
    if action.kind == "evaluate":
        row = rows_by_genid(workspace).get(action.arguments["genid"], {})
        return _number(row.get("cost_usd"))
    return 0
