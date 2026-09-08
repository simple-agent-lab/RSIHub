"""Continuous research lifecycle projected from the existing action journal."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .agent_optimizer import draft_source, freeze_optimizer, verify_optimizer

FIELDS = {
    "adopt_optimizer": ({"expected_digest", "reason"}, {"draft_path", "digest"}),
    "publish_best": ({"genid"}, set()),
    "pause_research": ({"reason", "resume_condition"}, set()),
    "resume_research": ({"reason"}, set()),
    "finish_research": ({"reason"}, set()),
}


def validate(kind: str, args: dict[str, Any]) -> None:
    for key in ("expected_digest", "reason", "resume_condition", "draft_path", "digest"):
        if key in args and (not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > 2000):
            raise RuntimeError(f"action.{key} must be a non-empty string of at most 2000 characters")
    if kind == "adopt_optimizer" and (("draft_path" in args) == ("digest" in args)):
        raise RuntimeError("adopt_optimizer requires exactly one of draft_path or digest")


def project(manifest: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    state = {
        "lifecycle": "active",
        "objective": manifest["objective"],
        "active_optimizer": manifest["initial_optimizer"],
        "last_publication": None,
        "suspension": None,
    }
    for event in events:
        if event.get("phase") != "completed":
            continue
        result = event.get("result", {})
        transition = result.get("research_transition")
        if transition == "adopt_optimizer":
            state["active_optimizer"] = result["optimizer"]
        elif transition == "publish_best":
            state["last_publication"] = result["champion"]
        elif transition == "pause_research":
            state.update(lifecycle="paused", suspension=result)
        elif transition == "resume_research":
            state.update(lifecycle="active", suspension=None)
        elif transition == "finish_research":
            state.update(lifecycle="finished", finish=result)
    return state


def check_mode(kind: str, state: dict[str, Any]) -> None:
    continuous = state.get("mode") == "continuous"
    if kind in FIELDS and not continuous:
        raise RuntimeError(f"{kind} requires a continuous research session")
    if continuous and kind == "submit_champion":
        raise RuntimeError("continuous research uses publish_best or finish_research, not submit_champion")


def dispatch(workspace: Path, action: Any, state: dict[str, Any]) -> dict[str, Any]:
    args = action.arguments
    result = {"research_transition": action.kind}
    if action.kind == "adopt_optimizer":
        current = state["research"]["active_optimizer"]
        if args["expected_digest"] != current["digest"]:
            raise RuntimeError("active optimizer changed; read current state before adopting")
        if "draft_path" in args:
            optimizer = freeze_optimizer(workspace, draft_source(workspace, args["draft_path"]))
        else:
            # Only already-published snapshots may be adopted by digest.
            digest = args["digest"]
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise RuntimeError("invalid optimizer digest")
            optimizer = freeze_optimizer(workspace, workspace / "runs/agent-driven/optimizer/versions" / digest)
            if optimizer["digest"] != digest:
                raise RuntimeError("optimizer snapshot was modified")
        verify_optimizer(workspace, optimizer)
        result.update(optimizer={**optimizer, "activation_id": action.action_id}, reason=args["reason"])
    elif action.kind == "publish_best":
        if args["genid"] != state["champion"]["genid"]:
            raise RuntimeError("publish_best requires the certified current best")
        result["champion"] = state["champion"]
    elif action.kind == "finish_research":
        result.update(champion=state["champion"], reason=args["reason"])
    else:
        result.update(args)
    return result


def record_rejection(workspace: Path, error: str, *, attempt: int | None = None) -> None:
    """Record only parse refusals, before any action or external effect starts."""
    value = str(attempt) if attempt is not None else os.environ.get("EVOLVE_CONTROLLER_ATTEMPT")
    if not value or not value.isdigit():
        return
    path = workspace / "runs/agent-driven/controller" / f"attempt-{int(value)}"
    if not path.is_dir():
        return
    (path / "action-rejection.json").write_text(json.dumps({"effect": "none", "error": error[:2000]}))


def correction(workspace: Path, attempts: int) -> dict[str, Any] | None:
    root = workspace / "runs/agent-driven/controller"
    path = root / f"attempt-{attempts}" / "action-rejection.json"
    if not path.is_file():
        return None
    receipt = json.loads(path.read_text())
    if receipt.get("effect") != "none":
        return None
    previous = root / "correction.json"
    old = json.loads(previous.read_text()) if previous.is_file() else {}
    count = old.get("count", 0) + 1 if old.get("attempt") == attempts - 1 else 1
    result = {**receipt, "attempt": attempts, "count": count}
    previous.write_text(json.dumps(result))
    return result
