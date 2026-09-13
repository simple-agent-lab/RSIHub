from __future__ import annotations

import asyncio
import importlib.util
import json
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
    env_text = env_out.read_text()
    assert f"DSH_NODE_BIN={node}" in env_text
    assert "DSH_RUNTIME_MODE=node" in env_text


def test_prepare_runtime_accepts_exe_mode_without_node(tmp_path: Path) -> None:
    env_out = tmp_path / "env"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = subprocess.run(
        ["sh", str(PREPARE), str(run_dir), str(env_out)],
        env={**os.environ, "DSH_RUNTIME_MODE": "exe", "DSH_NODE_BIN": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "DSH_RUNTIME_MODE=exe" in env_out.read_text()
    assert "DSH_NODE_BIN=" not in env_out.read_text()


def test_prepare_runtime_rejects_unknown_runtime_mode(tmp_path: Path) -> None:
    env_out = tmp_path / "env"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = subprocess.run(
        ["sh", str(PREPARE), str(run_dir), str(env_out)],
        env={**os.environ, "DSH_RUNTIME_MODE": "wasm"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unsupported DSH_RUNTIME_MODE" in result.stderr


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


def _stub_deepseek_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a tiny deepseek_harness stub so drivers import without the real SDK."""
    monkeypatch.setitem(sys.modules, "deepseek_harness", SimpleNamespace(DeepSeekHarness=object))


def test_drivers_reject_legacy_session_root_and_cordis_kwargs() -> None:
    """Regression lock: current SDK has no session_root / cordis constructor kwargs."""
    for relative in (
        "seeds/dsh/runners/rollout_driver.py",
        "seeds/dsh/runners/mutate_driver.py",
    ):
        source = (ROOT / relative).read_text()
        assert "session_root=" not in source, relative
        assert "cordis=" not in source, relative
        assert "dsh_home=" in source, relative
        assert "patches=" in source, relative


def test_seed_compositions_avoid_removed_spine_demo_package() -> None:
    """sdk-minimal does not ship dsh-agent-spine-demo; seeds must not name it."""
    for relative in (
        "seeds/dsh/profile.cordis.yml",
        "seeds/dsh/runners/compositions/mutate.cordis.yml",
        "seeds/dsh/runners/compositions/rollout.base.cordis.yml",
    ):
        text = (ROOT / relative).read_text()
        assert "dsh-agent-spine-demo" not in text, relative
        assert "agent-spine-demo" not in text, relative
    profile = (ROOT / "seeds/dsh/profile.cordis.yml").read_text()
    assert "@deepseek-ai/dsh-system-prompt" in profile
    assert "@deepseek-ai/dsh-tool-bash-persistent" in profile

    assert "system-prompt" in profile
    rollout = (ROOT / "seeds/dsh/runners/compositions/rollout.base.cordis.yml").read_text()
    assert "cordis-plugin-include" not in rollout
    assert "__CANDIDATE_PROFILE__" not in rollout


def test_rollout_cordis_docker_exec_omits_tty_flag() -> None:
    """Headless Harbor/node-pty fail with `docker exec -t` or interactive bash `-i`."""
    rollout = (ROOT / "seeds/dsh/runners/compositions/rollout.base.cordis.yml").read_text()
    assert "id: terminal-bash" in rollout
    assert "- exec" in rollout
    assert "- -i" in rollout
    # Forbid allocating a container PTY in shellArgs (comments may mention `-t`).
    assert "\n      - -t\n" not in rollout
    assert "Never pass `docker exec -t`" in rollout
    # docker exec keeps `-i`; bash must not be interactive `-i`.
    assert rollout.count("\n      - -i\n") == 1
    assert "- /bin/bash\n" in rollout
    assert "- --noprofile\n" in rollout
    assert "- --norc\n" in rollout
    assert "- --norc\n      - -i\n" not in rollout
    assert "bash -i" in rollout


def test_doctor_contract_wires_non_tty_docker_exec_probe() -> None:
    contract = json.loads((ROOT / "recipes/hyperagents_dsh/evaluator/doctor.json").read_text())
    assert contract["smoke"]["command"] == ["sh", "evaluator/doctor_pty_probe.sh"]
    probe = (ROOT / "recipes/hyperagents_dsh/evaluator/doctor_pty_probe.sh").read_text()
    assert "docker exec -t" not in probe
    assert "exec -i" in probe
    assert "/bin/bash --noprofile --norc" in probe
    # Comments may mention `/bin/sh -c` as the false-green path; code must not use it.
    code = "\n".join(line for line in probe.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    assert "/bin/sh -c" not in code
    assert "failing closed" in probe
    assert "script" in probe
    assert "echo ok" in probe


def test_doctor_pty_probe_uses_non_tty_exec_with_fake_docker(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            set -eu
            log="${DOCTOR_PTY_FAKE_LOG:?}"
            printf '%s\\n' "$*" >> "$log"
            case "$1" in
              image)
                # Pretend bash image is cached so the probe skips fallbacks/pull.
                exit 0
                ;;
              run)
                printf 'fake-cid\\n'
                ;;
              exec)
                # Refuse if a TTY was requested.
                for arg in "$@"; do
                  if [ "$arg" = "-t" ] || [ "$arg" = "-it" ] || [ "$arg" = "-ti" ]; then
                    echo "PTY requested" >&2
                    exit 2
                  fi
                done
                # Cordis-aligned argv: bash --noprofile --norc (not sh -c, not bash -i).
                printf '%s\\n' "$*" | grep -q '/bin/bash' || {
                  echo "expected /bin/bash" >&2
                  exit 3
                }
                printf '%s\\n' "$*" | grep -q -- '--noprofile' || {
                  echo "expected --noprofile" >&2
                  exit 3
                }
                printf '%s\\n' "$*" | grep -q -- '--norc' || {
                  echo "expected --norc" >&2
                  exit 3
                }
                printf '%s\\n' "$*" | grep -q -- '/bin/sh' && {
                  echo "sh -c path is a false green" >&2
                  exit 3
                }
                # Final argv token must not be interactive -i (docker -i appears earlier).
                last=
                for arg in "$@"; do
                  last=$arg
                done
                if [ "$last" = "-i" ]; then
                  echo "interactive bash -i requested" >&2
                  exit 3
                fi
                # Drain stdin (probe feeds 'echo ok') then print the marker.
                cat >/dev/null
                printf 'ok\\n'
                ;;
              rm)
                exit 0
                ;;
              *)
                echo "unexpected docker invocation: $*" >&2
                exit 1
                ;;
            esac
            """
        )
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["sh", str(ROOT / "recipes/hyperagents_dsh/evaluator/doctor_pty_probe.sh")],
        env={
            **os.environ,
            "DSH_DOCKER_BIN": str(docker),
            "DOCTOR_PTY_FAKE_LOG": str(log),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "non-TTY docker exec ok" in result.stdout
    assert "host-PTY docker exec ok" in result.stdout
    logged = log.read_text()
    assert "exec -i" in logged
    assert " -t " not in f" {logged} "
    assert "/bin/bash" in logged
    assert "--noprofile" in logged
    assert "--norc" in logged
    assert "/bin/sh -c" not in logged
    assert logged.count("/bin/bash") >= 2


def test_materialize_candidate_overlay_rewrites_relative_plugins(tmp_path: Path) -> None:
    helper = _load_module(
        "candidate_overlay_under_test",
        ROOT / "seeds" / "dsh" / "runners" / "candidate_overlay.py",
    )
    candidate = tmp_path / "candidate"
    (candidate / "plugins").mkdir(parents=True)
    (candidate / "plugins" / "seed-probe.mjs").write_text("export const name = 'x'\n")
    (candidate / "profile.cordis.yml").write_text(
        "- id: system-prompt\n"
        "  config:\n"
        "    personaPrefix: hi\n"
        "- insert:\n"
        "    - id: plugin-seed-probe\n"
        "      name: './plugins/seed-probe.mjs'\n"
    )
    dsh_home = tmp_path / "dsh-home"
    overlay = helper.materialize_candidate_overlay(candidate, dsh_home)
    assert overlay == dsh_home / "candidate.overlay.cordis.yml"
    text = overlay.read_text()
    assert str((candidate / "plugins" / "seed-probe.mjs").resolve()) in text
    assert "./plugins/seed-probe.mjs" not in text


def test_rollout_driver_passes_dsh_home_and_materialized_candidate_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_deepseek_harness(monkeypatch)
    module = _load_module("rollout_driver_under_test", ROOT / "seeds" / "dsh" / "runners" / "rollout_driver.py")
    candidate = tmp_path / "candidate"
    (candidate / "plugins").mkdir(parents=True)
    (candidate / "plugins" / "seed-probe.mjs").write_text("export const name = 'x'\n")
    (candidate / "profile.cordis.yml").write_text(
        "- id: system-prompt\n"
        "  config:\n"
        "    personaPrefix: seed\n"
        "- insert:\n"
        "    - id: plugin-seed-probe\n"
        "      name: './plugins/seed-probe.mjs'\n"
    )
    harbor = tmp_path / "rollout.base.cordis.yml"
    harbor.write_text("- id: terminal-bash\n  config: {}\n")
    dsh_home = tmp_path / "dsh-home"
    task = tmp_path / "task.txt"
    task.write_text("solve it\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class FakeHarness:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def run(self, instruction, session_id=None):
            captured["instruction"] = instruction
            captured["session_id"] = session_id
            return SimpleNamespace(final_response="done")

    monkeypatch.setattr(module, "DeepSeekHarness", FakeHarness)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout="/app\n"),
    )
    monkeypatch.setenv("DSH_CONTAINER", "cid")
    monkeypatch.setenv("DSH_CANDIDATE_DIR", str(candidate))
    monkeypatch.setenv("DSH_ROLLOUT_CORDIS", str(harbor))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(dsh_home))
    monkeypatch.setenv("DSH_TASK_FILE", str(task))
    monkeypatch.setenv("DSH_HOST_WORKSPACE", str(workspace))
    monkeypatch.setenv("DSH_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DSH_SESSION_ID", "trial1")
    monkeypatch.setenv("DSH_FINAL_RESPONSE", str(tmp_path / "final.txt"))

    assert module.main() == 0
    assert captured["dsh_home"] == str(dsh_home)
    assert captured["profile"] == "sdk-minimal"
    assert "session_root" not in captured
    assert "cordis" not in captured
    patches = captured["patches"]
    assert isinstance(patches, tuple) and len(patches) == 2
    assert Path(str(patches[0])) == harbor
    candidate_overlay = Path(str(patches[1]))
    assert candidate_overlay == dsh_home / "candidate.overlay.cordis.yml"
    assert candidate_overlay.is_file()
    assert str((candidate / "plugins" / "seed-probe.mjs").resolve()) in candidate_overlay.read_text()
    assert (tmp_path / "final.txt").read_text() == "done"


def test_mutate_driver_copies_overlay_under_dsh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_deepseek_harness(monkeypatch)
    module = _load_module("mutate_driver_under_test", ROOT / "seeds" / "dsh" / "runners" / "mutate_driver.py")
    dsh_home = tmp_path / "mutate-home"
    mutate_cwd = tmp_path / "target"
    mutate_cwd.mkdir()
    patch = tmp_path / "mutate.cordis.yml"
    patch.write_text("- id: system-prompt\n  config:\n    personaPrefix: meta\n")
    task = tmp_path / "prompt.txt"
    task.write_text("improve yourself\n")
    captured: dict[str, object] = {}

    class FakeHarness:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def run(self, prompt, session_id=None):
            captured["prompt"] = prompt
            captured["session_id"] = session_id
            return SimpleNamespace(final_response="mutated")

    monkeypatch.setattr(module, "DeepSeekHarness", FakeHarness)
    monkeypatch.setenv("DSH_TASK_FILE", str(task))
    monkeypatch.setenv("DSH_SESSION_ROOT", str(dsh_home))
    monkeypatch.setenv("DSH_MUTATE_CWD", str(mutate_cwd))
    monkeypatch.setenv("DSH_MUTATE_CORDIS", str(patch))
    monkeypatch.setenv("DSH_SESSION_ID", "mutate1")
    monkeypatch.setenv("DSH_FINAL_RESPONSE", str(tmp_path / "final.txt"))

    assert module.main() == 0
    assert captured["dsh_home"] == str(dsh_home)
    assert captured["cwd"] == str(mutate_cwd)
    patches = captured["patches"]
    assert isinstance(patches, tuple) and len(patches) == 1
    overlay = Path(str(patches[0]))
    assert overlay == dsh_home / "mutate.overlay.cordis.yml"
    assert overlay.is_file()
    assert "personaPrefix: meta" in overlay.read_text()
    assert "session_root" not in captured
    assert "cordis" not in captured
    assert (tmp_path / "final.txt").read_text() == "mutated"


def test_convert_session_reads_logs_under_dsh_home_sessions(tmp_path: Path) -> None:
    module = _load_module("dsh_trajectory_under_test", ROOT / "seeds" / "dsh" / "dsh_trajectory.py")
    home = tmp_path / "dsh-home"
    sessions = home / "sessions" / "trial1"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        '{"type":"user/message","data":{"content":[{"type":"text","text":"hello"}]}}\n'
        '{"type":"assistant/message","data":{"message":{"content":[{"type":"text","text":"hi"}]}}}\n'
    )
    out = tmp_path / "trajectory.json"
    module.convert_session(home, out)
    payload = out.read_text()
    assert "hello" in payload
    assert "hi" in payload
    assert '"agent": "dsh"' in payload or '"agent":"dsh"' in payload


def test_materialize_candidate_overlay_handles_quote_styles_and_non_plugins(tmp_path: Path) -> None:
    helper = _load_module(
        "candidate_overlay_quotes",
        ROOT / "seeds" / "dsh" / "runners" / "candidate_overlay.py",
    )
    candidate = tmp_path / "candidate"
    (candidate / "plugins").mkdir(parents=True)
    (candidate / "plugins" / "seed-probe.mjs").write_text("export const name = 'x'\n")
    (candidate / "profile.cordis.yml").write_text(
        "- id: system-prompt\n"
        "  name: '@deepseek-ai/dsh-system-prompt'\n"
        "  config:\n"
        "    personaPrefix: hi\n"
        "- insert:\n"
        "    - id: plugin-seed-probe\n"
        '      name: "./plugins/seed-probe.mjs"\n'
        "    - id: plugin-unquoted\n"
        "      name: ./plugins/seed-probe.mjs\n"
    )
    overlay = helper.materialize_candidate_overlay(candidate, tmp_path / "dsh-home")
    text = overlay.read_text()
    absolute = str((candidate / "plugins" / "seed-probe.mjs").resolve())
    assert absolute in text
    assert "./plugins/seed-probe.mjs" not in text
    assert "@deepseek-ai/dsh-system-prompt" in text


def test_runtime_mode_prefers_exe_and_errors_clearly_for_missing_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module("runtime_mode_under_test", ROOT / "seeds" / "dsh" / "runners" / "runtime_mode.py")
    monkeypatch.delenv("DSH_RUNTIME_MODE", raising=False)
    monkeypatch.setattr(module, "_runtime_package_importable", lambda: True)
    monkeypatch.setattr(module, "_try_resolve", lambda mode: ("/tmp/fake-dsh-exe",) if mode == "exe" else None)
    assert module.ensure_runtime_mode() == "exe"
    assert os.environ["DSH_RUNTIME_MODE"] == "exe"

    monkeypatch.setenv("DSH_RUNTIME_MODE", "node")
    monkeypatch.setattr(module, "_runtime_package_importable", lambda: True)
    monkeypatch.setattr(module, "_try_resolve", lambda mode: None)
    with pytest.raises(RuntimeError, match="pnpm exec tsx"):
        module.ensure_runtime_mode()


def test_convert_session_extracts_usage_from_assistant_message(tmp_path: Path) -> None:
    module = _load_module("dsh_trajectory_usage", ROOT / "seeds" / "dsh" / "dsh_trajectory.py")
    home = tmp_path / "dsh-home"
    sessions = home / "sessions" / "trial1"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        '{"type":"user/message","data":{"content":[{"type":"text","text":"hello"}]}}\n'
        '{"type":"assistant/message","data":{"message":{"content":[{"type":"text","text":"hi"}]},'
        '"usage":{"inputTokens":11,"outputTokens":7,"cacheReadTokens":3}}}\n'
    )
    out = tmp_path / "trajectory.json"
    module.convert_session(home, out)
    payload = __import__("json").loads(out.read_text())
    assert payload["usage"]["schema"] == "dsh-usage-v1"
    assert payload["usage"]["totals"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 3,
    }
    agent_steps = [s for s in payload["steps"] if s.get("source") == "agent"]
    assert agent_steps[0]["usage"]["input_tokens"] == 11


def test_convert_session_omits_invented_usage_when_logs_lack_metering(tmp_path: Path) -> None:
    module = _load_module("dsh_trajectory_no_usage", ROOT / "seeds" / "dsh" / "dsh_trajectory.py")
    home = tmp_path / "dsh-home"
    sessions = home / "sessions" / "trial1"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(
        '{"type":"user/message","data":{"content":[{"type":"text","text":"hello"}]}}\n'
        '{"type":"assistant/message","data":{"message":{"content":[{"type":"text","text":"hi"}]}}}\n'
    )
    out = tmp_path / "trajectory.json"
    module.convert_session(home, out)
    payload = __import__("json").loads(out.read_text())
    assert payload["usage"]["totals"] is None
