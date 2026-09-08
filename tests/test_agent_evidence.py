import hashlib
import json

import pytest

from evolve.agent_evidence import verify_claims


def test_claims_reject_contradictions_stale_and_missing_evidence(tmp_path):
    source = tmp_path / "result.json"
    source.write_text(json.dumps({"candidate": "g1", "errors": 2}))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    claim = {"path": "result.json", "sha256": digest, "keys": ["errors"], "value": 2}
    assert verify_claims(tmp_path, [claim], ["result.json"])["verified_claims"] == [claim]
    for wrong in [dict(claim, value=0), dict(claim, keys=["candidate"], value="g0")]:
        with pytest.raises(RuntimeError, match="contradicts"):
            verify_claims(tmp_path, [wrong], ["result.json"])
    source.write_text('{"errors":0}')
    with pytest.raises(RuntimeError, match="digest mismatch"):
        verify_claims(tmp_path, [claim], ["result.json"])
    source.unlink()
    with pytest.raises(RuntimeError, match="missing or invalid"):
        verify_claims(tmp_path, [claim], ["result.json"])


def test_claim_cannot_escape_workspace(tmp_path):
    claim = {"path": "../outside.json", "sha256": "x", "keys": [], "value": 0}
    with pytest.raises(RuntimeError, match="escapes"):
        verify_claims(tmp_path, [claim], ["../outside.json"])


def test_conflicting_observation_blocks_deferred_progression(tmp_path):
    from test_agent_launcher import _session

    from evolve.agent_driver import parse_action
    from evolve.agent_queue import defer_action, drain_action

    w = _session(tmp_path)
    source = w / "runs/facts.json"
    source.write_text('{"errors":2}')
    claim = {
        "path": "runs/facts.json",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "keys": ["errors"],
        "value": 0,
    }
    defer_action(
        w,
        parse_action(
            {
                "id": "wrong-facts",
                "type": "observe",
                "evidence": ["runs/facts.json"],
                "hypothesis": "example",
                "claims": [claim],
            }
        ),
    )
    with pytest.raises(RuntimeError, match="contradicts"):
        drain_action(w)
    with pytest.raises(RuntimeError, match="failed"):
        drain_action(w)
    events = [json.loads(line) for line in (w / "runs/agent-driven/actions.jsonl").read_text().splitlines()]
    assert events[-1]["phase"] == "failed"
    assert not any(e["phase"] == "completed" for e in events)


def test_free_text_observation_is_explicitly_unverified(tmp_path):
    from test_agent_launcher import _session

    from evolve.agent_driver import execute_action, parse_action

    w = _session(tmp_path)
    result = execute_action(
        w,
        parse_action(
            {
                "id": "unverified",
                "type": "observe",
                "evidence": ["runs/gen-0/eval"],
                "hypothesis": "There were no errors.",
            }
        ),
    )
    assert result["result"]["prose_verified"] is False
    assert result["result"]["verified_claims"] == []


def test_session_claim_ignores_controller_file(tmp_path):
    from test_agent_launcher import _session

    from evolve.agent_evidence import facts_bytes

    w = _session(tmp_path)
    content = facts_bytes(w)
    (w / "@session").write_text('{"evaluations_used":999}')
    claim = {
        "path": "@session",
        "sha256": hashlib.sha256(content).hexdigest(),
        "keys": ["evaluations_used"],
        "value": 0,
    }
    assert verify_claims(w, [claim], ["@session"])["verified_claims"]
    claim["value"] = 999
    with pytest.raises(RuntimeError, match="contradicts"):
        verify_claims(w, [claim], ["@session"])
