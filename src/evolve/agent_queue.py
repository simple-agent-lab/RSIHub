"""Durable handoff: the controller requests work, then exits while it executes."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .agent_driver import (
    SESSION_DIR,
    AgentAction,
    _append_event,
    _atomic_json,
    _read_events,
    _read_json_object,
    _session_lock,
    _timestamp,
    execute_action,
    parse_action,
    session_status,
)
from .agent_launcher import ControllerConfig, ControllerLimits, controller_status, launch_controller
from .agent_research import check_mode, correction
from .runtime.model_broker import ModelBrokerConfig
from .runtime.sandbox import SandboxConfig


def defer_action(workspace: Path, action: AgentAction) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = workspace / SESSION_DIR
    with _session_lock(root):
        from .agent_handoff import assert_handoff_complete

        assert_handoff_complete(workspace)
        return _defer_action_locked(workspace, action)


def _defer_action_locked(workspace: Path, action: AgentAction, *, validate_only: bool = False) -> dict[str, Any]:
    root = workspace / SESSION_DIR
    state = session_status(workspace)
    check_mode(action.kind, state)
    if state["status"] not in {"active", "exhausted"} and not (
        state["status"] == "paused" and action.kind in {"resume_research", "finish_research"}
    ):
        raise RuntimeError(f"cannot defer work while session is {state['status']}")
    path = root / "deferred-action.json"
    payload = {"id": action.action_id, "type": action.kind, **action.arguments}
    for event in _read_events(root / "actions.jsonl"):
        if event.get("action_id") == action.action_id and event.get("phase") == "started":
            if event.get("action") != {"type": action.kind, **action.arguments}:
                raise RuntimeError("action ID already records a different request")
    if path.exists():
        previous = _read_json_object(path)
        if previous["action"] == payload:
            return previous
        if previous["status"] != "completed":
            raise RuntimeError("a deferred action already awaits execution")
        if previous["action"]["id"] == action.action_id:
            raise RuntimeError("deferred action ID cannot be reused with different arguments")
    receipt = {"schema_version": 1, "status": "queued", "action": payload}
    if not validate_only:
        _atomic_json(path, receipt)
    return receipt


def drain_action(workspace: Path) -> bool:
    """Replay completed action receipts if a crash preceded queue acknowledgment."""
    from .agent_handoff import recover_handoff

    recover_handoff(workspace)
    path = workspace / SESSION_DIR / "deferred-action.json"
    if not path.exists():
        return False
    receipt = _read_json_object(path)
    if receipt["status"] == "completed":
        return False
    action = parse_action(receipt["action"])
    replayed = any(
        e.get("action_id") == action.action_id and e.get("phase") == "completed"
        for e in _read_events(workspace / SESSION_DIR / "actions.jsonl")
    )
    result = execute_action(workspace, action)
    if result.get("phase") != "completed":
        raise RuntimeError("deferred action failed; inspect its durable receipt before recovery")
    with _session_lock(workspace / SESSION_DIR):
        current = _read_json_object(path)
        if current != receipt:
            raise RuntimeError("deferred action changed during execution")
        if replayed:
            recoveries = workspace / SESSION_DIR / "handoff-recoveries.jsonl"
            if not any(e.get("action_id") == action.action_id for e in _read_events(recoveries)):
                _append_event(
                    recoveries,
                    {
                        "schema_version": 1,
                        "timestamp": _timestamp(),
                        "action_id": action.action_id,
                        "recovery": "terminal_receipt_reused",
                        "cause": "unknown",
                    },
                )
        _atomic_json(path, {**receipt, "status": "completed", "result": result})
    return True


def resolve_deferred(workspace: Path, reason: str) -> dict[str, Any]:
    """Release a queued/failed handoff only after its action is no longer live."""
    if not reason.strip():
        raise RuntimeError("deferred resolution requires a reason")
    root = workspace.resolve() / SESSION_DIR
    with _session_lock(root):
        state = session_status(workspace)
        if state["status"] in {"running", "interrupted"}:
            raise RuntimeError("resolve the live or interrupted action before its deferred handoff")
        path = root / "deferred-action.json"
        receipt = _read_json_object(path)
        if receipt["status"] == "completed":
            return receipt
        resolved = {**receipt, "status": "completed", "resolution": reason}
        _append_event(root / "deferred-resolutions.jsonl", resolved)
        _atomic_json(path, resolved)
        return resolved


def _close_budget(workspace: Path, reasons: list[str]) -> None:
    """Submit the certified best without launching more work or erasing usage."""
    state = session_status(workspace)
    status = controller_status(workspace)
    if state["status"] in {"running", "interrupted"} or status["pending_attempt"] is not None:
        raise RuntimeError("budget exhausted with interrupted work; explicit recovery required")
    if status["unpriced_controller_attempts"]:
        raise RuntimeError("controller usage is unknown; reconcile receipts before budget closure")
    root = workspace / SESSION_DIR
    _atomic_json(root / "progression/budget-stop.json", {"exhausted_reasons": reasons, "controller": status})
    if (root / "deferred-action.json").exists():
        resolve_deferred(workspace, "budget exhausted: " + ", ".join(reasons))
    if state.get("mode") == "continuous":
        execute_action(
            workspace, parse_action({"id": "host-budget-stop", "type": "finish_research", "reason": "budget"})
        )
        return
    action = parse_action(
        {
            "id": "host-budget-stop",
            "type": "submit_champion",
            "genid": state["champion"]["genid"],
            "stop_reason": "budget",
        }
    )
    execute_action(workspace, action)


def drive_controller(workspace: Path, config: ControllerConfig) -> dict[str, Any]:
    """Advance durable handoffs without running a model while an action executes.

    Interrupted actions remain explicit recovery boundaries. This loop never
    resubmits an uncertain external operation or silently restarts a failure.
    """
    workspace = workspace.resolve()
    root = workspace / SESSION_DIR / "progression"
    root.mkdir(parents=True, exist_ok=True)
    with _session_lock(root):
        manifest_path = root / "manifest.json"
        expected = {"schema_version": 1, "controller": asdict(config)}
        # Normalize tuples to the JSON representation before comparing restarts.
        expected = json.loads(json.dumps(expected))
        if config.sandbox is None:
            expected["controller"].pop("sandbox", None)
        if config.model_broker is None:
            expected["controller"].pop("model_broker", None)
        if manifest_path.exists() and _read_json_object(manifest_path) != expected:
            raise RuntimeError("progression already exists with different controller configuration")
        _atomic_json(manifest_path, expected)
        try:
            while True:
                from .agent_handoff import recover_handoff

                recover_handoff(workspace)
                state = session_status(workspace)
                if (workspace / SESSION_DIR / "controller/manifest.json").exists():
                    if controller_status(workspace)["pending_attempt"] is not None:
                        raise RuntimeError(
                            "controller attempt is interrupted; reconcile usage before executing deferred work"
                        )
                if state["status"] == "paused":
                    queued = workspace / SESSION_DIR / "deferred-action.json"
                    request = _read_json_object(queued) if queued.exists() else {}
                    if request.get("status") == "queued" and request["action"]["type"] in {
                        "resume_research",
                        "finish_research",
                    }:
                        drain_action(workspace)
                        continue
                if state["status"] in {"paused", "finished"}:
                    result = {
                        "status": state["status"],
                        "research": state["research"],
                        "champion": state["champion"],
                        "health": state["health"],
                        "objective_completion": "not_assessed",
                    }
                    _atomic_json(root / "status.json", result)
                    return result
                if state["status"] == "submitted":
                    result = {
                        "status": "submitted",
                        "evaluations_used": state["evaluations_used"],
                        "evaluation_limit": state["limits"]["max_evaluations"],
                        "champion": state["submitted_champion"],
                        "objective_completion": "not_assessed",
                        "health": state["health"],
                        "stop_reason": (state["submitted_champion"] or {}).get("stop_reason", "unspecified"),
                    }
                    if (root / "budget-stop.json").exists():
                        result["budget_exhausted_reasons"] = _read_json_object(root / "budget-stop.json")[
                            "exhausted_reasons"
                        ]
                    _atomic_json(root / "status.json", result)
                    return result
                if (workspace / SESSION_DIR / "controller/manifest.json").exists():
                    reasons = controller_status(workspace)["exhausted_reasons"] + state["exhausted_reasons"]
                    if reasons:
                        queued = workspace / SESSION_DIR / "deferred-action.json"
                        receipt = _read_json_object(queued) if queued.exists() else {}
                        if receipt.get("status") == "queued" and receipt["action"]["type"] == "submit_champion":
                            drain_action(workspace)
                        else:
                            _close_budget(workspace, reasons)
                        continue
                _atomic_json(root / "status.json", {"status": "executing_deferred_action"})
                if drain_action(workspace):
                    continue
                _atomic_json(root / "status.json", {"status": "controller_running"})
                launch_controller(workspace, config, deferred_actions=True)
                state = session_status(workspace)
                if state["status"] in {"submitted", "finished", "paused"}:
                    continue
                pending = workspace / SESSION_DIR / "deferred-action.json"
                if not pending.exists() or _read_json_object(pending)["status"] != "queued":
                    if state.get("mode") == "continuous":
                        feedback = correction(workspace, controller_status(workspace)["attempts_used"])
                        if feedback and feedback["count"] < 3:
                            continue
                    raise RuntimeError("controller exited without a deferred action or champion submission")

        except BaseException as exc:
            _atomic_json(root / "status.json", {"status": "blocked", "reason": type(exc).__name__})
            raise


def resume_controller(workspace: Path) -> dict[str, Any]:
    """Resume progression with its persisted configuration and original budgets."""
    manifest = _read_json_object(workspace / SESSION_DIR / "progression/manifest.json")
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported progression manifest")
    saved = manifest["controller"]
    config = ControllerConfig(
        saved["command"],
        tuple(saved["arguments"]),
        ControllerLimits(**saved["limits"]),
        SandboxConfig(**saved["sandbox"]) if saved.get("sandbox") is not None else None,
        ModelBrokerConfig(
            tuple(saved["model_broker"]["command"]),
            saved["model_broker"]["max_requests"],
            saved["model_broker"]["max_cost_usd"],
            saved["model_broker"]["timeout_s"],
        )
        if saved.get("model_broker") is not None
        else None,
    )
    config.validate()
    return drive_controller(workspace, config)
