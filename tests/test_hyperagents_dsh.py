from __future__ import annotations

import asyncio
import importlib.util
import os
import random
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolve.frozen.interfaces import OperatorContext

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "recipes" / "hyperagents_dsh" / "evaluator" / "prepare-runtime.sh"
NODE_CHECK = ROOT / "library" / "validate" / "node_check.py"
MUTATE_LOCAL = ROOT / "seeds" / "dsh" / "runners" / "mutate_local.py"
DSH_AGENT = ROOT / "seeds" / "dsh" / "agent.py"


def _fake_node(bin_dir: Path, version: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    node = bin_dir / "node"
    node.write_text(f"#!/bin/sh\necho 'v{version}'\n")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    return node


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_runtime_rejects_node_below_22_19(tmp_path: Path) -> None:
    node = _fake_node(tmp_path / "bin", "22.18.0")
    env_out = tmp_path / "env"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = subprocess.run(
        ["sh", str(PREPARE), str(run_dir), str(env_out)],
        env={**os.environ, "DSH_NODE_BIN": str(node)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "too old" in result.stderr
    assert "22.19" in result.stderr


def test_prepare_runtime_accepts_node_22_19(tmp_path: Path) -> None:
    node = _fake_node(tmp_path / "bin", "22.19.0")
    env_out = tmp_path / "env"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = subprocess.run(
        ["sh", str(PREPARE), str(run_dir), str(env_out)],
        env={**os.environ, "DSH_NODE_BIN": str(node)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"DSH_NODE_BIN={node}" in env_out.read_text()


def test_node_check_rejects_unreadable_profile(tmp_path: Path) -> None:
    module = _load_module("node_check_under_test", NODE_CHECK)
    checkout = tmp_path / "checkout"
    target = checkout / "target"
    target.mkdir(parents=True)
    (target / "profile.cordis.yml").write_bytes(b"\xff\xfe not utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = OperatorContext(
        workspace=checkout,
        checkout=checkout,
        run_dir=run_dir,
        genid="1",
        parent="0",
        round=None,
        fan_out=1,
        config={},
        rng=random.Random(0),
    )
    result = module.NodeCheckValidate().validate(checkout, ctx)
    assert result.accept is False
    assert "profile.cordis.yml" in result.reason


def test_mutate_local_propagates_driver_failure_when_target_unchanged(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    runners = checkout / "target" / "runners"
    runners.mkdir(parents=True)
    (checkout / "target" / "profile.cordis.yml").write_text("id: seed\n")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("improve yourself\n")
    (runners / "mutate_driver.py").write_text("raise SystemExit(7)\n")
    mutate_local = runners / "mutate_local.py"
    mutate_local.write_text(MUTATE_LOCAL.read_text())

    subprocess.run(["git", "init"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "add", "target"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "seed"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )

    fake_pkg = tmp_path / "site"
    fake_pkg.mkdir()
    (fake_pkg / "deepseek_harness.py").write_text("pass\n")
    workspace = tmp_path / "workspace"
    (workspace / ".venv" / "bin").mkdir(parents=True)
    shim = workspace / ".venv" / "bin" / "python"
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            export PYTHONPATH="{fake_pkg}${{PYTHONPATH:+:$PYTHONPATH}}"
            exec "{sys.executable}" "$@"
            """
        )
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [sys.executable, str(mutate_local)],
        cwd=checkout,
        env={
            **os.environ,
            "EVOLVE_PROMPT_FILE": str(prompt),
            "EVOLVE_RUN_DIR": str(tmp_path / "run"),
            "EVOLVE_WORKSPACE": str(workspace),
            "DSH_META_ATTEMPTS": "2",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7, result.stderr
    assert "no target/ changes" in result.stderr


def _make_dsh_agent(tmp_path: Path, *, timeout_sec: str = "0.05"):
    module = _load_module("dsh_agent_under_test", DSH_AGENT)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "profile.cordis.yml").write_text("id: seed\n")
    runners = candidate / "runners"
    (runners / "compositions").mkdir(parents=True)
    (runners / "compositions" / "rollout.base.cordis.yml").write_text("id: rollout\n")
    (runners / "rollout_driver.py").write_text("raise SystemExit(0)\n")
    logs = tmp_path / "logs"
    agent = module.DshAgent(
        logs_dir=logs,
        model_name="test/deepseek-v4-flash",
        extra_env={
            "EVOLVE_CANDIDATE_SOURCE": str(candidate),
            "DSH_TASK_TIMEOUT_SEC": timeout_sec,
        },
    )
    agent.session_id = "trial/1"
    return module, agent, candidate


def test_dsh_agent_timeout_raises_runtime_error_and_writes_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, agent, candidate = _make_dsh_agent(tmp_path)
    written: list[Path] = []
    proc_holder: dict[str, object] = {}

    class FakeProc:
        pid = 4242

        def __init__(self) -> None:
            self.killed = False

        async def wait(self) -> int:
            while not self.killed:
                await asyncio.sleep(0.01)
            return -9

    async def fake_create(*_args, **_kwargs):
        proc = FakeProc()
        proc_holder["proc"] = proc
        return proc

    async def fake_container_id(_environment) -> str:
        return "cid"

    def fake_killpg(_pid: int, _sig: int) -> None:
        proc = proc_holder.get("proc")
        assert isinstance(proc, FakeProc)
        proc.killed = True

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(module.os, "killpg", fake_killpg)
    monkeypatch.setattr(agent, "_container_id", fake_container_id)
    monkeypatch.setattr(agent, "_runners_dir", lambda: candidate / "runners")
    monkeypatch.setattr(
        agent,
        "_write_trajectory",
        lambda logs: written.append(logs),
    )

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(agent.run("do the task", SimpleNamespace(), SimpleNamespace()))

    assert written == [agent.logs_dir]


def test_dsh_agent_nonzero_driver_exit_raises_after_trajectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, agent, candidate = _make_dsh_agent(tmp_path, timeout_sec="30")
    written: list[Path] = []

    class FakeProc:
        pid = 4242

        async def wait(self) -> int:
            return 9

    async def fake_create(*_args, **_kwargs):
        return FakeProc()

    async def fake_container_id(_environment) -> str:
        return "cid"

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(agent, "_container_id", fake_container_id)
    monkeypatch.setattr(agent, "_runners_dir", lambda: candidate / "runners")
    monkeypatch.setattr(
        agent,
        "_write_trajectory",
        lambda logs: written.append(logs),
    )

    with pytest.raises(RuntimeError, match="exited 9"):
        asyncio.run(agent.run("do the task", SimpleNamespace(), SimpleNamespace()))

    assert written == [agent.logs_dir]
