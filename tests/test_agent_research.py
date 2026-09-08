import json
import sys

import pytest
from conftest import init_workspace, run_evolve

from evolve.agent_driver import AgentLimits, execute_action, parse_action, session_status, start_session
from evolve.agent_launcher import ControllerConfig, ControllerLimits, controller_status
from evolve.agent_optimizer import freeze_optimizer
from evolve.agent_queue import defer_action, drain_action, drive_controller, resume_controller


def research(tmp_path):
    w, home = init_workspace(tmp_path)
    result = run_evolve("eval", str(w), "0", env={"EVAL_STUB": "1", "EVOLVE_HOME": str(home)})
    assert result.returncode == 0, result.stderr
    method = tmp_path / "method"
    method.mkdir()
    (method / "instructions.md").write_text("first method")
    start_session(w, AgentLimits(30, 3, 3, max_cost_usd=10), mode="continuous", optimizer=method, objective="learn")
    return w


def act(w, name, kind, **args):
    return execute_action(w, parse_action({"id": name, "type": kind, **args}))


def test_method_adoption_replay_and_notes_survive(tmp_path):
    w = research(tmp_path)
    root = w / "runs/agent-driven"
    (root / "notes/experiment.json").write_text('{"failed":true}')
    old = session_status(w)["research"]["active_optimizer"]
    draft = root / "optimizer/drafts/new"
    draft.mkdir()
    (draft / "instructions.md").write_text("second method")
    action = parse_action(
        {
            "id": "adopt",
            "type": "adopt_optimizer",
            "draft_path": str(draft.relative_to(w)),
            "expected_digest": old["digest"],
            "reason": "learned",
        }
    )
    defer_action(w, action)
    execute_action(w, action)
    events = (root / "actions.jsonl").read_bytes()
    assert drain_action(w)
    assert (root / "actions.jsonl").read_bytes() == events
    new = session_status(w)["research"]["active_optimizer"]
    assert new["digest"] != old["digest"]
    act(w, "rollback", "adopt_optimizer", digest=old["digest"], expected_digest=new["digest"], reason="trial failed")
    assert session_status(w)["research"]["active_optimizer"]["activation_id"] == "rollback"
    assert json.loads((root / "notes/experiment.json").read_text()) == {"failed": True}


def test_publish_pause_resume_and_finish(tmp_path):
    w = research(tmp_path)
    act(w, "publish", "publish_best", genid="0")
    assert session_status(w)["status"] == "active"
    act(w, "pause", "pause_research", reason="dependency", resume_condition="available")
    assert session_status(w)["status"] == "paused"
    with pytest.raises(RuntimeError, match="paused"):
        act(w, "bad", "observe", evidence=["x"], hypothesis="x")
    act(w, "resume", "resume_research", reason="dependency available")
    act(w, "finish", "finish_research", reason="complete")
    assert session_status(w)["status"] == "finished"
    assert not (w / "runs/agent-driven/ACTIVE").exists()


def test_controller_loads_new_method_and_keeps_accounting(tmp_path):
    w = research(tmp_path)
    script = tmp_path / "controller.py"
    script.write_text("""import json,os
from pathlib import Path
from evolve.agent_driver import parse_action
from evolve.agent_queue import defer_action
w=Path(os.environ['EVOLVE_AGENT_WORKSPACE']); n=int(os.environ['EVOLVE_CONTROLLER_ATTEMPT'])
i=json.loads(Path(os.environ['EVOLVE_CONTROLLER_INPUT']).read_text()); method=i['optimizer']
a=Path(os.environ['EVOLVE_CONTROLLER_ATTEMPT_DIR'])
a.joinpath('method-load.json').write_text(json.dumps(method))
text=Path(i['optimizer_path'],'instructions.md').read_text()
a.joinpath('actual-method.txt').write_text(text)
if text=='first method':
 d=w/'runs/agent-driven/optimizer/drafts/new'; d.mkdir()
 d.joinpath('instructions.md').write_text('second method')
 payload={'id':'adopt','type':'adopt_optimizer','draft_path':str(d.relative_to(w)),'expected_digest':method['digest'],'reason':'new method'}
elif n==2:
 payload={'id':'publish','type':'publish_best','genid':'0'}
else:
 payload={'id':'finish','type':'finish_research','reason':'complete'}
defer_action(w,parse_action(payload))
Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT']).write_text(json.dumps({'total_tokens':1,'cost_usd':0.25}))
""")
    config = ControllerConfig(sys.executable, (str(script),), ControllerLimits(5, max_total_cost_usd=5))
    result = drive_controller(w, config)
    assert result["status"] == "finished"
    root = w / "runs/agent-driven/controller"
    assert (root / "attempt-1/actual-method.txt").read_text() == "first method"
    assert (root / "attempt-2/actual-method.txt").read_text() == "second method"
    assert (root / "attempt-3/actual-method.txt").read_text() == "second method"
    assert controller_status(w)["controller_cost_usd"] == 0.75
    assert resume_controller(w) == result


