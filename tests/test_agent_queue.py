import json
import sys
from pathlib import Path

import pytest
from conftest import research_method
from test_agent_launcher import _session

from evolve.agent_driver import execute_action, parse_action
from evolve.agent_launcher import ControllerConfig, ControllerLimits
from evolve.agent_queue import defer_action, drain_action, drive_controller, resume_controller


def observe(name: str):
    return parse_action({"id": name, "type": "observe", "evidence": ["runs/gen-0/eval"], "hypothesis": name})


def test_deferred_duplicate_and_conflicting_request(tmp_path: Path) -> None:
    w = _session(tmp_path)
    first = defer_action(w, observe("first"))
    assert defer_action(w, observe("first")) == first
    with pytest.raises(RuntimeError, match="already awaits"):
        defer_action(w, observe("second"))
    assert drain_action(w)
    assert not drain_action(w)


def test_crash_after_action_before_ack_does_not_repeat(tmp_path: Path) -> None:
    w = _session(tmp_path)
    action = observe("one")
    defer_action(w, action)
    execute_action(w, action)
    # The terminal action exists, but the queue acknowledgment was lost.
    events = w / "runs/agent-driven/actions.jsonl"
    before = events.read_bytes()
    assert drain_action(w)
    assert events.read_bytes() == before


def test_two_handoffs_progress_without_model_polling(tmp_path: Path) -> None:
    w = _session(tmp_path)
    script = tmp_path / "decide.py"
    script.write_text("""import os,json
from pathlib import Path
from evolve.agent_driver import parse_action
from evolve.agent_queue import defer_action
method=json.loads(Path(os.environ['EVOLVE_CONTROLLER_INPUT']).read_text())
Path(os.environ['EVOLVE_CONTROLLER_ATTEMPT_DIR'],'method-load.json').write_text(json.dumps(method['optimizer']))
w=Path(os.environ['EVOLVE_AGENT_WORKSPACE'])
n=int(os.environ['EVOLVE_CONTROLLER_ATTEMPT'])
if n<3:
 payload={'id':f'work-{n}','type':'observe','evidence':['runs/gen-0/eval'],'hypothesis':f'cycle{n}'}
else:
 payload={'id':'submit','type':'finish_research','reason':'completed'}
defer_action(w,parse_action(payload))
Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT']).write_text(json.dumps({'total_tokens':1,'cost_usd':0}))
""")
    config = ControllerConfig(sys.executable, (str(script),), ControllerLimits(3, max_tokens=10))
    result = drive_controller(w, config)
    assert result["status"] == "finished"
    assert result["evaluations_used"] == 0
    assert result["objective_completion"] == "not_assessed"
    events = [json.loads(x) for x in (w / "runs/agent-driven/actions.jsonl").read_text().splitlines()]
    assert [x["action_id"] for x in events if x["phase"] == "completed"] == ["work-1", "work-2", "submit"]
    assert resume_controller(w) == result


def test_controller_without_handoff_blocks_instead_of_busy_loop(tmp_path: Path) -> None:
    from test_agent_launcher import _controller

    w = _session(tmp_path)
    config = ControllerConfig(str(_controller(tmp_path / "controller")), (), ControllerLimits(4))
    with pytest.raises(RuntimeError, match="without a deferred action"):
        drive_controller(w, config)
    attempts = (w / "runs/agent-driven/controller/attempts.jsonl").read_text().splitlines()
    assert len(attempts) == 2


