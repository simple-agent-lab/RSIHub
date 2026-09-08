import json

import pytest
from test_agent_research import research

from evolve.agent_launcher import ControllerConfig, ControllerLimits, controller_status, launch_controller
from evolve.agent_queue import drive_controller, resume_controller
from evolve.runtime.process import OwnedResult
from evolve.runtime.sandbox import SandboxConfig

IMAGE = "sha256:" + "a" * 64


def test_isolated_transport_loads_adoption_preserves_notes_and_continues_after_publish(tmp_path, monkeypatch):
    workspace = research(tmp_path)
    seen = []
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    monkeypatch.setattr("evolve.agent_isolation.resolve_image", lambda *args: IMAGE)

    def controller(config, *, image_id, command, inputs, output):
        data = json.loads((inputs / "controller-input.json").read_text())
        seen.append((inputs / "optimizer/instructions.md").read_text())
        assert not (inputs / "evaluator").exists()
        assert not (inputs / ".git").exists()
        assert (inputs / "champion").is_dir()
        (output / "method-load.json").write_text(json.dumps(data["optimizer"]))
        (output / "usage.json").write_text(json.dumps({"total_tokens": 1, "cost_usd": 0.25}))
        if len(seen) == 1:
            (output / "notes/retained.txt").write_text("experience")
            (output / "drafts/next").mkdir()
            (output / "drafts/next/instructions.md").write_text("second method")
            request = {
                "id": "adopt",
                "type": "adopt_optimizer",
                "draft_path": "runs/agent-driven/optimizer/drafts/next",
                "expected_digest": data["optimizer"]["digest"],
                "reason": "new method",
            }
        elif len(seen) == 2:
            assert (output / "notes/retained.txt").read_text() == "experience"
            request = {"id": "publish", "type": "publish_best", "genid": "0"}
        else:
            assert (output / "notes/retained.txt").read_text() == "experience"
            request = {"id": "finish", "type": "finish_research", "reason": "after publication"}
        (output / "action.json").write_text(json.dumps(request))
        return OwnedResult(0, "", "", 0.1, False)

    monkeypatch.setattr("evolve.agent_isolation.run_sandbox", controller)
    config = ControllerConfig(
        "python3", ("/input/optimizer/controller.py",), ControllerLimits(5), SandboxConfig(IMAGE, 30)
    )
    assert drive_controller(workspace, config)["status"] == "finished"
    assert seen == ["first method", "second method", "second method"]
    assert controller_status(workspace)["controller_cost_usd"] == 0
    assert controller_status(workspace)["controller_tokens"] == 0
    assert resume_controller(workspace)["status"] == "finished"


def test_isolated_symlink_receipt_is_not_read_as_host_data(tmp_path, monkeypatch):
    workspace = research(tmp_path)
    secret = tmp_path / "private"
    secret.write_text('{"total_tokens":0,"cost_usd":0}')
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    monkeypatch.setattr("evolve.agent_isolation.resolve_image", lambda *args: IMAGE)

    def controller(config, *, image_id, command, inputs, output):
        (output / "method-load.json").symlink_to(secret)
        return OwnedResult(0, "", "", 0, False)

    monkeypatch.setattr("evolve.agent_isolation.run_sandbox", controller)
    config = ControllerConfig("true", (), ControllerLimits(2), SandboxConfig(IMAGE, 30))
    with pytest.raises(OSError):
        launch_controller(workspace, config)
    assert not (workspace / "runs/agent-driven/controller/attempt-1/method-load.json").exists()
    assert controller_status(workspace)["pending_attempt"] == 1
    with pytest.raises(RuntimeError, match="reconcile usage"):
        drive_controller(workspace, config)


def test_isolated_controller_requires_pinned_image(monkeypatch):
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    config = ControllerConfig("true", (), ControllerLimits(2), SandboxConfig("mutable-tag", 30))
    with pytest.raises(RuntimeError, match="immutable image"):
        config.validate()
    config = ControllerConfig("true", (), ControllerLimits(2), SandboxConfig(IMAGE, 30))
    config.validate()
