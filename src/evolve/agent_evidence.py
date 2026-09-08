"""Check explicit factual claims; free-form hypotheses are never certified facts."""

import hashlib
import json
from pathlib import Path
from typing import Any


def verify_claims(workspace: Path, claims: Any, evidence: list[str]) -> dict[str, Any]:
    if not isinstance(claims, list) or not claims or len(claims) > 50:
        raise RuntimeError("evidence claims must be a non-empty list of at most 50 items")
    verified = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"path", "sha256", "keys", "value"}:
            raise RuntimeError("claim requires path, sha256, keys, value")
        path, keys = claim["path"], claim["keys"]
        if not isinstance(path, str) or path not in evidence or Path(path).is_absolute():
            raise RuntimeError("claim path must name listed workspace evidence")
        source = (workspace / path).resolve()
        if not source.is_relative_to(workspace.resolve()):
            raise RuntimeError("claim evidence escapes workspace")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise RuntimeError("claim keys must be a list of object keys")
        try:
            content = facts_bytes(workspace) if path == "@session" else source.read_bytes()
            if hashlib.sha256(content).hexdigest() != claim["sha256"]:
                raise RuntimeError("claim evidence digest mismatch: " + path)
            actual = json.loads(content)
            for key in keys:
                actual = actual[key]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("claim evidence is missing or invalid: " + path) from exc
        if type(actual) is not type(claim["value"]) or actual != claim["value"]:
            raise RuntimeError("claim contradicts evidence: " + path + " / " + "/".join(keys))
        verified.append(claim)
    return {"recorded": True, "verified_claims": verified, "prose_verified": False}


def facts_bytes(workspace: Path) -> bytes:
    """Derive stable facts from host session receipts, never a controller-chosen file."""
    from .agent_driver import SESSION_DIR, _read_events, session_status

    state = session_status(workspace)
    facts = {key: state[key] for key in ("session_id", "baseline", "champion", "evaluations_used", "observed_cost_usd")}
    events = _read_events(workspace / SESSION_DIR / "actions.jsonl")
    kinds = {e["action_id"]: e["action"]["type"] for e in events if e.get("phase") == "started"}
    facts["evaluations"] = {
        e["action_id"]: e["result"]
        for e in events
        if e.get("phase") == "completed" and kinds.get(e["action_id"]) == "evaluate"
    }
    facts["health"] = state["health"]
    facts["research"] = state["research"]
    facts["research_workspace"] = "runs/agent-driven/notes"
    facts["failed_actions"] = [e["action_id"] for e in events if e.get("phase") == "failed"]
    return (json.dumps(facts, sort_keys=True, allow_nan=False) + "\n").encode()
