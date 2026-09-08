import asyncio
import importlib.util
import json
import os
import random
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.models.exec.config import ExecConfig
from harbor.models.task.config import EnvironmentConfig, VerifierConfig
from harbor.models.trial.result import ExceptionInfo
from harbor.trial.errors import AgentTimeoutError
from harbor.trial.queue import TrialQueue
from harbor.trial.trial import Trial

from evolve.agent import AgentCommandError
from evolve.frozen.interfaces import OperatorContext
from evolve.operators import _operator_deadline_s

ROOT = Path(__file__).resolve().parents[1]
INSTALLED_AGENT = "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent"
LEGACY_INSTALLED_AGENT = "evolve.integrations.harbor.miniswe_task_file:FileTaskMiniSweAgent"
CANDIDATE_AGENT = "evolve.integrations.harbor.miniswe_candidate:CandidateMiniSweAgent"
LEGACY_CANDIDATE_AGENT = "evolve.integrations.harbor.miniswe_candidate:MiniSweSourceAgent"


def _harbor_runner_module():
    spec = importlib.util.spec_from_file_location(
        "harbor_mutate_runner_under_test",
        ROOT / "library" / "_shared" / "runners" / "harbor.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_mutate_always_uses_host_auth_json(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _harbor_runner_module()
    monkeypatch.delenv("CODEX_FORCE_AUTH_JSON", raising=False)
    for override, lower, upper in module._PROXY_ENV:
        monkeypatch.delenv(override, raising=False)
        monkeypatch.delenv(lower, raising=False)
        monkeypatch.delenv(upper, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "judge-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://judge-bridge.example/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "http://judge-bridge.example/v1")

    env = module._agent_env({"agent": "codex"})
    assert env == {"CODEX_FORCE_AUTH_JSON": "${CODEX_FORCE_AUTH_JSON:-1}"}
    assert module._agent_env(
        {
            "agent": "codex",
            "agent_env": {"OPENAI_BASE_URL": "https://configured.example/v1"},
        }
    ) == {
        "CODEX_FORCE_AUTH_JSON": "${CODEX_FORCE_AUTH_JSON:-1}",
        "OPENAI_BASE_URL": "https://configured.example/v1",
        "no_proxy": "configured.example",
        "NO_PROXY": "configured.example",
    }
    assert "CODEX_FORCE_AUTH_JSON" not in module._agent_env({"agent": "mini-swe-agent"})


def test_harbor_mutate_forwards_openai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _harbor_runner_module()
    for override, lower, upper in module._PROXY_ENV:
        monkeypatch.delenv(override, raising=False)
        monkeypatch.delenv(lower, raising=False)
        monkeypatch.delenv(upper, raising=False)
    monkeypatch.delenv("EVOLVE_HARBOR_NO_PROXY", raising=False)
    for name in module._BYPASS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "workspace-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://workspace.example/v1")

    assert module._agent_env({"agent": "mini-swe-agent"}) == {
        "OPENAI_API_KEY": "workspace-key",
        "OPENAI_BASE_URL": "https://workspace.example/v1",
        "no_proxy": "workspace.example",
        "NO_PROXY": "workspace.example",
    }
    assert (
        module._agent_env(
            {
                "agent": "mini-swe-agent",
                "agent_env": {"OPENAI_BASE_URL": "https://configured.example/v1"},
            }
        )["OPENAI_BASE_URL"]
        == "https://configured.example/v1"
    )


def test_harbor_mutate_forwards_dependency_proxies_with_model_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harbor_runner_module()
    monkeypatch.setenv("OPENAI_API_KEY", "workspace-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://workspace.example/v1")
    monkeypatch.setenv("http_proxy", "http://dependency-proxy.example:8118")
    monkeypatch.setenv("HTTPS_PROXY", "http://dependency-proxy.example:8118")
    monkeypatch.setenv("no_proxy", ".internal.example")
    monkeypatch.setenv("NO_PROXY", ".upper.example")

    assert module._agent_env({"agent": "mini-swe-agent"}) == {
        "OPENAI_API_KEY": "workspace-key",
        "OPENAI_BASE_URL": "https://workspace.example/v1",
        "http_proxy": "http://dependency-proxy.example:8118",
        "HTTP_PROXY": "http://dependency-proxy.example:8118",
        "https_proxy": "http://dependency-proxy.example:8118",
        "HTTPS_PROXY": "http://dependency-proxy.example:8118",
        "no_proxy": ".internal.example,.upper.example,workspace.example",
        "NO_PROXY": ".internal.example,.upper.example,workspace.example",
    }


def test_harbor_mutate_redacts_configured_proxy_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _harbor_runner_module()
    proxy = "http://private-user:private-password@proxy.example.invalid:8118"
    monkeypatch.setenv("HTTPS_PROXY", proxy)

    redacted = module._redact(f"dependency download through {proxy} timed out")

    assert proxy not in redacted
    assert redacted == "dependency download through [REDACTED] timed out"


def test_harbor_mutate_redacts_config_only_proxy_from_command_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for name in (
        "EVOLVE_HARBOR_HTTP_PROXY",
        "EVOLVE_HARBOR_HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    proxy = "http://config-user:config-password@proxy.example.invalid:8118"
    ctx = _ctx(checkout, run_dir)
    ctx.config["agent_env"] = {"HTTPS_PROXY": proxy}

    _harbor_runner_module().run_agent(checkout, "failure evidence", ctx)

    command = json.loads((run_dir / "mutate" / "harbor" / "command.json").read_text())
    assert all(proxy not in argument for argument in command)
    assert "HTTPS_PROXY=[REDACTED]" in command


def test_harbor_mutate_child_creates_private_files(tmp_path: Path) -> None:
    module = _harbor_runner_module()
    output = tmp_path / "child-output"
    log = tmp_path / "harbor.log"

    returncode, _wall_s = module._run_harbor(
        ["/bin/sh", "-c", 'printf private > "$OUTPUT_PATH"'],
        tmp_path,
        log,
        {**os.environ, "OUTPUT_PATH": str(output)},
    )

    assert returncode == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_harbor_timeout_removes_only_its_compose_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harbor_runner_module()
    jobs = tmp_path / "jobs"
    trial = jobs / "readonly-job" / "readonly-trial"
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(json.dumps({"trial_name": "Readonly-Trial__AbC123"}))
    docker_calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        docker_calls.append(command)
        if command[1:3] == ["ps", "-aq"]:
            return SimpleNamespace(returncode=0, stdout="container-id\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="container-id\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    returncode, _wall_s = module._run_harbor(
        [
            "/bin/sh",
            "-c",
            "sleep 30",
            "--jobs-dir",
            str(jobs),
            "--job-name",
            "readonly-job",
        ],
        tmp_path,
        tmp_path / "harbor.log",
        dict(os.environ),
        timeout_s=0.05,
    )

    assert returncode != 0
    assert docker_calls == [
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=readonly-trial__abc123__env",
        ],
        ["docker", "rm", "-f", "container-id"],
    ]
    assert "cleaned timed-out Harbor project readonly-trial__abc123__env" in (tmp_path / "harbor.log").read_text()


def test_codex_harbor_process_cannot_fall_back_to_host_openai_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _harbor_runner_module()
    monkeypatch.setattr(
        module,
        "resolve_execution_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(process_environment=lambda values: dict(values)),
    )
    host = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "judge-key",
        "OPENAI_BASE_URL": "http://judge-bridge.example/v1",
        "OPENAI_API_BASE": "http://judge-bridge.example/v1",
    }

    assert module._harbor_process_env({"agent": "codex"}, host) == {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin",
    }
    assert module._harbor_process_env({"agent": "mini-swe-agent"}, host) == host


def test_harbor_process_discovers_colima_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _harbor_runner_module()
    socket_path = tmp_path / ".colima" / "default" / "docker.sock"
    resolve = module.resolve_execution_runtime
    monkeypatch.setattr(
        module,
        "resolve_execution_runtime",
        lambda config, environment: resolve(
            config,
            environment,
            host_platform="darwin",
            home=tmp_path,
            socket_probe=lambda path: path == socket_path,
        ),
    )

    assert module._harbor_process_env({}, {"HOME": str(tmp_path)}) == {
        "HOME": str(tmp_path),
        "DOCKER_HOST": f"unix://{socket_path}",
    }
    assert (
        module._harbor_process_env({}, {"HOME": str(tmp_path), "DOCKER_HOST": "unix:///custom/docker.sock"})[
            "DOCKER_HOST"
        ]
        == "unix:///custom/docker.sock"
    )


def test_harbor_process_discovers_linux_system_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _harbor_runner_module()
    socket_path = Path("/var/run/docker.sock")
    resolve = module.resolve_execution_runtime
    monkeypatch.setattr(
        module,
        "resolve_execution_runtime",
        lambda config, environment: resolve(
            config,
            environment,
            host_platform="linux",
            home=tmp_path,
            uid=1234,
            socket_probe=lambda path: path == socket_path,
        ),
    )

    assert module._harbor_process_env({}, {"HOME": str(tmp_path)}) == {
        "HOME": str(tmp_path),
        "DOCKER_HOST": "unix:///var/run/docker.sock",
    }


def test_harbor_rejects_oversized_instruction_with_unsafe_agent(tmp_path: Path) -> None:
    runner = _harbor_runner_module()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x" * (96 * 1024 + 1))

    with pytest.raises(RuntimeError, match="harbor_instruction_transport_unsafe"):
        runner._instruction_transport("mini-swe-agent", prompt)


@pytest.mark.parametrize("agent", [INSTALLED_AGENT, LEGACY_INSTALLED_AGENT])
def test_harbor_accepts_oversized_instruction_with_installed_agent(tmp_path: Path, agent: str) -> None:
    runner = _harbor_runner_module()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x" * 200_000)

    assert runner._instruction_transport(agent, prompt) == {
        "bytes": 200_000,
        "mode": "mounted-file",
        "safe": True,
    }


@pytest.mark.parametrize(
    "agent",
    [
        CANDIDATE_AGENT,
        LEGACY_CANDIDATE_AGENT,
        "custom:FileTaskMiniSweAgent",
        "custom:MiniSweSourceAgent",
    ],
)
def test_harbor_does_not_infer_safe_transport_from_class_suffix(tmp_path: Path, agent: str) -> None:
    runner = _harbor_runner_module()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x" * 200_000)

    with pytest.raises(RuntimeError, match="harbor_instruction_transport_unsafe"):
        runner._instruction_transport(agent, prompt)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    run_dir = tmp_path / "runs" / "gen-1"
    (checkout / "target").mkdir(parents=True)
    (checkout / "operators").mkdir()
    (checkout / "target" / "agent.py").write_text("print('parent')\n")
    (checkout / "pyproject.toml").write_text("[project]\nname='test'\nversion='0'\n")
    (checkout / "uv.lock").write_text("version = 1\n")
    (checkout / ".gitignore").write_text("artifacts/\n")
    (checkout / "target" / "obsolete.txt").write_text("remove me\n")
    (checkout / "operators" / "mutate.md").write_text(
        "# Mutate Operator\n\nImprove the target from the supplied failure evidence.\n"
    )
    (checkout / "evolve.yaml").write_text(
        "experiment:\n  id: test\n"
        "target:\n  seed: builtin-dummy\n"
        "surface:\n  include:\n    - target/**\n    - operators/**\n  exclude: []\n"
        "operators:\n  mutate: {operator: hyperagents, timeout_s: 30, config: {runner: harbor}}\n"
        "evaluator:\n  engine: harbor\n  dataset: pass@k\n"
        f"  agent: {CANDIDATE_AGENT}\n"
    )
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.name", "test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "parent")
    _git(checkout, "tag", "gen/0")
    (checkout / "archive.jsonl").write_text('{"genid":"0"}\n')
    (checkout / "artifacts" / "user").mkdir(parents=True)
    (checkout / "artifacts" / "user" / "brief.md").write_text("USER CONTEXT\n")
    parent_artifacts = checkout / "artifacts" / "generations" / "0"
    parent_artifacts.mkdir(parents=True)
    (parent_artifacts / "handoff.md").write_text("PARENT HANDOFF\n")
    evidence = run_dir / "analyze" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "raw_traces.jsonl").write_text('{"task_name":"task-a"}\n')
    return checkout, run_dir


