"""Receipt-backed incident history; successful completion never erases anomalies."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .evaluation.results import EvaluationRecord, Outcome


def unpriced_candidate_runs(workspace: Path) -> list[str]:
    return [
        str(path.relative_to(workspace))
        for path in (workspace / "runs").rglob("candidate-isolation/receipt.json")
        if json.loads(path.read_text()).get("usage_status") == "unknown"
    ]


def evaluation_execution(record: EvaluationRecord) -> dict[str, Any]:
    """Expose aggregate trial outcomes without exposing held-out task details."""
    return {
        "observed_trials": len(record.trials),
        "expected_trials": record.expected_trials,
        "scoreable_trials": record.scoreable_trials,
        "timeouts_by_owner": dict(Counter(t.owner for t in record.trials if t.outcome == Outcome.TIMEOUT)),
        "exception_counts": dict(Counter(t.exception_type for t in record.trials if t.exception_type)),
    }


def session_health(workspace: Path, events: list[dict[str, Any]], status: str) -> dict[str, Any]:
    from .agent_driver import SESSION_DIR, _read_events, _read_json_object

    root = workspace / SESSION_DIR
    incidents = []
    for path in sorted((root / "controller").glob("attempt-*/action-rejection.json")):
        incidents.append(
            {
                "kind": "action_rejected",
                "classification": "no_side_effect",
                "evidence": str(path.relative_to(workspace)),
            }
        )
    for event in events:
        execution = event.get("result", {}).get("execution", {})
        if event.get("phase") == "completed" and execution.get("timeouts_by_owner"):
            incidents.append(
                {
                    "kind": "evaluation_trial_timeouts",
                    "classification": "see_reported_owners",
                    "action_id": event["action_id"],
                    "timeouts_by_owner": execution["timeouts_by_owner"],
                    "evidence": f"{SESSION_DIR}/actions.jsonl",
                    "seq": event.get("seq"),
                }
            )
        if event.get("phase") == "failed":
            incidents.append(
                {
                    "kind": "action_failed",
                    "classification": "unknown",
                    "action_id": event["action_id"],
                    "evidence": f"{SESSION_DIR}/actions.jsonl",
                    "seq": event.get("seq"),
                }
            )
        elif event.get("result", {}).get("evaluation") == "infrastructure_failed":
            incidents.append(
                {
                    "kind": "evaluation_infrastructure_failed",
                    "classification": "infrastructure",
                    "action_id": event["action_id"],
                    "evidence": f"{SESSION_DIR}/actions.jsonl",
                    "seq": event.get("seq"),
                }
            )
    for event in _read_events(root / "handoff-recoveries.jsonl"):
        incidents.append(
            {
                "kind": "handoff_recovered",
                "classification": "unknown",
                "action_id": event["action_id"],
                "recovery": "terminal_receipt_reused",
                "evidence": f"{SESSION_DIR}/handoff-recoveries.jsonl",
            }
        )
    controller = _read_events(root / "controller/attempts.jsonl")
    terminal = {e["attempt"] for e in controller if e.get("phase") in {"completed", "failed"}}
    for event in controller:
        if event.get("phase") == "failed":
            incidents.append(
                {
                    "kind": "controller_failed",
                    "classification": "unknown",
                    "attempt": event["attempt"],
                    "returncode": event.get("returncode"),
                    "timed_out": event.get("timed_out"),
                    "evidence": f"{SESSION_DIR}/controller/attempts.jsonl",
                }
            )
    # Pending work is explicit uncertainty, not evidence of a crash or of success.
    pending = [e["attempt"] for e in controller if e.get("phase") == "started" and e["attempt"] not in terminal]
    unresolved: list[dict[str, Any]] = [
        {"kind": "candidate_cost_unknown", "evidence": path} for path in unpriced_candidate_runs(workspace)
    ]
    if status == "interrupted":
        unresolved.append({"kind": "action_outcome_unknown", "evidence": f"{SESSION_DIR}/actions.jsonl"})
    if pending:
        unresolved.append(
            {
                "kind": "controller_outcome_pending",
                "attempts": pending,
                "evidence": f"{SESSION_DIR}/controller/attempts.jsonl",
            }
        )
    queue = root / "deferred-action.json"
    if queue.exists():
        receipt = _read_json_object(queue)
        if receipt.get("status") == "queued" and any(
            e.get("action_id") == receipt["action"]["id"] and e.get("phase") in {"completed", "failed"} for e in events
        ):
            unresolved.append(
                {
                    "kind": "handoff_ack_pending",
                    "action_id": receipt["action"]["id"],
                    "evidence": f"{SESSION_DIR}/deferred-action.json",
                }
            )
    return {
        "status": "attention_required"
        if unresolved
        else "incidents_recorded"
        if incidents
        else "no_recorded_incidents",
        "incident_count": len(incidents),
        "incidents": incidents,
        "unresolved": unresolved,
        "coverage": "agent_action_controller_handoff_and_reported_evaluation_timeouts",
    }