@pytest.mark.slow
def test_two_candidate_cycles_complete_without_chat(tmp_path: Path, monkeypatch) -> None:
    from conftest import git, init_fixture_workspace
    from test_agent_driver import _certify_baseline

    from evolve.agent_driver import AgentLimits, start_session

    w = tmp_path / "workspace"
    home = tmp_path / "evolve-home"
    init_fixture_workspace(w, "hyperagents-smoke")
    failed = json.loads((w / "evaluator/splits.json").read_text())["tasks"]["gate"][0]
    target = w / "target/agent.py"
    target.write_text(target.read_text() + f"\n# FAIL {failed}\n")
    git(w, "add", "target/agent.py")
    git(w, "commit", "-m", "baseline defect")
    git(w, "tag", "-f", "gen/0")
    _certify_baseline(w, home)
    monkeypatch.setenv("EVAL_STUB", "1")
    monkeypatch.setenv("EVOLVE_HOME", str(home))
    start_session(w, AgentLimits(30, 5, 2), optimizer=research_method(w), objective="test research")
    script = tmp_path / "decide.py"
    script.write_text("""import json,os
from pathlib import Path
from evolve.agent_driver import parse_action
from evolve.agent_queue import defer_action
method=json.loads(Path(os.environ['EVOLVE_CONTROLLER_INPUT']).read_text())
Path(os.environ['EVOLVE_CONTROLLER_ATTEMPT_DIR'],'method-load.json').write_text(json.dumps(method['optimizer']))
w=Path(os.environ['EVOLVE_AGENT_WORKSPACE'])
n=int(os.environ['EVOLVE_CONTROLLER_ATTEMPT'])-1
gen=str(n//5+1); step=n%5
if n==10:
 payload={'id':'submit','type':'finish_research','reason':'completed'}
else:
 kind=['fork','operator','commit','evaluate','finalize'][step]
 payload={'id':f'{gen}-{step}','type':kind,'genid':gen}
 if kind!='evaluate':payload['parent']='0'
 if kind=='operator':
  p=w/f'runs/worktrees/gen-{gen}/target/agent.py'
  p.write_text('\\n'.join(x for x in p.read_text().splitlines() if not x.startswith('# FAIL '))+f'\\n# candidate {gen}\\n')
  payload['stage']='validate'
defer_action(w,parse_action(payload))
Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT']).write_text(json.dumps({'total_tokens':1,'cost_usd':0}))
""")
    result = drive_controller(w, ControllerConfig(sys.executable, (str(script),), ControllerLimits(12, max_tokens=20)))
    assert result["evaluations_used"] == 2
    assert result["champion"]["genid"] == "1"
    assert result["research"]["finish"]["reason"] == "completed"
    assert (w / "runs/agent-driven/progression/status.json").exists()


def test_explicit_resolution_releases_queued_request_without_execution(tmp_path: Path) -> None:
    from evolve.agent_queue import resolve_deferred

    w = _session(tmp_path)
    defer_action(w, observe("cancelled-request"))
    resolved = resolve_deferred(w, "Operator cancelled before dispatch")
    assert resolved["status"] == "completed"
    assert not drain_action(w)
    assert (w / "runs/agent-driven/deferred-resolutions.jsonl").exists()
    defer_action(w, observe("replacement"))
    assert drain_action(w)


def test_resume_uses_saved_configuration_and_cumulative_attempts(tmp_path: Path) -> None:
    w = _session(tmp_path)
    script = tmp_path / "resume_decide.py"
    script.write_text("""import os,json
from pathlib import Path
from evolve.agent_driver import parse_action
from evolve.agent_queue import defer_action
method=json.loads(Path(os.environ['EVOLVE_CONTROLLER_INPUT']).read_text())
Path(os.environ['EVOLVE_CONTROLLER_ATTEMPT_DIR'],'method-load.json').write_text(json.dumps(method['optimizer']))
w=Path(os.environ['EVOLVE_AGENT_WORKSPACE'])
if int(os.environ['EVOLVE_CONTROLLER_ATTEMPT']) == 2:
 defer_action(w,parse_action({'id':'final','type':'finish_research','reason':'completed'}))
Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT']).write_text(json.dumps({'total_tokens':1,'cost_usd':0}))
""")
    config = ControllerConfig(sys.executable, (str(script),), ControllerLimits(2, max_tokens=10))
    with pytest.raises(RuntimeError, match="without a deferred action"):
        drive_controller(w, config)
    result = resume_controller(w)
    assert result["status"] == "finished"
    from evolve.agent_launcher import controller_status

    assert controller_status(w)["attempts_used"] == 2