def _ctx(checkout: Path, run_dir: Path) -> OperatorContext:
    return OperatorContext(
        workspace=checkout,
        checkout=checkout,
        run_dir=run_dir,
        genid="1",
        parent="0",
        round=None,
        fan_out=1,
        config={
            "runner": "harbor",
            "agent": "mini-swe-agent",
            "model": "gpt-test",
            "environment": "docker",
        },
        rng=random.Random(0),
        timeout_s=30,
    )


def test_visible_run_inputs_do_not_merge_current_generation_twice(tmp_path: Path) -> None:
    checkout, _ = _checkout(tmp_path)
    run_dir = checkout / "runs" / "gen-1"
    snapshot = run_dir / "rollout" / "certified-jobs" / "snapshot" / "job"
    snapshot.mkdir(parents=True)
    (snapshot / "result.json").write_text('{"reward": 1}\n')
    (run_dir / "rollout" / "harbor-state.json").write_text('{"schema_version": 1}\n')
    feedback = run_dir / "feedback" / "evidence"
    feedback.mkdir(parents=True)
    (feedback / "harbor-state.json").write_text('{"schema_version": 1}\n')
    for path in (snapshot, snapshot.parent):
        path.chmod(0o555)
    destination = tmp_path / "bundle"
    destination.mkdir()

    _harbor_runner_module()._copy_visible_run_inputs(_ctx(checkout, run_dir), destination)

    assert (
        destination / "runs" / "gen-1" / "rollout" / "certified-jobs" / "snapshot" / "job" / "result.json"
    ).read_text() == '{"reward": 1}\n'
    copied_snapshot = destination / "runs" / "gen-1" / "rollout" / "certified-jobs" / "snapshot"
    assert stat.S_IMODE(copied_snapshot.stat().st_mode) & stat.S_IWUSR
    assert stat.S_IMODE((copied_snapshot / "job" / "result.json").stat().st_mode) == 0o644
    assert (destination / "runs" / "gen-1" / "rollout" / "harbor-state.json").is_file()
    assert (destination / "runs" / "gen-1" / "feedback" / "evidence" / "harbor-state.json").is_file()