def test_optimizer_rejects_escape_symlink_and_bad_python(tmp_path):
    method = tmp_path / "method"
    method.mkdir()
    (method / "instructions.md").write_text("test")
    (method / "leak").symlink_to("/etc/passwd")
    with pytest.raises(RuntimeError, match="symlink"):
        freeze_optimizer(tmp_path, method)
    (method / "leak").unlink()
    (method / "tool.py").write_text("def !!!")
    with pytest.raises(RuntimeError, match="Python"):
        freeze_optimizer(tmp_path, method)


def test_mode_compatibility_and_snapshot_tampering(tmp_path):
    w = research(tmp_path)
    with pytest.raises(RuntimeError, match="publish_best"):
        act(w, "old", "submit_champion", genid="0")
    state = session_status(w)
    method = state["research"]["active_optimizer"]
    root = w / "runs/agent-driven/optimizer/versions" / method["digest"]
    (root / "instructions.md").write_text("tampered")
    from evolve.agent_optimizer import controller_input

    with pytest.raises(RuntimeError, match="modified"):
        controller_input(w, state, tmp_path)


@pytest.mark.parametrize("malformed", [False, True])
def test_parse_refusal_is_corrected_without_resetting_budget(tmp_path, malformed):
    w = research(tmp_path)
    script = tmp_path / "correct.py"
    script.write_text(
        """import json,os,subprocess,sys
from pathlib import Path
from evolve.agent_driver import parse_action
from evolve.agent_queue import defer_action
w=Path(os.environ['EVOLVE_AGENT_WORKSPACE']); n=int(os.environ['EVOLVE_CONTROLLER_ATTEMPT'])
i=json.loads(Path(os.environ['EVOLVE_CONTROLLER_INPUT']).read_text())
a=Path(os.environ['EVOLVE_CONTROLLER_ATTEMPT_DIR'])
a.joinpath('method-load.json').write_text(json.dumps(i['optimizer']))
if n==1:
 bad={'id':'bad','type':'observe','evidence':['x'],'hypothesis':'x'*2001}
 r=subprocess.run([str(Path(sys.executable).with_name('evolve')),'agent','act',str(w),'--action','{' if MALFORMED else json.dumps(bad)])
 assert r.returncode!=0
else:
 assert (a.parent/'correction.json').is_file()
 defer_action(w,parse_action({'id':'end','type':'finish_research','reason':'corrected'}))
Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT']).write_text(json.dumps({'total_tokens':1,'cost_usd':0.25}))
""".replace("MALFORMED", str(malformed))
    )
    result = drive_controller(w, ControllerConfig(sys.executable, (str(script),), ControllerLimits(5)))
    assert result["status"] == "finished"
    assert controller_status(w)["controller_cost_usd"] == 0.5
    events = (w / "runs/agent-driven/actions.jsonl").read_text()
    assert '"action_id": "bad"' not in events


def test_continuous_budget_closes_without_more_model_calls(tmp_path):
    w = research(tmp_path)
    script = tmp_path / "publish.py"
    script.write_text("""import json,os
from pathlib import Path
from evolve.agent_driver import parse_action
from evolve.agent_queue import defer_action
w=Path(os.environ['EVOLVE_AGENT_WORKSPACE'])
i=json.loads(Path(os.environ['EVOLVE_CONTROLLER_INPUT']).read_text())
a=Path(os.environ['EVOLVE_CONTROLLER_ATTEMPT_DIR'])
a.joinpath('method-load.json').write_text(json.dumps(i['optimizer']))
defer_action(w,parse_action({'id':'pub','type':'publish_best','genid':'0'}))
Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT']).write_text(json.dumps({'total_tokens':1,'cost_usd':0.25}))
""")
    result = drive_controller(w, ControllerConfig(sys.executable, (str(script),), ControllerLimits(1)))
    assert result["status"] == "finished"
    assert result["research"]["finish"]["reason"] == "budget"
    assert controller_status(w)["attempts_used"] == 1


def test_unpriced_isolated_candidate_prevents_new_actions_and_model_calls(tmp_path):
    w = research(tmp_path)
    receipt = w / "runs/native-trial/candidate-isolation/receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"status": "stopped", "usage_status": "unknown"}))
    state = session_status(w)
    assert "unpriced_candidate_cost" in state["exhausted_reasons"]
    assert state["health"]["unresolved"][0]["kind"] == "candidate_cost_unknown"
    with pytest.raises(RuntimeError, match="unpriced_candidate_cost"):
        act(w, "no-new-work", "fork", parent="0", genid="1")
    with pytest.raises(RuntimeError, match="unpriced_candidate_cost"):
        drive_controller(w, ControllerConfig("must-not-be-executed", (), ControllerLimits(3)))
    assert controller_status(w)["attempts_used"] == 0
    assert controller_status(w)["total_observed_cost_usd"] is None
