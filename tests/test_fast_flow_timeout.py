import json
import sys

from library._shared.harbor.execution import _run_harbor


def test_unresponsive_process_is_killed_and_evidence_retained(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLVE_OPERATOR_TIMEOUT_S", "2")
    log = tmp_path / "harbor.log"
    script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('partial evidence',flush=True);time.sleep(60)"
    code = _run_harbor([sys.executable, "-c", script], tmp_path, log, {})
    assert code == 124
    assert "partial evidence" in log.read_text()
    status = json.loads(log.with_suffix(".status.json").read_text())
    assert status["forced_termination"] is True