def _install_fake_harbor(bin_dir: Path) -> Path:
    harbor = bin_dir / "harbor"
    harbor.parent.mkdir(parents=True, exist_ok=True)
    harbor.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path


def option(*names):
    for name in names:
        if name in sys.argv:
            return sys.argv[sys.argv.index(name) + 1]
    raise SystemExit(f"missing option: {names}")


if len(sys.argv) < 2 or sys.argv[1] != "exec":
    raise SystemExit("expected harbor exec")
if "--config" in sys.argv:
    config = json.loads(Path(option("--config")).read_text())
    compile_config = config["map"]["compile"]
    job_config = config["map"]["job"]
    source = Path(compile_config["environments"][0]["paths"][0])
    jobs_dir = Path(job_config["jobs_dir"])
    job_name = job_config["job_name"]
    agent_config = job_config["agents"][0]
    if agent_config["name"] not in (
        "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent",
        "evolve.integrations.harbor.miniswe_task_file:FileTaskMiniSweAgent",
    ):
        raise SystemExit("unexpected config agent")
    if agent_config["model_name"] != "gpt-test":
        raise SystemExit("expected config model")
    readonly = "/app/task/workspace" not in compile_config.get("artifacts", [])
    miniswe = True
else:
    if "--no-scan" not in sys.argv:
        raise SystemExit("expected --no-scan")
    readonly = os.environ.get("FAKE_HARBOR_MODE") == "readonly"
    if readonly:
        if "--artifact" in sys.argv:
            raise SystemExit("readonly execution must not request an artifact")
    elif option("--artifact") != "/app/task/workspace":
        raise SystemExit("expected /app/task/workspace artifact")
    if option("--workdir") != "/app":
        raise SystemExit("expected /app workdir")
    agent_name = option("--agent")
    if agent_name not in (
        "codex",
        "mini-swe-agent",
        "evolve.integrations.harbor.miniswe_candidate:CandidateMiniSweAgent",
        "evolve.integrations.harbor.miniswe_candidate:MiniSweSourceAgent",
        "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent",
        "evolve.integrations.harbor.miniswe_task_file:FileTaskMiniSweAgent",
    ):
        raise SystemExit("unexpected agent")
    miniswe = agent_name in (
        "mini-swe-agent",
        "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent",
        "evolve.integrations.harbor.miniswe_task_file:FileTaskMiniSweAgent",
    )
    if option("--model") != "gpt-test":
        raise SystemExit("expected gpt-test model")
    source = Path(option("--path", "-p"))
    if readonly and not (source / ".evolve-readonly").is_file():
        raise SystemExit("read-only task root must be materialized")
    jobs_dir = Path(option("--jobs-dir"))
    job_name = option("--job-name")
job_dir = jobs_dir / job_name
trial_dir = job_dir / "task-0001__fake"
trial_dir.mkdir(parents=True, exist_ok=True)
artifact = trial_dir / "artifacts" / "app" / "task" / "workspace"
if not readonly:
    artifact.parent.mkdir(parents=True, exist_ok=True)
    workspace = source / "workspace"
    if not (workspace / ".git").exists():
        raise SystemExit("workspace is missing sanitized Git baseline")
    if (workspace / "archive.jsonl").exists():
        raise SystemExit("workspace exposes archive evidence")
    if (workspace / "evaluator").exists():
        raise SystemExit("workspace exposes evaluator data")
    if not (workspace / "runs" / "gen-1" / "analyze" / "evidence" / "raw_traces.jsonl").is_file():
        raise SystemExit("workspace is missing current trace evidence")
    if (workspace / "artifacts" / "user" / "brief.md").read_text() != "USER CONTEXT\\n":
        raise SystemExit("workspace is missing user artifacts")
    if (workspace / "artifacts" / "generations" / "0" / "handoff.md").read_text() != "PARENT HANDOFF\\n":
        raise SystemExit("workspace is missing selected-parent artifacts")
    if not (workspace / "artifacts" / "generations" / "1").is_dir():
        raise SystemExit("workspace is missing current generation artifact directory")
    shutil.copytree(workspace, artifact, symlinks=True)

    (artifact / "target" / "agent.py").write_text("print('child')\\n")
    (artifact / "target" / "added.txt").write_text("created in Harbor\\n")
    (artifact / "target" / "obsolete.txt").unlink()
    if (artifact / "operators").exists():
        (artifact / "operators" / "mutate.md").write_text("# Changed by Harbor\\n")
    current_artifacts = artifact / "artifacts" / "generations" / "1"
    (current_artifacts / "handoff.md").write_text("NEXT HANDOFF\\n")
    (current_artifacts / "notes.txt").write_text("DURABLE NOTE\\n")
    if os.environ.get("FAKE_HARBOR_MODE") == "artifact-protected-edit":
        (artifact / "artifacts" / "user" / "brief.md").write_text("COMPROMISED\\n")
        (artifact / "artifacts" / "generations" / "0" / "handoff.md").write_text("COMPROMISED\\n")
    if os.environ.get("FAKE_HARBOR_MODE") == "artifact-ancestor-symlink":
        shutil.rmtree(artifact / "artifacts" / "generations")
        escaped = artifact.parent / "outside-artifacts" / "1"
        escaped.mkdir(parents=True)
        (escaped / "handoff.md").write_text("ESCAPED\\n")
        target = os.path.relpath(escaped.parent, artifact / "artifacts")
        (artifact / "artifacts" / "generations").symlink_to(target, target_is_directory=True)
    if os.environ.get("FAKE_HARBOR_MODE") == "artifact-symlink":
        (artifact / "target" / "link.txt").symlink_to("agent.py")
    if os.environ.get("FAKE_HARBOR_MODE") == "protected-edit":
        (artifact / "evolve.yaml").write_text("experiment: {id: compromised}\\n")

manifest = [
    {
        "source": "/logs/artifacts",
        "destination": "artifacts/logs/artifacts",
        "type": "directory",
        "status": "empty",
        "service": None,
    },
    {
        "source": "/app/task/workspace",
        "destination": "artifacts/app/task/workspace",
        "type": "directory",
        "status": "ok",
        "service": None,
    },
]
if readonly:
    report_dir = trial_dir / "artifacts" / "logs" / "artifacts"
    report_dir.mkdir(parents=True)
    (report_dir / "ahe-debugger-response.md").write_text("ROOT CAUSE: collected artifact")
    manifest = [
        {
            "source": "/logs/artifacts",
            "destination": "artifacts/logs/artifacts",
            "type": "directory",
            "status": "ok",
            "service": None,
        }
    ]
(trial_dir / "artifacts" / "manifest.json").write_text(json.dumps(manifest))

exception = None
if os.environ.get("FAKE_HARBOR_MODE") == "agent-error":
    exception = {
        "exception_type": "NonZeroAgentExitCodeError",
        "exception_message": "agent failed",
        "exception_traceback": "omitted",
        "occurred_at": "2026-07-15T00:00:00",
    }

