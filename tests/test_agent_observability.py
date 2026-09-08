import json

from test_agent_launcher import _session

from evolve.agent_driver import _append_event, execute_action, parse_action, session_status
from evolve.agent_evidence import facts_bytes
from evolve.agent_queue import defer_action, drain_action


def test_recovered_handoff_remains_visible_after_submission(tmp_path):
    w = _session(tmp_path)
    action = parse_action({"id": "one", "type": "observe", "evidence": ["runs/gen-0/eval"], "hypothesis": "test"})
    defer_action(w, action)
    execute_action(w, action)
    assert session_status(w)["health"]["unresolved"][0]["kind"] == "handoff_ack_pending"
    drain_action(w)
    drain_action(w)
    execute_action(w, parse_action({"id": "finish", "type": "submit_champion", "genid": "0"}))
    state = session_status(w)
    assert state["status"] == "submitted"
    assert state["health"]["status"] == "incidents_recorded"
    assert state["health"]["incident_count"] == 1
    assert state["health"]["incidents"][0]["recovery"] == "terminal_receipt_reused"
    assert state["health"]["incidents"][0]["classification"] == "unknown"
    assert json.loads(facts_bytes(w))["health"] == state["health"]


def test_infrastructure_result_and_controller_failure_are_not_hidden(tmp_path):
    w = _session(tmp_path)
    root = w / "runs/agent-driven"
    _append_event(
        root / "actions.jsonl",
        {
            "phase": "completed",
            "action_id": "failed-evaluation",
            "seq": 1,
            "result": {"evaluation": "infrastructure_failed", "score": None},
        },
    )
    (root / "controller").mkdir()
    _append_event(
        root / "controller/attempts.jsonl",
        {
            "phase": "failed",
            "attempt": 1,
            "returncode": -9,
            "timed_out": False,
        },
    )
    health = json.loads(facts_bytes(w))["health"]
    assert health["incident_count"] == 2
    assert health["incidents"][0]["classification"] == "infrastructure"
    assert health["incidents"][1]["returncode"] == -9
    assert health["incidents"][1]["classification"] == "unknown"
    assert all((w / i["evidence"]).is_file() for i in health["incidents"])


def test_pending_controller_is_not_reported_as_clean_or_as_confirmed_crash(tmp_path):
    w = _session(tmp_path)
    root = w / "runs/agent-driven/controller"
    root.mkdir()
    _append_event(root / "attempts.jsonl", {"phase": "started", "attempt": 1})
    health = session_status(w)["health"]
    assert health["status"] == "attention_required"
    assert health["incidents"] == []
    assert health["unresolved"][0]["kind"] == "controller_outcome_pending"


def test_scoreable_timeout_reaches_host_facts_without_task_identity(tmp_path):
    from evolve.agent_observability import evaluation_execution
    from evolve.evaluation.results import EvaluationRecord, Outcome, TrialResult

    record = EvaluationRecord(
        experiment_id="test",
        generation="1",
        candidate_commit="abc",
        purpose="candidate",
        attempt=1,
        evaluator_fingerprint="a",
        task_set_hash="b",
        runtime_fingerprint="c",
        expected_trials=1,
        outcome=Outcome.BENCHMARK_COMPLETE,
        reason="scoreable timeout",
        score=0.0,
        cost_usd=1.0,
        wall_s=100,
        scoreable_trials=1,
        trials=(TrialResult("secret-heldout-name", 0, Outcome.TIMEOUT, 0.0, "benchmark_agent", "AgentTimeoutError"),),
    )
    execution = evaluation_execution(record)
    assert execution["timeouts_by_owner"] == {"benchmark_agent": 1}
    assert execution["exception_counts"] == {"AgentTimeoutError": 1}
    assert "secret-heldout-name" not in json.dumps(execution)
    w = _session(tmp_path)
    _append_event(
        w / "runs/agent-driven/actions.jsonl",
        {
            "phase": "started",
            "action_id": "timed",
            "seq": 1,
            "action": {"type": "evaluate", "genid": "1"},
        },
    )
    _append_event(
        w / "runs/agent-driven/actions.jsonl",
        {
            "phase": "completed",
            "action_id": "timed",
            "seq": 1,
            "result": {"evaluation": "benchmark_complete", "score": 0.0, "execution": execution},
        },
    )
    facts = json.loads(facts_bytes(w))
    assert facts["evaluations"]["timed"]["execution"] == execution
    assert facts["health"]["incidents"][0]["kind"] == "evaluation_trial_timeouts"
    assert facts["health"]["incidents"][0]["timeouts_by_owner"] == {"benchmark_agent": 1}
