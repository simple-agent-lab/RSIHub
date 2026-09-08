import asyncio
import json

import pytest
from conftest import git
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.agent.context import AgentContext

from evolve.integrations.harbor.isolated_candidate import IsolatedCandidateAgent
from evolve.runtime.process import OwnedResult
from evolve.workspace import _runtime_kwargs

IMAGE = "sha256:" + "a" * 64


def test_host_proxy_exports_candidate_without_importing_it(tmp_path, monkeypatch):
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    repo = tmp_path / "repo"
    (repo / "target").mkdir(parents=True)
    marker = tmp_path / "host-imported"
    (repo / "target/agent.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n")
    for args in [
        ("init",),
        ("config", "user.name", "fixture"),
        ("config", "user.email", "test@example.test"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    ]:
        git(repo, *args)
    monkeypatch.setattr("evolve.integrations.harbor.isolated_candidate.resolve_image", lambda *args: IMAGE)
    monkeypatch.setattr(
        "evolve.integrations.harbor.isolated_candidate.run_sandbox",
        lambda *args, **kwargs: OwnedResult(0, "", "", 0, False),
    )
    agent = IsolatedCandidateAgent(
        logs_dir=tmp_path / "trial/agent",
        candidate_import_path="target.agent:HarborAgent",
        worker_image=IMAGE,
        extra_env={"EVOLVE_CANDIDATE_SOURCE": str(repo / "target")},
    )
    agent._start()
    try:
        assert not marker.exists()
        root = tmp_path / "trial/candidate-isolation"
        assert (root / "input/target/agent.py").read_bytes() == (repo / "target/agent.py").read_bytes()
        assert not (root / "input/.git").exists()
        config = json.loads((root / "input/agent.json").read_text())
        assert config["kwargs"]["extra_env"]["EVOLVE_CANDIDATE_SOURCE"] == "/input/target"
    finally:
        agent._shutdown()


def test_worker_reported_error_keeps_candidate_failure_classification(tmp_path, monkeypatch):
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    agent = IsolatedCandidateAgent(
        logs_dir=tmp_path / "trial/agent", candidate_import_path="target.agent:HarborAgent", worker_image=IMAGE
    )
    response = tmp_path / "response.json"
    response.write_text(json.dumps({"error": "AssertionError", "message": "candidate failed"}))
    with pytest.raises(NonZeroAgentExitCodeError, match="candidate failed"):
        agent._response(response)


def test_agent_kwargs_are_rendered_without_shell_interpretation():
    value = {
        "candidate_import_path": "target.agent:HarborAgent",
        "worker_image": IMAGE,
        "literal": "$(not-executed)\nnext",
    }
    encoded = _runtime_kwargs(value, "agent_kwargs")
    assert {name: json.loads(data) for name, data in (line.split("=", 1) for line in encoded.splitlines())} == value


@pytest.mark.parametrize("cost", [None, 0.125])
def test_reported_run_failure_preserves_partial_context_and_parser(tmp_path, monkeypatch, cost):
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    agent = IsolatedCandidateAgent(
        logs_dir=tmp_path / "trial/agent", candidate_import_path="target.agent:HarborAgent", worker_image=IMAGE
    )
    output = agent._root / "output/logs"
    output.mkdir(parents=True)
    (output / "partial.log").write_text("retained failure evidence")
    response = tmp_path / "response.json"
    response.write_text(json.dumps({"error": "RuntimeError", "message": "failed", "context": {"cost_usd": cost}}))

    async def phase(*args, result_context, **kwargs):
        return agent._response(response, result_context)

    monkeypatch.setattr(agent, "_phase", phase)
    context = AgentContext()
    with pytest.raises(NonZeroAgentExitCodeError):
        asyncio.run(agent.run("instruction", object(), context))
    assert context.cost_usd == cost
    assert agent._closed == (cost is not None)
    assert (agent.logs_dir / "partial.log").read_text() == "retained failure evidence"


def test_candidate_receipt_does_not_treat_missing_usage_as_free(tmp_path, monkeypatch):
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    agent = IsolatedCandidateAgent(
        logs_dir=tmp_path / "trial/agent", candidate_import_path="target.agent:HarborAgent", worker_image=IMAGE
    )
    agent._root.mkdir(parents=True)
    receipt = agent._root / "receipt.json"
    receipt.write_text('{"usage_status":"unknown"}')
    response = tmp_path / "response.json"
    response.write_text('{"context":{}}')
    agent._response(response)
    assert json.loads(receipt.read_text())["usage_status"] == "unknown"
    response.write_text('{"context":{"cost_usd":0}}')
    agent._response(response)
    assert json.loads(receipt.read_text())["usage_status"] == "reported"