result = {
    "trial_name": "task-0001__fake",
    "agent_info": {
        "name": "mini-swe-agent",
        "version": "fake",
        "model_info": {"name": "gpt-test", "provider": None},
    },
    "exception_info": exception,
    "agent_result": {
        "n_input_tokens": 100,
        "n_cache_tokens": 25,
        "n_output_tokens": 10,
        "cost_usd": 0.25,
    },
    "verifier_result": {"rewards": {"reward": 1.0}},
}
(trial_dir / "result.json").write_text(json.dumps(result))
(job_dir / "result.json").write_text(json.dumps({"stats": {"n_completed_trials": 1}}))

agent_dir = trial_dir / "agent"
agent_dir.mkdir()
(agent_dir / "trajectory.json").write_text(
    json.dumps(
        {
            "steps": [
                {
                    "source": "agent",
                    "message": (
                        "ROOT CAUSE: ignored trajectory"
                        if readonly
                        else 'Completed the mutation.\\npredicted_fixes: ["task-1"]'
                    ),
                }
            ]
        }
    )
)
if miniswe:
    exit_status = "RepeatedFormatError" if os.environ.get("FAKE_HARBOR_MODE") == "miniswe-exit-error" else "Submitted"
    (agent_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "exit",
                        "content": exit_status,
                        "extra": {"exit_status": exit_status, "submission": ""},
                    }
                ]
            }
        )
    )
