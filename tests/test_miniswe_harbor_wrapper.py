import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
import types
from pathlib import Path

import pytest
from conftest import git, init_workspace_from_config, write_locked_miniswe_seed

from evolve.config import default_config

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "src" / "evolve" / "integrations" / "harbor" / "miniswe_candidate.py"
CANDIDATE_AGENT = "evolve.integrations.harbor.miniswe_candidate:CandidateMiniSweAgent"


def _known_unbounded_local_run(command, cwd, env, timeout):
    process = subprocess.Popen(command, cwd=cwd, env=env)
    process.communicate(timeout=timeout)
    os.killpg(process.pid, 9)
    stdout, _ = process.communicate()
    return stdout


@pytest.fixture
def adapter_path() -> Path:
    return ADAPTER


def _install_fake_harbor(monkeypatch):
    root = types.ModuleType("harbor")
    agents = types.ModuleType("harbor.agents")
    installed = types.ModuleType("harbor.agents.installed")
    codex = types.ModuleType("harbor.agents.installed.codex")
    mini = types.ModuleType("harbor.agents.installed.mini_swe_agent")

    class Codex:
        CLI_FLAGS = []

        def __init__(self, *args, **kwargs) -> None:
            self._extra_env = dict(kwargs.get("extra_env") or {})

        def _get_env(self, name: str):
            return self._extra_env.get(name) or os.environ.get(name)

        def build_cli_flags(self) -> str:
            return ""

    class MiniSweAgent:
        def __init__(self, *args, **kwargs) -> None:
            self.model_name = kwargs.get("model_name", "openai/test-model")
            self.mcp_servers = []
            self._mini_swe_agent_trajectory_path = "/logs/agent/mini-swe-agent.trajectory.json"

        def _get_env(self, name: str):
            return {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://llm.example/v1",
                "EVOLVE_INSTALL_HTTP_PROXY": "http://proxy.example:8118",
            }.get(name) or os.environ.get(name)

        async def exec_as_agent(self, environment, command: str, env=None):
            environment.commands.append(command)
            environment.envs.append(env or {})
            fail_on = getattr(environment, "fail_on", None)
            should_fail = fail_on and fail_on in command
            if fail_on == "external uv sync":
                should_fail = "uv sync" in command and "--no-install-local" in command
            elif fail_on == "local uv sync":
                should_fail = "uv sync" in command and "--no-install-local" not in command
            if should_fail:
                raise getattr(environment, "failure", RuntimeError("simulated command failure"))

        async def exec_as_root(self, environment, command: str, env=None):
            environment.commands.append(command)
            environment.envs.append(env or {})

    codex.Codex = Codex
    mini.MiniSweAgent = MiniSweAgent
    monkeypatch.setitem(sys.modules, "harbor", root)
    monkeypatch.setitem(sys.modules, "harbor.agents", agents)
    monkeypatch.setitem(sys.modules, "harbor.agents.installed", installed)
    monkeypatch.setitem(sys.modules, "harbor.agents.installed.codex", codex)
    monkeypatch.setitem(sys.modules, "harbor.agents.installed.mini_swe_agent", mini)
    return MiniSweAgent


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("evolve.integrations.harbor.miniswe_candidate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_fake_miniswe_models(monkeypatch):
    minisweagent = types.ModuleType("minisweagent")
    models = types.ModuleType("minisweagent.models")
    litellm_model = types.ModuleType("minisweagent.models.litellm_model")
    litellm_response_model = types.ModuleType("minisweagent.models.litellm_response_model")

    class FakeLitellmModel:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeLitellmResponseModel(FakeLitellmModel):
        pass

    class FakeLitellmModelConfig:
        model_fields = {"model_name", "model_kwargs", "cost_tracking"}

    litellm_model.LitellmModel = FakeLitellmModel
    litellm_model.LitellmModelConfig = FakeLitellmModelConfig
    litellm_response_model.LitellmResponseModel = FakeLitellmResponseModel
    monkeypatch.setitem(sys.modules, "minisweagent", minisweagent)
    monkeypatch.setitem(sys.modules, "minisweagent.models", models)
    monkeypatch.setitem(sys.modules, "minisweagent.models.litellm_model", litellm_model)
    monkeypatch.setitem(sys.modules, "minisweagent.models.litellm_response_model", litellm_response_model)
    return FakeLitellmModel, FakeLitellmResponseModel


def _install_fake_miniswe_local(monkeypatch, run):
    minisweagent = types.ModuleType("minisweagent")
    minisweagent.__path__ = []
    environments = types.ModuleType("minisweagent.environments")
    environments.__path__ = []
    local = types.ModuleType("minisweagent.environments.local")
    local._run = run
    monkeypatch.setitem(sys.modules, "minisweagent", minisweagent)
    monkeypatch.setitem(sys.modules, "minisweagent.environments", environments)
    monkeypatch.setitem(sys.modules, "minisweagent.environments.local", local)
    return local


def _load_model_factory(adapter_path: Path, monkeypatch):
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    model_classes = _install_fake_miniswe_models(monkeypatch)
    namespace = {}
    exec(module.MODEL_SETUP, namespace)
    return module, namespace["build_model"], model_classes


def test_candidate_miniswe_exposes_canonical_name_and_legacy_alias(adapter_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)

    assert module.MiniSweSourceAgent is module.CandidateMiniSweAgent


def test_miniswe_wrapper_forwards_reasoning_effort(adapter_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    monkeypatch.setenv("MINISWE_REASONING_EFFORT", "high")

    source_env = module.MiniSweSourceAgent()._source_env()

    assert source_env["MINISWE_REASONING_EFFORT"] == "high"
    monkeypatch.delenv("MINISWE_REASONING_EFFORT")
    assert "MINISWE_REASONING_EFFORT" not in module.MiniSweSourceAgent()._source_env()


def test_miniswe_wrapper_source_environment_contains_only_strings(adapter_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    agent = module.MiniSweSourceAgent()
    agent.model_name = None

    source_env = agent._source_env()

    assert source_env["MSWEA_MODEL_NAME"] == ""
    assert all(isinstance(value, str) for value in source_env.values())


def test_miniswe_wrapper_omits_session_header_when_unset(adapter_path: Path, monkeypatch) -> None:
    _, build_model, (FakeLitellmModel, FakeLitellmResponseModel) = _load_model_factory(adapter_path, monkeypatch)
    monkeypatch.setenv("MSWEA_MODEL_NAME", "openai/gpt-5.4")
    monkeypatch.setenv("MINISWE_REASONING_EFFORT", "high")
    monkeypatch.delenv("EVOLVE_SESSION_ID", raising=False)

    model = build_model(
        {
            "model": {
                "model_kwargs": {
                    "drop_params": True,
                    "reasoning_effort": "legacy",
                    "reasoning": {"effort": "legacy"},
                }
            }
        }
    )

    assert type(model) is FakeLitellmResponseModel
    assert model.kwargs["model_name"] == "openai/gpt-5.4"
    assert model.kwargs["cost_tracking"] == "ignore_errors"
    kwargs = model.kwargs["model_kwargs"]
    assert kwargs["drop_params"] is True
    assert kwargs["max_output_tokens"] == 64_000
    assert kwargs["reasoning"] == {"effort": "high"}
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["prompt_cache_key"].startswith("evolve-")
    assert "extra_headers" not in kwargs
    assert "reasoning_effort" not in kwargs
    assert "store" not in kwargs


def test_miniswe_wrapper_treats_blank_session_id_as_unset(adapter_path: Path, monkeypatch) -> None:
    _, build_model, _ = _load_model_factory(adapter_path, monkeypatch)
    monkeypatch.setenv("MSWEA_MODEL_NAME", "openai/gpt-5.4")
    monkeypatch.setenv("EVOLVE_SESSION_ID", "   ")

    model = build_model({"model": {}})

    kwargs = model.kwargs["model_kwargs"]
    assert kwargs["prompt_cache_key"].startswith("evolve-")
    assert "extra_headers" not in kwargs


def test_miniswe_wrapper_uses_configured_session_id_literally(adapter_path: Path, monkeypatch) -> None:
    _, build_model, (_, FakeLitellmResponseModel) = _load_model_factory(adapter_path, monkeypatch)
    monkeypatch.setenv("MSWEA_MODEL_NAME", "openai/gpt-5.4")
    monkeypatch.setenv("EVOLVE_SESSION_ID", "experiment-42")

    model = build_model({"model": {}})

    assert type(model) is FakeLitellmResponseModel
    kwargs = model.kwargs["model_kwargs"]
    assert kwargs["prompt_cache_key"] == "experiment-42"
    assert json.loads(kwargs["extra_headers"]["extra"]) == {"session_id": "experiment-42"}


def test_candidate_source_environment_forwards_only_configured_session_id(adapter_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    monkeypatch.delenv("EVOLVE_SESSION_ID", raising=False)

    assert "EVOLVE_SESSION_ID" not in module.CandidateMiniSweAgent()._source_env()

    monkeypatch.setenv("EVOLVE_SESSION_ID", "experiment-42")
    assert module.CandidateMiniSweAgent()._source_env()["EVOLVE_SESSION_ID"] == "experiment-42"


def test_miniswe_wrapper_preserves_explicit_responses_output_budget(adapter_path: Path, monkeypatch) -> None:
    _, build_model, (_, FakeLitellmResponseModel) = _load_model_factory(adapter_path, monkeypatch)
    monkeypatch.setenv("MSWEA_MODEL_NAME", "openai/gpt-5.4")
    monkeypatch.setenv("MINISWE_REASONING_EFFORT", "high")

    model = build_model({"model": {"model_kwargs": {"max_output_tokens": 12_345}}})

    assert type(model) is FakeLitellmResponseModel
    assert model.kwargs["model_kwargs"]["max_output_tokens"] == 12_345


def test_miniswe_wrapper_uses_responses_without_openai_reasoning(adapter_path: Path, monkeypatch) -> None:
    _, build_model, (FakeLitellmModel, FakeLitellmResponseModel) = _load_model_factory(adapter_path, monkeypatch)
    monkeypatch.setenv("MSWEA_MODEL_NAME", "openai/gpt-5.4")
    monkeypatch.delenv("MINISWE_REASONING_EFFORT", raising=False)

    model = build_model({"model": {"model_kwargs": {"drop_params": True}}})

    assert type(model) is FakeLitellmResponseModel
    assert "reasoning" not in model.kwargs["model_kwargs"]
    assert model.kwargs["model_kwargs"]["include"] == ["reasoning.encrypted_content"]


def test_miniswe_wrapper_uses_standard_model_for_non_openai(adapter_path: Path, monkeypatch) -> None:
    _, build_model, (FakeLitellmModel, FakeLitellmResponseModel) = _load_model_factory(adapter_path, monkeypatch)
    monkeypatch.setenv("MSWEA_MODEL_NAME", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("MINISWE_REASONING_EFFORT", "high")

    model = build_model({"model": {"model_kwargs": {"drop_params": True}}})

    assert type(model) is FakeLitellmModel
    assert not isinstance(model, FakeLitellmResponseModel)
    assert model.kwargs["model_kwargs"] == {"drop_params": True}


def test_miniswe_wrapper_rejects_invalid_reasoning_effort(adapter_path: Path, monkeypatch) -> None:
    _, build_model, _ = _load_model_factory(adapter_path, monkeypatch)
    monkeypatch.setenv("MSWEA_MODEL_NAME", "openai/gpt-5.4")
    monkeypatch.setenv("MINISWE_REASONING_EFFORT", "maximum")

    with pytest.raises(ValueError, match=r"none, low, medium, high, xhigh"):
        build_model({"model": {}})


def test_miniswe_wrapper_reuses_model_setup_for_runner_and_preflight(adapter_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)

    assert module.RUNNER.startswith(module.MODEL_SETUP)
    assert module.MODEL_PREFLIGHT.startswith(module.MODEL_SETUP)
    assert "model = build_model(config)" in module.RUNNER
    assert "build_model(config)" in module.MODEL_PREFLIGHT
    compile(module.RUNNER, "<miniswe-runner>", "exec")
    compile(module.MINISWE_PREFLIGHT, "<miniswe-preflight>", "exec")


def test_miniswe_wrapper_timeout_drain_does_not_wait_for_escaped_daemon(
    adapter_path: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    local = _install_fake_miniswe_local(monkeypatch, _known_unbounded_local_run)
    namespace = {}

    exec(module.PIPE_SAFE_LOCAL_ENV_SETUP, namespace)

    assert local._run is namespace["_pipe_safe_run"]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        local._run(
            "setsid sh -c 'sleep 2' & sleep 5",
            str(tmp_path),
            os.environ.copy(),
            0.1,
        )
    assert time.monotonic() - started < 1.5


def test_miniswe_wrapper_preserves_candidate_modified_local_run(
    adapter_path: Path,
    monkeypatch,
) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)

    def candidate_run(command, cwd, env, timeout):
        return subprocess.CompletedProcess(command, 0, stdout="candidate")

    local = _install_fake_miniswe_local(monkeypatch, candidate_run)
    namespace = {}

    exec(module.PIPE_SAFE_LOCAL_ENV_SETUP, namespace)

    assert local._run is candidate_run


def test_miniswe_wrapper_loads_evolved_skills_and_memory_into_system_prompt(
    adapter_path: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    source = tmp_path / "candidate"
    skill = source / "skills" / "artifact-verification" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: artifact-verification\n"
        "description: Verify generated artifacts before submitting\n"
        "---\n\n"
        "Check the exact output path and inspect the resulting file.\n"
    )
    draft = source / "skills" / "_drafts" / "SKILL.md"
    draft.parent.mkdir()
    draft.write_text("---\nname: ignored-draft\ndescription: not runtime context\n---\n")
    memory = source / "memory" / "semantic.jsonl"
    memory.parent.mkdir()
    memory.write_text(
        '{"insight":"Inspect exact output paths before submission","source":"trajectory"}\n'
        '{"insight":"Run the task-specific verifier when available","source":"trajectory"}\n'
    )
    namespace = {}
    exec(module.EVOLVED_CONTEXT_SETUP, namespace)

    context, stats = namespace["_load_evolved_context"](source)
    agent_kwargs = {"system_template": "Base MiniSwe system prompt."}
    applied_stats = namespace["_apply_evolved_context"](agent_kwargs, source)

    assert stats == {"skills_loaded": 1, "memories_loaded": 2}
    assert applied_stats == stats
    assert "## Available Skills" in context
    assert "bundled references, scripts, and assets" in context
    assert "**artifact-verification**: Verify generated artifacts before submitting" in context
    assert str(skill) in context
    assert "ignored-draft" not in context
    assert "## Evolved Memory" in context
    assert "Inspect exact output paths before submission" in context
    assert "Run the task-specific verifier when available" in context
    assert agent_kwargs["system_template"].startswith("Base MiniSwe system prompt.")
    assert context in agent_kwargs["system_template"]
    assert '_apply_evolved_context(agent_kwargs, "/installed-agent/miniswe-source")' in module.RUNNER


def test_miniswe_wrapper_rejects_invalid_evolved_memory(
    adapter_path: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    memory = tmp_path / "candidate" / "memory" / "semantic.jsonl"
    memory.parent.mkdir(parents=True)
    memory.write_text("not-json\n")
    namespace = {}
    exec(module.EVOLVED_CONTEXT_SETUP, namespace)

    with pytest.raises(json.JSONDecodeError):
        namespace["_load_evolved_context"](tmp_path / "candidate")


def test_candidate_archive_normalizes_owner_modes_without_mutating_source(tmp_path: Path) -> None:
    archive_module = importlib.import_module("evolve.integrations.harbor._candidate_source")
    source = tmp_path / "source"
    package = source / "src" / "minisweagent"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    executable = source / "run.sh"
    executable.write_text("#!/bin/sh\n")
    source.chmod(0o700)
    (source / "pyproject.toml").chmod(0o600)
    executable.chmod(0o700)

    with archive_module.candidate_source_archive(source) as archive_path:
        assert stat.S_IMODE(archive_path.stat().st_mode) == 0o644
        with tarfile.open(archive_path) as archive:
            members = {member.name: member for member in archive.getmembers()}
        assert members["."].mode == 0o700
        assert members["./pyproject.toml"].mode == 0o600
        assert members["./src"].mode == 0o700
        assert members["./run.sh"].mode == 0o700
        assert not (members["./pyproject.toml"].mode & stat.S_IWOTH)
        retained_path = archive_path

    assert not retained_path.exists()
    assert stat.S_IMODE(source.stat().st_mode) == 0o700
    assert stat.S_IMODE((source / "pyproject.toml").stat().st_mode) == 0o600


@pytest.mark.parametrize("target", ["/outside", "../../outside"])
def test_candidate_archive_rejects_symlinks_escaping_source(tmp_path: Path, target: str) -> None:
    archive_module = importlib.import_module("evolve.integrations.harbor._candidate_source")
    source = tmp_path / "source"
    source.mkdir()
    (source / "escape").symlink_to(target)

    with pytest.raises(archive_module.UnsafeCandidateSourceError, match="symlink escapes candidate source"):
        with archive_module.candidate_source_archive(source):
            pytest.fail("unsafe source must be rejected before archive creation")


def test_candidate_adapter_classifies_escaping_source_symlink_as_candidate_invalid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_harbor(monkeypatch)
    source = tmp_path / "source"
    (source / "src" / "minisweagent").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (source / "uv.lock").write_text("version = 1\n")
    (source / "escape").symlink_to("../../outside")
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(source))
    module = _load(ADAPTER)

    class Environment:
        async def upload_file(self, source_path, target_path):
            pytest.fail(f"unsafe source must not be uploaded: {source_path} -> {target_path}")

    with pytest.raises(module.EvolveCandidateInvalidError, match="unsafe_source_tree"):
        asyncio.run(module.CandidateMiniSweAgent().install(Environment()))


def test_miniswe_wrapper_subclasses_harbor_miniswe_and_installs_candidate_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base = _install_fake_harbor(monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    (target / "pyproject.toml").write_text("[project]\nname = 'mini-swe-agent'\nversion = '0.test'\n")
    (target / "uv.lock").write_text("version = 1\nrevision = 1\nrequires-python = '>=3.11'\n")
    (target / "src" / "minisweagent").mkdir(parents=True)
    target.chmod(0o700)
    (target / "pyproject.toml").chmod(0o600)
    (target / "uv.lock").chmod(0o600)
    module = _load(ADAPTER)

    class Environment:
        def __init__(self) -> None:
            self.uploads = []
            self.uploaded_directories = []
            self.commands = []
            self.envs = []
            self.archive_modes = {}
            self.uploaded_archive_destination = None

        async def upload_dir(self, source_dir, target_dir):
            self.uploaded_directories.append((Path(source_dir), target_dir))

        async def upload_file(self, source_path, target_path):
            source = Path(source_path)
            self.uploads.append((source, target_path))
            if target_path == "/tmp/evolve-miniswe-source.tar":
                self.uploaded_archive_destination = target_path
                with tarfile.open(source) as archive:
                    self.archive_modes = {member.name: member.mode for member in archive.getmembers()}

    environment = Environment()
    host_uv = tmp_path / "uv"
    host_uv.write_text("uv")
    monkeypatch.setenv("EVOLVE_UV_BINARY", str(host_uv))
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(target))
    monkeypatch.setenv("http_proxy", "http://dependency-proxy.example:8118")
    monkeypatch.setenv("HTTPS_PROXY", "http://dependency-proxy.example:8118")
    monkeypatch.setenv("no_proxy", ".internal.example,llm.example")
    monkeypatch.setenv("NO_PROXY", ".internal.example,llm.example")
    for name in ("UV_CACHE_DIR", "UV_LINK_MODE", "UV_OFFLINE", "UV_PYTHON", "UV_PYTHON_INSTALL_DIR"):
        monkeypatch.delenv(name, raising=False)
    agent = module.MiniSweSourceAgent()
    asyncio.run(agent.install(environment))

    assert issubclass(module.MiniSweSourceAgent, base)
    assert not environment.uploaded_directories
    assert environment.uploaded_archive_destination == "/tmp/evolve-miniswe-source.tar"
    assert environment.uploads[1] == (host_uv, "/tmp/evolve-runtime-uv")
    assert environment.archive_modes["./pyproject.toml"] == 0o600
    assert environment.archive_modes["./src"] == 0o700
    assert not (environment.archive_modes["./pyproject.toml"] & stat.S_IWOTH)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "pyproject.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((target / "uv.lock").stat().st_mode) == 0o600
    joined = "\n".join(environment.commands)
    assert "chmod -R a+rX" not in joined
    extraction = environment.commands[0]
    assert "tar -xf /tmp/evolve-miniswe-source.tar" in extraction
    assert "--no-same-owner" in extraction
    assert "mkdir -p /installed-agent/miniswe-source" in extraction
    assert "rm -f /tmp/evolve-miniswe-source.tar" not in extraction
    bootstrap = environment.commands[1]
    assert "apt-get" not in joined
    assert "apk add" not in joined
    assert "if [ ! -f /tmp/evolve-runtime-uv ]" in bootstrap
    assert "chmod 700 /tmp/evolve-runtime-uv" in bootstrap
    assert "/tmp/evolve-runtime-uv --version > /tmp/evolve-runtime-uv.version" in bootstrap
    assert "command -v uv" not in bootstrap
    assert '"$HOME/.local/bin/uv"' not in joined
    assert "uv tool install" not in joined
    assert "mini-swe-agent --" not in joined
    assert "curl" not in bootstrap
    assert "/tmp/evolve-runtime-uv sync --project /installed-agent/miniswe-source --frozen" in joined
    assert "/installed-agent/miniswe-source/.venv/bin/python" in joined
    assert "uv run --project /installed-agent/miniswe-source" not in joined
    assert "from minisweagent.agents.default import DefaultAgent" in joined
    sync_indices = [index for index, command in enumerate(environment.commands) if "uv sync" in command]
    assert len(sync_indices) == 2
    assert "--no-install-local" in environment.commands[sync_indices[0]]
    assert "--no-install-local" not in environment.commands[sync_indices[1]]
    assert 'export PATH="$HOME/.local/bin:$PATH"' not in environment.commands[sync_indices[0]]
    expected_proxy_env = {
        "http_proxy": "http://dependency-proxy.example:8118",
        "HTTPS_PROXY": "http://dependency-proxy.example:8118",
        "no_proxy": ".internal.example,llm.example",
        "NO_PROXY": ".internal.example,llm.example",
    }
    for sync_index in sync_indices:
        sync_env = environment.envs[sync_index]
        assert sync_env == {
            "UV_CACHE_DIR": "/opt/evolve/uv/cache",
            "UV_LINK_MODE": "copy",
            "UV_OFFLINE": "1",
            "UV_PYTHON_INSTALL_DIR": "/opt/evolve/uv/python",
            **expected_proxy_env,
        }
    model_index = next(
        index for index, command in enumerate(environment.commands) if "EVOLVE_PREFLIGHT_MODEL" in command
    )
    evidence_index = next(
        index for index, command in enumerate(environment.commands) if "evolve-runtime.json" in command
    )
    assert model_index < evidence_index
    assert '"frozen_sync": true' in environment.commands[evidence_index]
    assert '"miniswe_import": true' in environment.commands[evidence_index]
    assert '"model_path_init": true' in environment.commands[evidence_index]
    assert "/tmp/evolve-runtime-uv.version" in environment.commands[evidence_index]
    assert "hashlib.sha256" in environment.commands[evidence_index]
    assert "/tmp/evolve-runtime-uv" in environment.commands[evidence_index]
    assert "skills_loaded" in environment.commands[evidence_index]
    assert "memories_loaded" in environment.commands[evidence_index]
    assert "context_chars" in environment.commands[evidence_index]
    assert environment.commands[evidence_index + 1] == ("rm -f /tmp/evolve-runtime-uv /tmp/evolve-runtime-uv.version")
    assert environment.envs[evidence_index + 1] == {}
    assert expected_proxy_env.items() <= environment.envs[model_index].items()
    assert environment.envs[model_index]["OPENAI_API_KEY"] == "test-key"
    assert environment.envs[model_index]["OPENAI_BASE_URL"] == "https://llm.example/v1"
    assert "unset HTTP_PROXY" not in joined


def test_miniswe_wrapper_runs_candidate_source_api_not_cli(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    monkeypatch.setenv("MINISWE_STEP_LIMIT", "100")
    monkeypatch.setenv("MINISWE_COST_LIMIT", "3.0")
    monkeypatch.setenv("MINISWE_ENV_TIMEOUT", "30")
    target = tmp_path / "target"
    target.mkdir()
    module = _load(ADAPTER)

    class Environment:
        def __init__(self) -> None:
            self.commands = []
            self.envs = []

    environment = Environment()
    agent = module.MiniSweSourceAgent(model_name="openai/test-model")
    asyncio.run(agent.run("Fix the bug.", environment, object()))

    joined = "\n".join(environment.commands)
    assert "mini-swe-agent --" not in joined
    assert "/installed-agent/miniswe-source/.venv/bin/python /tmp/miniswe-source-run.py" in joined
    assert "uv run --project /installed-agent/miniswe-source" not in joined
    assert "get_config_from_spec" in joined
    assert "DefaultAgent" in joined
    assert "from minisweagent.environments.local import LocalEnvironment" in joined
    assert "from minisweagent.models.litellm_model import LitellmModel" in joined
    env = environment.envs[-1]
    assert env["MSWEA_MODEL_NAME"] == "openai/test-model"
    assert env["OPENAI_API_KEY"] == "test-key"
    assert env["OPENAI_BASE_URL"] == "https://llm.example/v1"
    assert env["OPENAI_API_BASE"] == "https://llm.example/v1"
    assert env["MINISWE_STEP_LIMIT"] == "100"
    assert env["MINISWE_COST_LIMIT"] == "3.0"
    assert env["MINISWE_ENV_TIMEOUT"] == "30"
    assert 'agent_kwargs["step_limit"] = int(os.environ.get("MINISWE_STEP_LIMIT"' in module.RUNNER
    assert 'agent_kwargs["cost_limit"] = float(os.environ.get("MINISWE_COST_LIMIT"' in module.RUNNER
    assert 'env_kwargs["timeout"] = int(os.environ.get("MINISWE_ENV_TIMEOUT"' in module.RUNNER
    assert 'env_kwargs["cwd"] = os.environ.get("MINISWE_CWD") or os.getcwd()' in module.RUNNER


@pytest.mark.parametrize(
    ("trial_name", "expects_hint"),
    [
        ("tau3-banking_knowledge-task-067__trial", True),
        ("tau3-airline_knowledge-task-067__trial", False),
        ("terminal-bench-task__trial", False),
    ],
)
def test_miniswe_wrapper_only_adds_tau3_cli_hint_for_banking_tasks(
    adapter_path: Path,
    monkeypatch,
    tmp_path: Path,
    trial_name: str,
    expects_hint: bool,
) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load(adapter_path)
    agent = module.MiniSweSourceAgent()
    agent.logs_dir = tmp_path / trial_name / "agent"
    agent.mcp_servers = [
        types.SimpleNamespace(
            name="tau3-runtime",
            transport="streamable-http",
            url="http://tau3-runtime:8000/mcp",
        )
    ]

    augmented = agent._augment_instruction("Original instruction.")
    generic = (
        "Original instruction.\n\nMCP Servers:\n"
        "The following MCP servers are available for this task.\n"
        "- tau3-runtime: streamable-http transport, url: http://tau3-runtime:8000/mcp\n"
    )

    assert (module.TAU3_BANKING_MCP_CLI_HINT in augmented) is expects_hint
    assert augmented == (f"{generic}\n{module.TAU3_BANKING_MCP_CLI_HINT}\n" if expects_hint else generic)


def test_miniswe_runtime_and_offline_install_forward_download_proxies(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    for name in proxy_names:
        monkeypatch.setenv(name, f"http://inherited-{name.lower()}.example:8118")
    monkeypatch.setenv("no_proxy", ".internal.example,llm.example")
    monkeypatch.setenv("NO_PROXY", ".internal.example,llm.example")

    target = tmp_path / "target"
    target.mkdir()
    module = _load(ADAPTER)

    class Environment:
        def __init__(self) -> None:
            self.commands = []
            self.envs = []

    environment = Environment()
    agent = module.MiniSweSourceAgent(model_name="openai/test-model")
    asyncio.run(agent.run("Fix the bug.", environment, object()))

    runtime_command = environment.commands[-1]
    assert "unset HTTP_PROXY" not in runtime_command
    for name in (*proxy_names, "no_proxy", "NO_PROXY"):
        assert environment.envs[-1][name] == os.environ[name]

    install_env = agent._install_env()
    for name in (*proxy_names, "no_proxy", "NO_PROXY"):
        assert install_env[name] == os.environ[name]
    assert install_env["UV_OFFLINE"] == "1"


@pytest.mark.parametrize(
    ("fragment", "code", "failure"),
    [
        ("local uv sync", "local_project_sync_failed", RuntimeError("failed building candidate")),
        ("EVOLVE_PREFLIGHT_MINISWE", "miniswe_import_failed", ImportError("minisweagent")),
        ("EVOLVE_PREFLIGHT_MODEL", "model_path_import_failed", ModuleNotFoundError("fastapi")),
    ],
    ids=["litellm-build-failure", "miniswe-import-failure", "missing-fastapi"],
)
def test_miniswe_install_classifies_candidate_phase_failures(
    tmp_path: Path,
    monkeypatch,
    fragment: str,
    code: str,
    failure: Exception,
) -> None:
    _install_fake_harbor(monkeypatch)
    target = write_locked_miniswe_seed(tmp_path / "target")
    module = _load(ADAPTER)
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(target))

    class Environment:
        def __init__(self) -> None:
            self.commands = []
            self.envs = []
            self.uploads = []
            self.fail_on = fragment
            self.failure = failure

        async def upload_dir(self, source_dir, target_dir):
            self.uploads.append((Path(source_dir), target_dir))

        async def upload_file(self, source_path, target_path):
            self.uploads.append((Path(source_path), target_path))

    with pytest.raises(RuntimeError, match=f"EVOLVE_CANDIDATE_INVALID: {code}"):
        asyncio.run(module.MiniSweSourceAgent().install(Environment()))


def test_miniswe_external_dependency_sync_is_infrastructure_owned(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    target = write_locked_miniswe_seed(tmp_path / "target")
    module = _load(ADAPTER)
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(target))

    class Environment:
        def __init__(self) -> None:
            self.commands = []
            self.envs = []
            self.uploads = []
            self.fail_on = "external uv sync"
            self.failure = RuntimeError("offline cache miss")

        async def upload_dir(self, source_dir, target_dir):
            self.uploads.append((Path(source_dir), target_dir))

        async def upload_file(self, source_path, target_path):
            self.uploads.append((Path(source_path), target_path))

    with pytest.raises(module.EvolveRuntimeInfrastructureError) as raised:
        asyncio.run(module.MiniSweSourceAgent().install(Environment()))

    assert str(raised.value) == ("EVOLVE_RUNTIME_INFRASTRUCTURE: external_dependency_sync_failed: offline cache miss")


def test_miniswe_local_sync_offline_cache_miss_is_infrastructure_owned(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    target = write_locked_miniswe_seed(tmp_path / "target")
    module = _load(ADAPTER)
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(target))

    class Environment:
        def __init__(self) -> None:
            self.commands = []
            self.envs = []
            self.uploads = []
            self.fail_on = "local uv sync"
            self.failure = RuntimeError(
                "Network connectivity is disabled, but the requested data wasn't found in the cache"
            )

        async def upload_file(self, source_path, target_path):
            self.uploads.append((Path(source_path), target_path))

    with pytest.raises(module.EvolveRuntimeInfrastructureError) as raised:
        asyncio.run(module.MiniSweSourceAgent().install(Environment()))

    assert str(raised.value).startswith("EVOLVE_RUNTIME_INFRASTRUCTURE: local_project_sync_failed:")


def test_miniswe_runtime_requires_a_host_uv_without_touching_task_image(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    target = write_locked_miniswe_seed(tmp_path / "target")
    module = _load(ADAPTER)
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(target))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.delenv("EVOLVE_UV_BINARY", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    class Environment:
        def __init__(self) -> None:
            self.commands = []
            self.envs = []
            self.uploads = []

        async def upload_file(self, source_path, target_path):
            self.uploads.append((Path(source_path), target_path))

    environment = Environment()
    with pytest.raises(
        module.EvolveRuntimeInfrastructureError,
        match="EVOLVE_RUNTIME_INFRASTRUCTURE: uv_bootstrap_failed: host uv binary missing",
    ):
        asyncio.run(module.MiniSweSourceAgent().install(environment))

    assert environment.commands == []
    assert environment.uploads == []


def test_miniswe_install_rejects_missing_lock_before_upload(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    target = write_locked_miniswe_seed(tmp_path / "target")
    (target / "uv.lock").unlink()
    module = _load(ADAPTER)
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(target))

    class Environment:
        uploads = []

    with pytest.raises(RuntimeError, match="EVOLVE_CANDIDATE_INVALID: lock_missing"):
        asyncio.run(module.MiniSweSourceAgent().install(Environment()))

    assert Environment.uploads == []


def test_init_with_local_miniswe_seed_writes_protected_harbor_adapter(tmp_path: Path) -> None:
    from evolve.workspace import InitOptions

    seed = write_locked_miniswe_seed(tmp_path / "miniswe")
    (seed / ".gitignore").write_text("uv.lock\n")
    expected_lock = (seed / "uv.lock").read_bytes()
    workspace = tmp_path / "workspace"
    config = default_config("hill_climb", workspace.name)
    config["evaluator"]["agent"] = CANDIDATE_AGENT

    init_workspace_from_config(InitOptions(workspace=workspace, seed=str(seed)), config)

    wrapper = workspace / ".evolve" / "evolve" / "integrations" / "harbor" / "miniswe_candidate.py"
    assert wrapper.exists()
    assert "class CandidateMiniSweAgent(MiniSweAgent):" in wrapper.read_text()
    assert not (workspace / "evolve_harbor_adapter").exists()
    assert not (workspace / "evolve_harbor_agent").exists()
    assert (workspace / "target" / "uv.lock").read_bytes() == expected_lock
    assert git(workspace, "ls-files", "target/uv.lock") == "target/uv.lock"


def test_init_tracks_seed_lockfile_even_when_seed_gitignore_excludes_it(tmp_path: Path) -> None:
    from evolve.workspace import InitOptions

    seed = write_locked_miniswe_seed(tmp_path / "miniswe")
    (seed / ".gitignore").write_text("uv.lock\n")
    workspace = tmp_path / "workspace"
    config = default_config("hill_climb", workspace.name)
    config["evaluator"]["agent"] = CANDIDATE_AGENT

    init_workspace_from_config(InitOptions(workspace=workspace, seed=str(seed)), config)

    git(workspace, "cat-file", "-e", "gen/0:target/uv.lock")


def test_init_rejects_unlocked_local_miniswe_seed_before_workspace_creation(
    tmp_path: Path,
) -> None:
    from evolve.workspace import InitOptions, init_workspace

    seed = write_locked_miniswe_seed(tmp_path / "miniswe")
    (seed / "uv.lock").unlink()
    workspace = tmp_path / "workspace"

    with pytest.raises(
        ValueError,
        match=r"MiniSWE candidate.*prepared target.*uv\.lock",
    ):
        init_workspace(InitOptions(workspace=workspace, recipe="hill_climb", seed=str(seed)))

    assert not workspace.exists()


def test_init_with_local_miniswe_seed_excludes_virtualenv_cache(tmp_path: Path) -> None:
    from evolve.workspace import InitOptions

    seed = write_locked_miniswe_seed(tmp_path / "miniswe")
    (seed / ".venv" / "bin").mkdir(parents=True)
    (seed / ".venv" / "bin" / "python").write_text("not source\n")
    (seed / ".pytest_cache").mkdir()
    (seed / ".env").write_text("OPENAI_API_KEY=must-not-copy\n")
    (seed / ".env.local").write_text("HTTPS_PROXY=http://user:pass@proxy.example\n")
    (seed / "src" / "minisweagent" / ".env.test").write_text("TOKEN=must-not-copy\n")
    workspace = tmp_path / "workspace"
    config = default_config("hill_climb", workspace.name)
    config["evaluator"]["agent"] = CANDIDATE_AGENT

    init_workspace_from_config(InitOptions(workspace=workspace, seed=str(seed)), config)

    assert (workspace / "target" / "src" / "minisweagent" / "__init__.py").exists()
    assert not (workspace / "target" / ".venv").exists()
    assert not (workspace / "target" / ".pytest_cache").exists()
    assert not (workspace / "target" / ".env").exists()
    assert not (workspace / "target" / ".env.local").exists()
    assert not (workspace / "target" / "src" / "minisweagent" / ".env.test").exists()
