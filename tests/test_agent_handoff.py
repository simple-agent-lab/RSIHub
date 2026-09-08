import json
import os
import subprocess
import sys

import pytest
from test_agent_research import research

from evolve.agent_driver import execute_action, parse_action
from evolve.agent_handoff import recover_handoff
from evolve.agent_queue import drain_action


@pytest.mark.parametrize("crash", ["1", "2", "3", "4", "queued"])
def test_process_exit_during_directory_import_rolls_forward_once(tmp_path, crash):
    workspace = research(tmp_path)
    root = workspace / "runs/agent-driven"
    attempt = root / "controller/attempt-1"
    attempt.mkdir(parents=True)
    (root / "notes/old").write_text("old notes")
    for i in range(2):
        stage = attempt / f"accepted-{i}"
        stage.mkdir()
        script = stage / "new.sh"
        script.write_text("#!/bin/sh\necho new\n")
        script.chmod(0o755)
    program = r"""
import os,sys
from pathlib import Path
from evolve import agent_handoff as h, agent_queue as q
from evolve.agent_driver import parse_action
w=Path(sys.argv[1]); point=sys.argv[2]; root=w/'runs/agent-driven'; attempt=root/'controller/attempt-1'
move=h._move
counter=0
def stop_after_move(a,b):
    global counter
    move(a,b); counter+=1
    if str(counter)==point: os._exit(91)
h._move=stop_after_move
defer=q._defer_action_locked
def stop_after_enqueue(*args,**kwargs):
    result=defer(*args,**kwargs)
    if point=='queued' and not kwargs.get('validate_only'): os._exit(91)
    return result
q._defer_action_locked=stop_after_enqueue
h.commit_handoff(w,attempt,[(root/'notes',attempt/'accepted-0'),(root/'optimizer/drafts',attempt/'accepted-1')],parse_action({'id':'publish','type':'publish_best','genid':'0'}))
"""
    result = subprocess.run([sys.executable, "-c", program, str(workspace), crash], env=os.environ.copy())
    assert result.returncode == 91
    with pytest.raises(RuntimeError, match="handoff is incomplete"):
        execute_action(workspace, parse_action({"id": "other", "type": "publish_best", "genid": "0"}))
    assert recover_handoff(workspace)
    assert not recover_handoff(workspace)
    for folder in (root / "notes", root / "optimizer/drafts"):
        assert (folder / "new.sh").read_text() == "#!/bin/sh\necho new\n"
        assert (folder / "new.sh").stat().st_mode & 0o111
    assert (attempt / "previous-0/old").read_text() == "old notes"
    assert drain_action(workspace)
    assert not drain_action(workspace)
    events = [json.loads(line) for line in (root / "actions.jsonl").read_text().splitlines()]
    assert sum(e.get("action_id") == "publish" and e["phase"] == "completed" for e in events) == 1