print(f"Map job written to {job_dir}")
"""
    )
    harbor.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        '[ -z "${UV_MARKER:-}" ] || printf called > "$UV_MARKER"\n'
        '[ "$1" = run ] || exit 90\nshift\n'
        '[ "$1" = --project ] || exit 91\nshift 2\n'
        '[ "$1" = --frozen ] || exit 92\nshift\n'
        '[ "$1" = --python ] || exit 93\nshift 2\n'
        'exec "$@"\n'
    )
    uv.chmod(0o755)
    return harbor


def test_harbor_mutate_round_trips_target_and_writes_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    marker = tmp_path / "uv-called"
    monkeypatch.setenv("UV_MARKER", str(marker))

    runner = _harbor_runner_module()
    result = runner.run_agent(checkout, "failure evidence", _ctx(checkout, run_dir))

    assert (checkout / "target" / "agent.py").read_text() == "print('child')\n"
    assert (checkout / "target" / "added.txt").read_text() == "created in Harbor\n"
    assert not (checkout / "target" / "obsolete.txt").exists()
    assert (checkout / "artifacts" / "generations" / "1" / "handoff.md").read_text() == "NEXT HANDOFF\n"
    assert (checkout / "artifacts" / "generations" / "1" / "notes.txt").read_text() == "DURABLE NOTE\n"
    meta_dir = run_dir / "mutate"
    usage = result.usage
    assert usage["usd"] == 0.25
    assert usage["input_tokens"] == 100
    assert usage["cache_tokens"] == 25
    assert usage["output_tokens"] == 10
    assert 'predicted_fixes: ["task-1"]' in result.output
    prompt = (meta_dir / "harbor" / "prompt.md").read_text()
    assert "failure evidence" in prompt
    assert "/app/task/workspace" in prompt
    assert "remove generated virtual environments" in prompt
    assert "gate/sealed evaluations are intentionally unavailable" in prompt
    assert "/app/candidate" not in prompt
    command = json.loads((meta_dir / "harbor" / "command.json").read_text())
    assert command[command.index("--artifact") + 1] == "/app/task/workspace"
    assert command[command.index("--workdir") + 1] == "/app"
    assert list((meta_dir / "harbor" / "jobs").glob("*/*/result.json"))
    assert marker.read_text() == "called"


def test_harbor_discards_returned_edits_to_user_and_prior_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "artifact-protected-edit")

    _harbor_runner_module().run_agent(checkout, "failure evidence", _ctx(checkout, run_dir))

    assert (checkout / "artifacts" / "user" / "brief.md").read_text() == "USER CONTEXT\n"
    assert (checkout / "artifacts" / "generations" / "0" / "handoff.md").read_text() == "PARENT HANDOFF\n"
    assert (checkout / "artifacts" / "generations" / "1" / "handoff.md").read_text() == "NEXT HANDOFF\n"


def test_harbor_rejects_symlinked_artifact_ancestor_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "artifact-ancestor-symlink")

    with pytest.raises(AgentCommandError, match="symlink"):
        _harbor_runner_module().run_agent(checkout, "failure evidence", _ctx(checkout, run_dir))

    assert (checkout / "target" / "agent.py").read_text() == "print('parent')\n"
    assert not (checkout / "artifacts" / "generations" / "1" / "handoff.md").exists()


@pytest.mark.parametrize("agent", [CANDIDATE_AGENT, LEGACY_CANDIDATE_AGENT])
def test_candidate_adapter_uses_generic_mutate_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    ctx = _ctx(checkout, run_dir)
    ctx.config["agent"] = agent

    _harbor_runner_module().run_agent(checkout, "failure evidence", ctx)

    harbor_root = run_dir / "mutate" / "harbor"
    command = json.loads((harbor_root / "command.json").read_text())
    harbor_index = command.index("harbor")
    assert command[harbor_index : harbor_index + 2] == ["harbor", "exec"]
    assert "--config" not in command
    assert command[command.index("--agent") + 1] == agent
    assert not (harbor_root / "exec-config.json").exists()


def test_file_task_mutate_configures_per_attempt_timeout_and_timeout_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    ctx = replace(_ctx(checkout, run_dir), timeout_s=3600)
    ctx.config.update(
        {
            "agent": INSTALLED_AGENT,
            "image": "evolve-mutate:test",
            "max_retries": 1,
        }
    )
    runner = _harbor_runner_module()
    observed: dict[str, object] = {}
    real_run_harbor = runner._run_harbor

    def capture_run_harbor(*args, timeout_s=None, **kwargs):
        observed["process_timeout_s"] = timeout_s
        return real_run_harbor(*args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(runner, "_run_harbor", capture_run_harbor)

    runner.run_agent(checkout, "failure evidence", ctx)

    harbor_root = run_dir / "mutate" / "harbor"
    config = ExecConfig.model_validate_json((harbor_root / "exec-config.json").read_text())
    job = config.map.job
    assert config.map.compile.environments[0].docker_image == "evolve-mutate:test"
    assert job.agents[0].override_timeout_sec == 3600
    assert "EVOLVE_CANDIDATE_SOURCE" not in job.agents[0].env
    assert job.environment.mounts is None
    assert job.n_attempts == 1
    assert job.retry.max_retries == 1
    assert job.retry.exclude_exceptions == {
        "VerifierTimeoutError",
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
        "ApiUsageLimitError",
    }
    queue = TrialQueue(1, retry_config=job.retry)
    assert queue._should_retry_exception("AgentTimeoutError")
    assert queue._calculate_backoff_delay_sec(0) == 1
    assert observed["process_timeout_s"] == 13380.0


def test_codex_mutate_keeps_harbor_whole_process_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    ctx = _ctx(checkout, run_dir)
    ctx.config.update(
        {
            "agent": "codex",
            "max_retries": 1,
            "timeout_s": 3600,
        }
    )
    runner = _harbor_runner_module()
    observed: dict[str, object] = {}
    real_run_harbor = runner._run_harbor

    def capture_run_harbor(*args, timeout_s=None, **kwargs):
        observed["process_timeout_s"] = timeout_s
        return real_run_harbor(*args, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(runner, "_run_harbor", capture_run_harbor)

    runner.run_agent(checkout, "failure evidence", ctx)

    command = json.loads((run_dir / "mutate" / "harbor" / "command.json").read_text())
    assert "--config" not in command
    assert command[command.index("--agent") + 1] == "codex"
    assert observed["process_timeout_s"] is None


def test_agent_timeout_retry_loop_fits_full_lifecycle_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _harbor_runner_module()
    retry = ExecConfig.model_validate(
        {
            "map": {
                "compile": {
                    "output_dir": str(tmp_path / "tasks"),
                    "environments": [{"paths": [str(tmp_path)]}],
                    "instructions": [{"text": "test"}],
                },
                "job": {
                    "jobs_dir": str(tmp_path / "jobs"),
                    "agents": [{"name": "mini-swe-agent"}],
                    "retry": runner._retry_config({"max_retries": 1}),
                },
            }
        }
    ).map.job.retry
    elapsed_s = 600.0  # task compilation
    attempts: list[int] = []

    class TimedTrial:
        def __init__(self, attempt: int) -> None:
            self.attempt = attempt
            self.config = SimpleNamespace(agent=SimpleNamespace(n_concurrent=None))
            self.paths = SimpleNamespace(trial_dir=tmp_path / f"trial-{attempt}")
            self.paths.trial_dir.mkdir()

        def add_hook(self, _event, _hook) -> None:
            pass

        async def run(self):
            nonlocal elapsed_s
            # Environment start (600), setup (360), agent (3600), verifier
            # (600), artifact/log collection (600), and teardown (600).
            elapsed_s += 6360.0
            exception_info = (
                ExceptionInfo.from_exception(AgentTimeoutError("first attempt timed out"))
                if self.attempt == 0
                else None
            )
            return SimpleNamespace(exception_info=exception_info)

    async def create_trial(_config):
        attempt = len(attempts)
        attempts.append(attempt)
        return TimedTrial(attempt)

    real_rmtree = shutil.rmtree

    def timed_rmtree(path, *, ignore_errors=False):
        nonlocal elapsed_s
        elapsed_s += 59.0  # failed-trial removal and recreation allowance
        real_rmtree(path, ignore_errors=ignore_errors)

    async def timed_sleep(delay_s):
        nonlocal elapsed_s
        elapsed_s += delay_s

    monkeypatch.setattr(Trial, "create", staticmethod(create_trial))
    monkeypatch.setattr("harbor.trial.queue.shutil.rmtree", timed_rmtree)
    monkeypatch.setattr("harbor.trial.queue.asyncio.sleep", timed_sleep)

    queue = TrialQueue(1, retry_config=retry)
    result = asyncio.run(
        queue._execute_trial_with_retries(SimpleNamespace(agent=None, trial_name="timeout-then-success"))
    )

    assert EnvironmentConfig().build_timeout_sec == 600
    assert Trial._AGENT_SETUP_TIMEOUT_SEC == 360
    assert VerifierConfig().timeout_sec == 600
    assert attempts == [0, 1]
    assert result.exception_info is None
    assert elapsed_s == runner._mutate_process_timeout_s(
        {
            "runner": "harbor",
            "agent": INSTALLED_AGENT,
            "max_retries": 1,
        },
        3600,
    )
    assert 600.0 + elapsed_s + 600.0 + 60.0 == _operator_deadline_s(
        "mutate",
        {
            "runner": "harbor",
            "agent": INSTALLED_AGENT,
            "max_retries": 1,
        },
        3600,
    )


@pytest.mark.parametrize(
    "agent",
    [
        INSTALLED_AGENT,
        LEGACY_INSTALLED_AGENT,
    ],
)
def test_harbor_mutate_rejects_unsuccessful_miniswe_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "miniswe-exit-error")
    ctx = _ctx(checkout, run_dir)
    ctx.config["agent"] = agent

    with pytest.raises(AgentCommandError, match="exit_status=RepeatedFormatError"):
        _harbor_runner_module().run_agent(checkout, "failure evidence", ctx)

    assert (checkout / "target" / "agent.py").read_text() == "print('parent')\n"


def test_harbor_mutate_rejects_legacy_pythonpath(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    ctx = _ctx(checkout, run_dir)
    ctx.config["agent_pythonpath"] = "/legacy"

    with pytest.raises(AgentCommandError, match="agent_pythonpath was removed"):
        _harbor_runner_module().run_agent(checkout, "failure evidence", ctx)


def test_harbor_mutate_forwards_custom_environment_kwargs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    ctx = _ctx(checkout, run_dir)
    ctx.config["environment"] = "evolve.harbor_local:LocalEnvironment"
    ctx.config["environment_kwargs"] = {"workdir": "/workspace"}

    _harbor_runner_module().run_agent(checkout, "failure evidence", ctx)

    command = json.loads((run_dir / "mutate" / "harbor" / "command.json").read_text())
    assert command[command.index("--env") + 1] == "evolve.harbor_local:LocalEnvironment"
    assert command[command.index("--environment-kwarg") + 1] == 'workdir="/workspace"'


def test_harbor_readonly_agent_returns_response_without_candidate_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "readonly")
    runner = _harbor_runner_module()
    output_dir = run_dir / "analyze" / "debugger" / "task-a" / "attempt-1"

    result = runner.run_readonly_agent(
        checkout,
        "Analyze this trace",
        _ctx(checkout, run_dir),
        output_dir=output_dir,
        job_name="ahe-debug-task-a-attempt-1",
        timeout_s=30,
    )

    assert result.output == "ROOT CAUSE: collected artifact"
    assert result.usage["usd"] == 0.25
    command = json.loads((output_dir / "command.json").read_text())
    assert "--artifact" not in command
    assert not (checkout / "target" / "added.txt").exists()


def test_harbor_readonly_agent_mounts_input_files_under_task_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "readonly")
    runner = _harbor_runner_module()
    output_dir = run_dir / "analyze" / "debugger" / "task-a" / "attempt-1"

    runner.run_readonly_agent(
        checkout,
        "Analyze the mounted trace evidence",
        _ctx(checkout, run_dir),
        output_dir=output_dir,
        job_name="ahe-debug-task-a-attempt-1",
        timeout_s=30,
        input_files={"trace-evidence.json": '{"task_name":"task-a"}\n'},
    )

    assert (output_dir / "task" / "inputs" / "trace-evidence.json").read_text() == ('{"task_name":"task-a"}\n')


def test_harbor_mutate_does_not_pass_run_only_timeout_multiplier(tmp_path: Path) -> None:
    runner = _harbor_runner_module()
    command = runner._base_command(
        ["harbor"],
        tmp_path / "task",
        tmp_path / "prompt.md",
        tmp_path / "jobs",
        tmp_path / "tasks",
        "job",
        {"agent_timeout_multiplier": 4},
    )

    assert "--agent-timeout-multiplier" not in command


def test_harbor_mutate_serializes_structured_agent_kwargs_as_json(tmp_path: Path) -> None:
    runner = _harbor_runner_module()
    command = runner._base_command(
        ["harbor"],
        tmp_path / "task",
        tmp_path / "prompt.md",
        tmp_path / "jobs",
        tmp_path / "tasks",
        "job",
        {
            "agent": "evolve.integrations.harbor.local_auto_agent:LocalAutoAgent",
            "agent_kwargs": {"model_by_agent": {"codex": "gpt-test"}},
        },
    )

    encoded = command[command.index("--ak") + 1]
    assert encoded == 'model_by_agent={"codex":"gpt-test"}'


def test_readonly_artifact_output_rejects_missing_report(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial"
    artifacts = trial_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "source": "/logs/artifacts",
                    "destination": "artifacts/logs/artifacts",
                    "status": "ok",
                }
            ]
        )
    )

    with pytest.raises(RuntimeError, match="missing AHE debugger report"):
        _harbor_runner_module()._readonly_artifact_output(trial_dir)


def test_readonly_artifact_output_rejects_escape(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial"
    artifacts = trial_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "manifest.json").write_text(
        json.dumps([{"source": "/logs/artifacts", "destination": "../outside", "status": "ok"}])
    )

    with pytest.raises(RuntimeError, match="escaped the trial"):
        _harbor_runner_module()._readonly_artifact_output(trial_dir)


def test_readonly_artifact_output_rejects_empty_report(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial"
    report_dir = trial_dir / "artifacts" / "logs" / "artifacts"
    report_dir.mkdir(parents=True)
    (report_dir / "ahe-debugger-response.md").write_text("\n")
    (trial_dir / "artifacts" / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "source": "/logs/artifacts",
                    "destination": "artifacts/logs/artifacts",
                    "status": "ok",
                }
            ]
        )
    )

    with pytest.raises(RuntimeError, match="empty AHE debugger report"):
        _harbor_runner_module()._readonly_artifact_output(trial_dir)


def test_harbor_mutate_round_trips_target_and_operators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, run_dir = _checkout(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    ctx = _ctx(checkout, run_dir)
    ctx.config["editable_roots"] = ["target", "operators"]
    runner = _harbor_runner_module()
    runner.run_agent(checkout, "failure evidence", ctx)

    assert (checkout / "target" / "agent.py").read_text() == "print('child')\n"
    assert (checkout / "operators" / "mutate.md").read_text() == "# Changed by Harbor\n"


def test_harbor_mutate_rejects_non_top_level_editable_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, run_dir = _checkout(tmp_path)
    ctx = _ctx(checkout, run_dir)
    ctx.config["editable_roots"] = ["target/src"]
    runner = _harbor_runner_module()

    with pytest.raises(AgentCommandError, match="top-level relative directory"):
        runner.run_agent(checkout, "failure evidence", ctx)


def test_harbor_mutate_accepts_root_containing_narrow_mutable_surface(tmp_path: Path) -> None:
    checkout, _run_dir = _checkout(tmp_path)
    config = checkout / "evolve.yaml"
    config.write_text(config.read_text().replace("    - target/**\n", "    - target/agent.py\n"))
    runner = _harbor_runner_module()

    roots = runner._editable_roots(["target"], runner.load_surface_policy(checkout))

    assert roots == ("target",)


def test_harbor_mutate_rejects_root_disjoint_from_mutable_surface(tmp_path: Path) -> None:
    checkout, _run_dir = _checkout(tmp_path)
    config = checkout / "evolve.yaml"
    config.write_text(
        config.read_text().replace("    - target/**\n", "    - target/agent.py\n").replace("    - operators/**\n", "")
    )
    runner = _harbor_runner_module()

    with pytest.raises(ValueError, match="contains no mutable surface paths"):
        runner._editable_roots(["operators"], runner.load_surface_policy(checkout))


def test_harbor_mutate_rejects_protected_workspace_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, run_dir = _checkout(tmp_path)
    before_config = (checkout / "evolve.yaml").read_text()
    before_target = (checkout / "target" / "agent.py").read_text()
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "protected-edit")

    with pytest.raises(AgentCommandError, match="outside surface"):
        _harbor_runner_module().run_agent(checkout, "failure evidence", _ctx(checkout, run_dir))

    assert (checkout / "evolve.yaml").read_text() == before_config
    assert (checkout / "target" / "agent.py").read_text() == before_target


def test_multi_root_install_rolls_back_when_second_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)
    bundle = runner._prepare_bundle(checkout, _ctx(checkout, run_dir), ["target", "operators"], surface)
    assert bundle.staging.parent == checkout.parent
    returned = tmp_path / "returned"
    shutil.copytree(bundle.workspace, returned, symlinks=True)
    (returned / "target" / "agent.py").write_text("print('child')\n")
    (returned / "operators" / "mutate.md").write_text("# child\n")
    before_target = (checkout / "target" / "agent.py").read_text()
    before_operator = (checkout / "operators" / "mutate.md").read_text()
    rename = Path.rename

    def fail_operators(path: Path, target: Path) -> Path:
        if path.as_posix().endswith("replacements/operators"):
            raise OSError("simulated second-root failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_operators)
    try:
        with pytest.raises(OSError, match="second-root"):
            runner._install_bundle(checkout, returned, bundle, "gen/0", surface)
        assert (checkout / "target" / "agent.py").read_text() == before_target
        assert (checkout / "operators" / "mutate.md").read_text() == before_operator
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)


@pytest.mark.parametrize("trajectory_only", [None, False, True])
def test_harbor_bundle_never_exposes_gate_or_sealed_data(tmp_path: Path, trajectory_only: bool | None) -> None:
    checkout, run_dir = _checkout(tmp_path)
    evaluator = checkout / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps(
            {
                "tasks": {
                    "train": ["train-task"],
                    "gate": ["gate-secret-task"],
                    "sealed": ["sealed-secret-task"],
                }
            }
        )
    )
    _git(checkout, "add", "evaluator/splits.json")
    _git(checkout, "commit", "-qm", "record private task partitions")
    (checkout / ".evolve-eval-receipts.jsonl").write_text("private-receipt\n")
    private_eval = checkout / "runs" / "evaluations" / "genesis"
    private_eval.mkdir(parents=True)
    (private_eval / "trajectory.json").write_text("gate-secret-task reward=1\n")
    private_anchor = checkout / "runs" / "evaluations" / "anchor"
    private_anchor.mkdir(parents=True)
    (private_anchor / "trajectory.json").write_text("sealed-secret-task reward=1\n")
    prior_evidence = checkout / "runs" / "gen-0" / "analyze" / "evidence"
    prior_evidence.mkdir(parents=True)
    (prior_evidence / "history.json").write_text('{"task_name":"train-task"}\n')
    prior_gate = checkout / "runs" / "gen-0" / "gate"
    prior_gate.mkdir()
    (prior_gate / "result.json").write_text('{"gate-secret-task": 1}\n')
    select = checkout / "runs" / "gen-1" / "select"
    select.mkdir(parents=True)
    (select / "pareto.json").write_text('{"gate-secret-task": 1}\n')
    runner = _harbor_runner_module()
    ctx = _ctx(checkout, run_dir)
    if trajectory_only is not None:
        ctx.config["trajectory_only"] = trajectory_only
    surface = runner.load_surface_policy(checkout)
    bundle = runner._prepare_bundle(checkout, ctx, ["target"], surface)
    try:
        assert not (bundle.workspace / "archive.jsonl").exists()
        assert not (bundle.workspace / ".evolve-eval-receipts.jsonl").exists()
        assert not (bundle.workspace / "evaluator").exists()
        assert not (bundle.workspace / "runs" / "evaluations").exists()
        assert not (bundle.workspace / "runs" / "gen-1" / "select").exists()
        assert not (bundle.workspace / "runs" / "gen-0" / "gate").exists()
        assert (bundle.workspace / "runs" / "gen-0" / "analyze" / "evidence" / "history.json").is_file()
        assert (bundle.workspace / "runs" / "gen-1" / "analyze" / "evidence" / "raw_traces.jsonl").is_file()
        assert (bundle.workspace / "target" / "agent.py").is_file()
        assert _git(bundle.workspace, "status", "--short") == ""
        assert _git(bundle.workspace, "config", "--get", "maintenance.auto") == "false"
        assert _git(bundle.workspace, "config", "--get", "gc.auto") == "0"
        assert _git(bundle.workspace, "log", "--all", "--", "evaluator/splits.json") == ""
        assert "evaluator/splits.json" not in _git(bundle.workspace, "rev-list", "--all", "--objects")
        visible = "\n".join(
            path.read_text(errors="ignore")
            for path in bundle.workspace.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(bundle.workspace).parts
        )
        assert "gate-secret-task" not in visible
        assert "sealed-secret-task" not in visible
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)


def test_harbor_bundle_rejects_private_task_identifiers_in_prompt(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    evaluator = checkout / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps({"tasks": {"train": ["train-task"], "gate": ["gate-secret-task"], "sealed": []}})
    )
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)

    with pytest.raises(RuntimeError, match="private gate/sealed task identifier"):
        runner._prepare_bundle(
            checkout,
            _ctx(checkout, run_dir),
            ["target"],
            surface,
            prompt="analyze gate-secret-task",
        )


def test_harbor_bundle_redacts_private_task_identifiers_echoed_by_train_rollout(
    tmp_path: Path,
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    evaluator = checkout / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps({"tasks": {"train": ["train-task"], "gate": ["gate-secret-task"], "sealed": []}})
    )
    rollout = run_dir / "rollout"
    rollout.mkdir(exist_ok=True)
    (rollout / "cases.json").write_text(
        '{"task_name":"train-task","observation":"found gate-secret-task in an external listing"}\n'
    )
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)
    bundle = runner._prepare_bundle(
        checkout,
        _ctx(checkout, run_dir),
        ["target"],
        surface,
        prompt="safe train evidence",
    )
    try:
        copied = (bundle.workspace / "runs" / "gen-1" / "rollout" / "cases.json").read_text()
        assert "gate-secret-task" not in copied
        assert "[PRIVATE_TASK]" in copied
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)


def test_harbor_bundle_omits_trajectory_judge_workdirs(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    evaluator = checkout / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps({"tasks": {"train": ["train-task"], "gate": ["gate-secret-task"], "sealed": []}})
    )
    evidence = run_dir / "analyze" / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "trajectory_only.json").write_text('[{"task_id":"train-task"}]\n')
    judge = run_dir / "analyze" / "judge" / "0000-task" / "attempt-1"
    judge.mkdir(parents=True)
    (judge / "agent-context.jsonl").write_text('{"unrelated_context":"gate-secret-task"}\n')
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)

    bundle = runner._prepare_bundle(
        checkout,
        _ctx(checkout, run_dir),
        ["target"],
        surface,
        prompt="analyze train-task",
    )
    try:
        copied = bundle.workspace / "runs" / "gen-1" / "analyze"
        assert (copied / "evidence" / "trajectory_only.json").is_file()
        assert not (copied / "judge").exists()
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)


def test_harbor_bundle_allows_private_name_prefix_in_training_identifier(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    evaluator = checkout / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps(
            {
                "tasks": {
                    "train": ["tau3-airline-11"],
                    "gate": ["tau3-airline-1"],
                    "sealed": [],
                }
            }
        )
    )
    evidence = run_dir / "analyze" / "evidence" / "raw_traces.jsonl"
    evidence.write_text('{"task_name":"tau3-airline-11"}\n')
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)

    bundle = runner._prepare_bundle(
        checkout,
        _ctx(checkout, run_dir),
        ["target"],
        surface,
        prompt="analyze tau3-airline-11",
    )
    shutil.rmtree(bundle.staging, ignore_errors=True)


def test_harbor_bundle_exposes_gate_data_only_when_enabled(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    evaluator = checkout / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps({"tasks": {"train": ["train-task"], "gate": ["gate-task"], "sealed": []}})
    )
    _git(checkout, "add", "evaluator/splits.json")
    _git(checkout, "commit", "-qm", "record task partitions")
    (checkout / ".evolve-eval-receipts.jsonl").write_text("receipt\n")
    evaluation = checkout / "runs" / "evaluations" / "genesis"
    evaluation.mkdir(parents=True)
    (evaluation / "trajectory.json").write_text("gate-task reward=1\n")
    ctx = _ctx(checkout, run_dir)
    ctx.config["expose_gate_data"] = True
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)
    bundle = runner._prepare_bundle(checkout, ctx, ["target"], surface, prompt="analyze gate-task")
    try:
        assert (bundle.workspace / "archive.jsonl").is_file()
        assert (bundle.workspace / ".evolve-eval-receipts.jsonl").is_file()
        assert (bundle.workspace / "evaluator" / "splits.json").is_file()
        assert (bundle.workspace / "runs" / "evaluations" / "genesis" / "trajectory.json").is_file()
        assert _git(bundle.workspace, "log", "--all", "--", "evaluator/splits.json")
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)


def test_harbor_bundle_rejects_gate_visibility_when_sealed_split_exists(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    evaluator = checkout / "evaluator"
    evaluator.mkdir()
    (evaluator / "splits.json").write_text(
        json.dumps({"tasks": {"train": ["train-task"], "gate": ["gate-task"], "sealed": ["sealed-task"]}})
    )
    ctx = _ctx(checkout, run_dir)
    ctx.config["expose_gate_data"] = True
    runner = _harbor_runner_module()

    with pytest.raises(RuntimeError, match="sealed split is non-empty"):
        runner._prepare_bundle(checkout, ctx, ["target"], runner.load_surface_policy(checkout))


@pytest.mark.parametrize("expose_gate_data", [False, True])
def test_harbor_bundle_omits_gitignored_checkout_state(
    tmp_path: Path,
    expose_gate_data: bool,
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    (checkout / ".gitignore").write_text("artifacts/\n.venv/\n.cache/\n")
    (checkout / ".venv").mkdir()
    (checkout / ".venv" / "framework-state.txt").write_text("host framework environment\n")
    (checkout / ".cache").mkdir()
    (checkout / ".cache" / "host-state.txt").write_text("host cache\n")
    (checkout / "target" / ".venv").mkdir()
    (checkout / "target" / ".venv" / "candidate-state.txt").write_text("host candidate environment\n")
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)
    ctx = _ctx(checkout, run_dir)
    ctx.config["expose_gate_data"] = expose_gate_data

    bundle = runner._prepare_bundle(checkout, ctx, ["target"], surface)

    try:
        assert not (bundle.workspace / ".venv").exists()
        assert not (bundle.workspace / ".cache").exists()
        assert not (bundle.workspace / "target" / ".venv").exists()
        assert (bundle.workspace / "target" / "agent.py").read_text() == "print('parent')\n"
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)


def test_sanitized_harbor_bundle_disables_background_git_maintenance(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    runner = _harbor_runner_module()
    bundle = runner._prepare_bundle(
        checkout,
        _ctx(checkout, run_dir),
        ["target"],
        runner.load_surface_policy(checkout),
    )

    try:
        assert _git(bundle.workspace, "config", "--get", "gc.auto") == "0"
        assert _git(bundle.workspace, "config", "--get", "maintenance.auto") == "false"
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)


def test_harbor_bundle_rejects_non_boolean_gate_visibility(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    ctx = _ctx(checkout, run_dir)
    ctx.config["expose_gate_data"] = "false"
    runner = _harbor_runner_module()

    with pytest.raises(ValueError, match="expose_gate_data must be true or false"):
        runner._prepare_bundle(
            checkout,
            ctx,
            ["target"],
            runner.load_surface_policy(checkout),
        )


def test_harbor_trial_exception_does_not_modify_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, run_dir = _checkout(tmp_path)
    before = {
        path.relative_to(checkout / "target").as_posix(): path.read_bytes()
        for path in (checkout / "target").rglob("*")
        if path.is_file()
    }
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "agent-error")

    runner = _harbor_runner_module()
    with pytest.raises(AgentCommandError) as excinfo:
        runner.run_agent(checkout, "failure evidence", _ctx(checkout, run_dir))

    assert excinfo.value.returncode == 1
    after = {
        path.relative_to(checkout / "target").as_posix(): path.read_bytes()
        for path in (checkout / "target").rglob("*")
        if path.is_file()
    }
    assert after == before
    assert "NonZeroAgentExitCodeError" in str(excinfo.value)
    error = json.loads((run_dir / "mutate" / "harbor" / "error.json").read_text())
    assert error["type"] == "RuntimeError"
    assert "NonZeroAgentExitCodeError" in error["message"]


def test_harbor_mutate_rejects_source_symlinks_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, run_dir = _checkout(tmp_path)
    (checkout / "outside-secret.txt").write_text("do not upload\n")
    (checkout / "target" / "leak.txt").symlink_to("../outside-secret.txt")
    bin_dir = tmp_path / "bin"
    fake_harbor = _install_fake_harbor(bin_dir)
    marker = tmp_path / "harbor-was-launched"
    fake_harbor.write_text(
        fake_harbor.read_text().replace(
            "if len(sys.argv) < 2",
            f"Path({str(marker)!r}).write_text('yes')\n\nif len(sys.argv) < 2",
        )
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    runner = _harbor_runner_module()
    with pytest.raises(AgentCommandError) as excinfo:
        runner.run_agent(checkout, "failure evidence", _ctx(checkout, run_dir))

    assert excinfo.value.returncode == 1
    assert not marker.exists()
    assert (checkout / "target" / "leak.txt").is_symlink()
    assert "symlink" in str(excinfo.value).lower()


def test_harbor_mutate_rejects_returned_symlinks_without_modifying_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, run_dir = _checkout(tmp_path)
    before = {
        path.relative_to(checkout / "target").as_posix(): path.read_bytes()
        for path in (checkout / "target").rglob("*")
        if path.is_file()
    }
    bin_dir = tmp_path / "bin"
    _install_fake_harbor(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HARBOR_MODE", "artifact-symlink")

    runner = _harbor_runner_module()
    with pytest.raises(AgentCommandError) as excinfo:
        runner.run_agent(checkout, "failure evidence", _ctx(checkout, run_dir))

    assert excinfo.value.returncode == 1
    after = {
        path.relative_to(checkout / "target").as_posix(): path.read_bytes()
        for path in (checkout / "target").rglob("*")
        if path.is_file()
    }
    assert after == before
    assert "symlink" in str(excinfo.value).lower()


def test_install_bundle_omits_ignored_runtime_tree_with_symlinks(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    (checkout / "target" / ".gitignore").write_text(".venv/\n")
    _git(checkout, "add", "target/.gitignore")
    _git(checkout, "commit", "-qm", "ignore runtime environment")
    _git(checkout, "tag", "-f", "gen/0")
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)
    bundle = runner._prepare_bundle(checkout, _ctx(checkout, run_dir), ["target"], surface)
    returned = tmp_path / "returned"
    shutil.copytree(bundle.workspace, returned)
    (returned / "target" / "agent.py").write_text("print('child')\n")
    python = returned / "target" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to("/runtime/python")

    try:
        changed = runner._install_bundle(checkout, returned, bundle, "gen/0", surface)
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)

    assert changed == ["target/agent.py"]
    assert (checkout / "target" / "agent.py").read_text() == "print('child')\n"
    assert not (checkout / "target" / ".venv").exists()


def test_install_bundle_ignores_workspace_runtime_environment(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)

    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)
    bundle = runner._prepare_bundle(checkout, _ctx(checkout, run_dir), ["target"], surface)
    returned = tmp_path / "returned"
    shutil.copytree(bundle.workspace, returned)
    (returned / "target" / "agent.py").write_text("print('child')\n")
    python = returned / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to("/runtime/python")

    try:
        changed = runner._install_bundle(checkout, returned, bundle, "gen/0", surface)
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)

    assert changed == ["target/agent.py"]
    assert (checkout / "target" / "agent.py").read_text() == "print('child')\n"
    assert not (checkout / ".venv").exists()


def test_install_bundle_ignores_new_root_virtual_environment(tmp_path: Path) -> None:
    checkout, run_dir = _checkout(tmp_path)
    (checkout / ".gitignore").write_text(".venv/\n")
    _git(checkout, "add", ".gitignore")
    _git(checkout, "commit", "-qm", "ignore framework environment")
    _git(checkout, "tag", "-f", "gen/0")
    runner = _harbor_runner_module()
    surface = runner.load_surface_policy(checkout)
    bundle = runner._prepare_bundle(checkout, _ctx(checkout, run_dir), ["target"], surface)
    returned = tmp_path / "returned"
    shutil.copytree(bundle.workspace, returned)
    (returned / "target" / "agent.py").write_text("print('child')\n")
    python = returned / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("container-only runtime\n")

    try:
        changed = runner._install_bundle(checkout, returned, bundle, "gen/0", surface)
    finally:
        shutil.rmtree(bundle.staging, ignore_errors=True)

    assert changed == ["target/agent.py"]
    assert (checkout / "target" / "agent.py").read_text() == "print('child')\n"
    assert not (checkout / ".venv").exists()


def test_agent_output_prefers_preserved_model_response_over_post_submit_message(tmp_path: Path) -> None:
    agent = tmp_path / "trial" / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(json.dumps({"steps": [{"source": "agent", "message": "submit next"}]}))
    (agent / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "tool",
                        "content": "continue",
                        "extra": {
                            "response": {"choices": [{"message": {"content": "analysis and required manifest"}}]}
                        },
                    }
                ]
            }
        )
    )

    assert _harbor_runner_module()._agent_output(tmp_path / "trial") == "analysis and required manifest"