@pytest.mark.parametrize(
    "limits,reason",
    [
        (ControllerLimits(1), "attempts"),
        (ControllerLimits(3, max_tokens=10), "controller_tokens"),
        (ControllerLimits(3, max_cost_usd=0.25), "controller_cost_usd"),
    ],
)
def test_budget_exhaustion_submits_best_without_queued_work(tmp_path, limits, reason):
    from evolve.agent_launcher import controller_status

    w = _session(tmp_path)
    script = tmp_path / "budget.py"
    script.write_text("""import os,json
from pathlib import Path
from evolve.agent_driver import parse_action
from evolve.agent_queue import defer_action
method=json.loads(Path(os.environ['EVOLVE_CONTROLLER_INPUT']).read_text())
Path(os.environ['EVOLVE_CONTROLLER_ATTEMPT_DIR'],'method-load.json').write_text(json.dumps(method['optimizer']))
w=Path(os.environ['EVOLVE_AGENT_WORKSPACE'])
defer_action(w,parse_action({'id':'must-not-run','type':'fork','genid':'1','parent':'0'}))
Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT']).write_text(json.dumps({'total_tokens':10,'cost_usd':.25}))
""")
    config = ControllerConfig(sys.executable, (str(script),), limits)
    result = drive_controller(w, config)
    assert result["status"] == "finished"
    assert result["research"]["finish"]["reason"] == "budget"
    assert result["champion"]["genid"] == "0"
    assert reason in result["budget_exhausted_reasons"]
    assert result["evaluations_used"] == 0
    assert not (w / "runs/worktrees/gen-1").exists()
    assert resume_controller(w) == result
    assert controller_status(w)["attempts_used"] == 1


@pytest.mark.parametrize("point", ["before", "during", "after"])
def test_real_process_death_does_not_repeat_external_effect(tmp_path, point):
    import subprocess

    from evolve.agent_driver import resolve_interrupted, session_status

    w = _session(tmp_path)
    defer_action(w, observe("kill-test"))
    script = tmp_path / "die.py"
    script.write_text("""import os,signal,sys
from pathlib import Path
import evolve.agent_driver as driver
from evolve.agent_queue import drain_action
w=Path(sys.argv[1]);point=sys.argv[2]
original=driver._dispatch
def dispatch(workspace,action,manifest):
 counter=workspace/'runs/external-counter'
 counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
 if point=='during':os.kill(os.getpid(),signal.SIGKILL)
 return original(workspace,action,manifest)
driver._dispatch=dispatch
if point=='before':os.kill(os.getpid(),signal.SIGKILL)
a=driver.parse_action({'id':'kill-test','type':'observe','evidence':['runs/gen-0/eval'],'hypothesis':'kill-test'})
driver.execute_action(w,a)
os.kill(os.getpid(),signal.SIGKILL)
""")
    proc = subprocess.run([sys.executable, str(script), str(w), point], timeout=20)
    assert proc.returncode < 0
    counter = w / "runs/external-counter"
    if point == "during":
        assert session_status(w)["status"] == "interrupted"
        assert session_status(w)["health"]["unresolved"][0]["kind"] == "action_outcome_unknown"
        with pytest.raises(RuntimeError, match="interrupted"):
            drain_action(w)
        assert counter.read_text() == "1"
        resolve_interrupted(w, "kill-test", "External counter confirms one effect; do not replay")
        with pytest.raises(RuntimeError, match="failed"):
            drain_action(w)
    else:
        assert drain_action(w)
        assert not drain_action(w)
        health = session_status(w)["health"]
        assert health["incident_count"] == (1 if point == "after" else 0)
        assert (counter.read_text() if counter.exists() else "0") == ("1" if point == "after" else "0")


def test_missing_cost_blocks_resume_without_free_work(tmp_path):
    from test_agent_launcher import _controller

    from evolve.agent_launcher import controller_status

    w = _session(tmp_path)
    script = _controller(tmp_path / "unpriced", invalid_first=True)
    config = ControllerConfig(str(script), (), ControllerLimits(3, max_cost_usd=1))
    with pytest.raises(RuntimeError, match="valid usage receipt"):
        drive_controller(w, config)
    status = controller_status(w)
    assert status["controller_cost_usd"] is None
    assert status["unpriced_controller_attempts"] == 1
    with pytest.raises(RuntimeError, match="unknown"):
        resume_controller(w)
    assert controller_status(w)["attempts_used"] == 1
