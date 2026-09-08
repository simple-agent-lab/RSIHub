from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import run_evolve


class FakeCliFlag:
    def __init__(self, kwarg: str, **kwargs: Any) -> None:
        self.kwarg = kwarg
        self.kwargs = kwargs


class FakeCodex:
    CLI_FLAGS: list[FakeCliFlag] = []

    def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any) -> None:
        self.logs_dir = logs_dir
        self.model_name = model_name
        self.kwargs = kwargs
        self._extra_env = dict(kwargs.get("extra_env") or {})
        self.base_setup_called = False
        self.agent_commands: list[str] = []

    def _get_env(self, name: str) -> str | None:
        return self._extra_env.get(name)

    def build_cli_flags(self) -> str:
        return ""

    def render_instruction(self, instruction: str) -> str:
        return instruction

    def _build_register_skills_command(self) -> str | None:
        return "register-base-skills"

    async def exec_as_agent(
        self,
        environment: object,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> str:
        del environment, env, cwd, timeout_sec
        self.agent_commands.append(command)
        return command

    async def setup(self, environment: object) -> None:
        self.base_setup_called = True


class FakeBaseEnvironment:
    pass


class RecordingEnvironment:
    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str]] = []

    async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
        self.uploads.append((source_dir, target_dir))


def _install_fake_harbor(monkeypatch) -> None:
    modules = {
        "harbor": ModuleType("harbor"),
        "harbor.agents": ModuleType("harbor.agents"),
        "harbor.agents.installed": ModuleType("harbor.agents.installed"),
        "harbor.agents.installed.base": ModuleType("harbor.agents.installed.base"),
        "harbor.agents.installed.codex": ModuleType("harbor.agents.installed.codex"),
        "harbor.environments": ModuleType("harbor.environments"),
        "harbor.environments.base": ModuleType("harbor.environments.base"),
    }
    modules["harbor.agents.installed.base"].CliFlag = FakeCliFlag
    modules["harbor.agents.installed.codex"].Codex = FakeCodex
    modules["harbor.environments.base"].BaseEnvironment = FakeBaseEnvironment
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_target_agent(path: Path):
    spec = importlib.util.spec_from_file_location("evolve_test_builtin_codex", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_candidate_target(target: Path, *, model: str) -> Path:
    target.mkdir(parents=True)
    (target / "prompt.md").write_text(f"Prompt for {model}.\n")
    (target / "codex.toml").write_text(
        f"""[codex]
model = "{model}"

[skills]
enabled = true

[compaction]
override_defaults = false
"""
    )
    skills = target / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text(f"Skill for {model}.\n")
    return skills


def test_builtin_codex_wrapper_injects_skills_and_opt_in_compaction(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "experiment"
    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "aevolve",
        "--seed",
        "builtin-codex",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(tmp_path / "home")},
    )
    assert result.returncode == 0, result.stderr
    assert not (workspace / "target" / "__pycache__").exists()
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(workspace / "target" / "agent.py")

    agent = module.HarborAgent(logs_dir=tmp_path / "logs")
    assert agent.model_name == "gpt-5.4"
    assert agent.kwargs["version"] == "0.143.0"
    assert agent.kwargs["skills_dir"] == "/tmp/evolve-target-skills"
    assert "auto_compact_token_limit" not in agent.kwargs
    assert {flag.kwarg for flag in agent.CLI_FLAGS} >= {
        "auto_compact_token_limit",
        "auto_compact_token_limit_scope",
        "tool_output_token_limit",
    }

    environment = RecordingEnvironment()
    asyncio.run(agent.setup(environment))
    assert agent.base_setup_called is True
    assert agent.agent_commands == [
        "mkdir -p /tmp/evolve-target-skills "
        "/tmp/evolve-target-marketplace/.agents /tmp/evolve-target-marketplace/plugins"
    ]
    assert environment.uploads == [
        (workspace / "target" / "skills", "/tmp/evolve-target-skills"),
        (workspace / "target" / ".agents", "/tmp/evolve-target-marketplace/.agents"),
        (workspace / "target" / "plugins", "/tmp/evolve-target-marketplace/plugins"),
    ]

    registration = agent._build_register_skills_command()
    assert registration is not None
    assert "register-base-skills" in registration
    assert registration.count(". ~/.nvm/nvm.sh") == 2
    assert "codex plugin marketplace add /tmp/evolve-target-marketplace" in registration
    assert "codex plugin add evolve-target@evolve-target" in registration
    rewritten = asyncio.run(agent.exec_as_agent(environment, "codex exec --json -- task"))
    assert rewritten.startswith("export EVOLVE_CODEX_AGENT_RUN_ID=")
    assert rewritten.endswith("codex exec --dangerously-bypass-hook-trust --json -- task")
    unchanged = asyncio.run(agent.exec_as_agent(environment, "codex plugin list"))
    assert unchanged == "codex plugin list"

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    auth_path = tmp_path / "home" / ".codex" / "auth.json"
    with pytest.raises(ValueError, match="auth.json does not exist"):
        agent._resolve_auth_json_path()
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text("{}\n")
    assert agent._resolve_auth_json_path() == auth_path
    custom_home = tmp_path / "custom-codex-home"
    custom_home.mkdir()
    (custom_home / "auth.json").write_text("{}\n")
    agent._get_env = lambda name: str(custom_home) if name == "CODEX_HOME" else None
    assert agent._resolve_auth_json_path() == custom_home / "auth.json"

    api_agent = module.HarborAgent(
        logs_dir=tmp_path / "api-logs",
        extra_env={
            "OPENAI_BASE_URL": "http://bridge.example/v1",
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert api_agent._auth_mode() == "api"
    assert api_agent._resolve_auth_json_path() is None
    api_flags = api_agent.build_cli_flags()
    assert 'model_provider="evolve_http"' in api_flags
    assert 'forced_login_method="api"' in api_flags
    assert 'model_providers.evolve_http.base_url="http://bridge.example/v1"' in api_flags
    assert 'model_providers.evolve_http.wire_api="responses"' in api_flags
    assert 'model_providers.evolve_http.env_http_headers={"api-key"="OPENAI_API_KEY"}' in api_flags
    assert "model_providers.evolve_http.supports_websockets=false" in api_flags

    forced_auth_agent = module.HarborAgent(
        logs_dir=tmp_path / "forced-auth-logs",
        extra_env={
            "CODEX_FORCE_AUTH_JSON": "1",
            "CODEX_HOME": str(custom_home),
            "OPENAI_BASE_URL": "http://bridge.example/v1",
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert forced_auth_agent._auth_mode() == "auth_json"
    assert forced_auth_agent._resolve_auth_json_path() == custom_home / "auth.json"
    assert 'model_provider="evolve_http"' not in forced_auth_agent.build_cli_flags()

    explicit_api_agent = module.HarborAgent(
        logs_dir=tmp_path / "explicit-api-logs",
        extra_env={
            "EVOLVE_CODEX_AUTH_MODE": "api",
            "OPENAI_API_BASE": "http://bridge.example/v1",
            "OPENAI_API_KEY": "test-key",
        },
    )
    assert explicit_api_agent._auth_mode() == "api"
    assert explicit_api_agent._resolve_auth_json_path() is None

    config_path = workspace / "target" / "codex.toml"
    config_path.write_text(config_path.read_text().replace("override_defaults = false", "override_defaults = true"))
    compacting_agent = module.HarborAgent(logs_dir=tmp_path / "logs-2")
    assert compacting_agent.kwargs["auto_compact_token_limit"] == 100000
    assert compacting_agent.kwargs["auto_compact_token_limit_scope"] == "total"
    assert compacting_agent.kwargs["tool_output_token_limit"] == 12000


def test_builtin_codex_wrapper_cleans_processes_after_cancellation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(Path(__file__).parents[1] / "seeds" / "codex" / "agent.py")
    calls: list[tuple[str, int | None]] = []

    async def cancelling_exec(
        self,
        environment: object,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> str:
        del self, environment, env, cwd
        calls.append((command, timeout_sec))
        if len(calls) == 1:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                raise
        return command

    monkeypatch.setattr(FakeCodex, "exec_as_agent", cancelling_exec)
    agent = module.HarborAgent(logs_dir=tmp_path / "logs")

    with pytest.raises(TimeoutError):
        asyncio.run(
            asyncio.wait_for(
                agent.exec_as_agent(object(), "codex exec --json -- task"),
                timeout=0.01,
            )
        )

    launch, cleanup = calls
    assert launch[0].startswith("export EVOLVE_CODEX_AGENT_RUN_ID=")
    run_id = launch[0].split(";", 1)[0].split("=", 1)[1]
    assert f"marker=EVOLVE_CODEX_AGENT_RUN_ID={run_id}" in cleanup[0]
    assert "/proc/[0-9]*/environ" in cleanup[0]
    assert "kill -TERM $pids" in cleanup[0]
    assert "kill -KILL $pids" in cleanup[0]
    assert cleanup[1] == 10


def test_builtin_codex_seed_contains_valid_plugin_layout() -> None:
    root = Path(__file__).resolve().parents[1] / "seeds" / "codex"
    marketplace = __import__("json").loads((root / ".agents" / "plugins" / "marketplace.json").read_text())
    plugin = root / "plugins" / "evolve-target"
    manifest = __import__("json").loads((plugin / ".codex-plugin" / "plugin.json").read_text())
    hooks = __import__("json").loads((plugin / "hooks" / "hooks.json").read_text())

    assert marketplace["name"] == "evolve-target"
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/evolve-target"
    assert manifest["name"] == plugin.name
    assert "hooks" not in manifest
    assert hooks["hooks"]["SessionStart"][0]["hooks"][0]["type"] == "command"
    assert (plugin / "hooks" / "session_start.py").is_file()
    assert "EVOLVE_PLUGIN_SESSION_CONTEXT_V1" in (plugin / "context.md").read_text()


def test_paper_poster_codex_seed_uses_cli_default_unless_model_is_explicit(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(
        Path(__file__).parents[1] / "evals" / "skills" / "make-paper-poster" / "seed" / "agent.py"
    )

    default_agent = module.HarborAgent(logs_dir=tmp_path / "default")
    assert default_agent.model_name is None
    command = asyncio.run(
        default_agent.exec_as_agent(
            RecordingEnvironment(),
            f"codex exec --model {module.DEFAULT_MODEL_SENTINEL} --json -- task",
        )
    )
    assert "--model" not in command

    pinned_agent = module.HarborAgent(logs_dir=tmp_path / "pinned", model_name="gpt-pinned")
    assert pinned_agent.model_name == "gpt-pinned"


def test_builtin_codex_wrapper_uses_candidate_source_from_extra_env(tmp_path: Path, monkeypatch) -> None:
    original_target = tmp_path / "original" / "target"
    original_target.mkdir(parents=True)
    source_agent = Path(__file__).resolve().parents[1] / "seeds" / "codex" / "agent.py"
    (original_target / "agent.py").write_text(source_agent.read_text())
    (original_target / "prompt.md").write_text("Original prompt.\n")
    (original_target / "codex.toml").write_text(
        """[codex]
model = "original-model"
reasoning_effort = "low"

[skills]
enabled = true

[compaction]
override_defaults = false
"""
    )
    (original_target / "skills").mkdir()

    candidate_target = tmp_path / "candidate" / "target"
    candidate_target.mkdir(parents=True)
    (candidate_target / "prompt.md").write_text("Candidate prompt.\n")
    candidate_config = candidate_target / "codex.toml"
    candidate_config.write_text(
        """[codex]
model = "candidate-model"
reasoning_effort = "medium"

[skills]
enabled = true

[compaction]
override_defaults = false
"""
    )
    candidate_skills = candidate_target / "skills"
    candidate_skills.mkdir()
    (candidate_skills / "SKILL.md").write_text("Candidate skill.\n")

    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(original_target / "agent.py")
    extra_env = {"EVOLVE_CANDIDATE_SOURCE": str(candidate_target)}
    agent = module.HarborAgent(logs_dir=tmp_path / "logs", extra_env=extra_env)

    assert agent.model_name == "candidate-model"
    assert agent.kwargs["reasoning_effort"] == "medium"
    assert agent.kwargs["prompt_template_path"] == candidate_target / "prompt.md"
    environment = RecordingEnvironment()
    asyncio.run(agent.setup(environment))
    assert environment.uploads == [
        (candidate_skills, "/tmp/evolve-target-skills"),
    ]

    candidate_config.write_text(candidate_config.read_text().replace("enabled = true", "enabled = false"))
    disabled_agent = module.HarborAgent(logs_dir=tmp_path / "disabled-logs", extra_env=extra_env)
    assert disabled_agent.kwargs["skills_dir"] is None
    disabled_environment = RecordingEnvironment()
    asyncio.run(disabled_agent.setup(disabled_environment))
    assert disabled_environment.uploads == []


@pytest.mark.parametrize("extra_env", [{}, {"EVOLVE_CANDIDATE_SOURCE": ""}])
def test_builtin_codex_wrapper_does_not_inherit_ambient_candidate_source(
    tmp_path: Path,
    monkeypatch,
    extra_env: dict[str, str],
) -> None:
    ambient_target = tmp_path / "ambient" / "target"
    _write_candidate_target(ambient_target, model="ambient-model")
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(ambient_target))
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(Path(__file__).resolve().parents[1] / "seeds" / "codex" / "agent.py")

    agent = module.HarborAgent(logs_dir=tmp_path / "logs", extra_env=extra_env)

    assert agent.model_name == "gpt-5.4"
    assert agent.kwargs["prompt_template_path"] == module.MODULE_ROOT / "prompt.md"


def test_builtin_codex_wrapper_rejects_malformed_candidate_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ambient_target = tmp_path / "ambient" / "target"
    _write_candidate_target(ambient_target, model="ambient-model")
    monkeypatch.setenv("EVOLVE_CANDIDATE_SOURCE", str(ambient_target))
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(Path(__file__).resolve().parents[1] / "seeds" / "codex" / "agent.py")

    assert module._target_root("EVOLVE_CANDIDATE_SOURCE=/tmp/not-a-mapping") == module.MODULE_ROOT


def test_builtin_codex_wrapper_rejects_non_string_candidate_before_reading_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_target = tmp_path / "original" / "target"
    original_target.mkdir(parents=True)
    source_agent = Path(__file__).resolve().parents[1] / "seeds" / "codex" / "agent.py"
    (original_target / "agent.py").write_text(source_agent.read_text())
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(original_target / "agent.py")

    with pytest.raises(TypeError, match="EVOLVE_CANDIDATE_SOURCE must be a string"):
        module.HarborAgent(
            logs_dir=tmp_path / "logs",
            extra_env={"EVOLVE_CANDIDATE_SOURCE": 42},
        )


def test_builtin_codex_wrapper_isolates_candidate_root_per_instance(tmp_path: Path, monkeypatch) -> None:
    first_target = tmp_path / "first" / "target"
    first_skills = _write_candidate_target(first_target, model="first-model")
    second_target = tmp_path / "second" / "target"
    second_skills = _write_candidate_target(second_target, model="second-model")
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(Path(__file__).resolve().parents[1] / "seeds" / "codex" / "agent.py")

    first = module.HarborAgent(
        logs_dir=tmp_path / "first-logs",
        extra_env={"EVOLVE_CANDIDATE_SOURCE": str(first_target)},
    )
    second = module.HarborAgent(
        logs_dir=tmp_path / "second-logs",
        extra_env={"EVOLVE_CANDIDATE_SOURCE": str(second_target)},
    )

    assert first.model_name == "first-model"
    assert first.kwargs["prompt_template_path"] == first_target / "prompt.md"
    assert second.model_name == "second-model"
    assert second.kwargs["prompt_template_path"] == second_target / "prompt.md"
    first_environment = RecordingEnvironment()
    second_environment = RecordingEnvironment()
    asyncio.run(first.setup(first_environment))
    asyncio.run(second.setup(second_environment))
    assert first_environment.uploads == [(first_skills, "/tmp/evolve-target-skills")]
    assert second_environment.uploads == [(second_skills, "/tmp/evolve-target-skills")]


def test_codex_deadline_counts_setup_before_model_call(tmp_path, monkeypatch):
    import json

    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(Path(__file__).resolve().parents[1] / "seeds/codex/agent.py")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "agent": {"override_timeout_sec": 100},
                "timeout_multiplier": 1,
            }
        )
    )
    agent = module.HarborAgent(logs_dir=tmp_path / "agent")
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    prompt = agent.render_instruction("Solve the task")
    assert "100 seconds" in prompt
    assert "EVOLVE_AGENT_DEADLINE_UNIX" in prompt
    monkeypatch.setattr(module.time, "time", lambda: 1030)
    command = asyncio.run(agent.exec_as_agent(object(), "codex exec -- task"))
    assert "EVOLVE_AGENT_DEADLINE_UNIX=1100.000000" in command
    assert "1130" not in command
    assert asyncio.run(agent.exec_as_agent(object(), "setup")) == "setup"
    (tmp_path / "config.json").unlink()
    assert "time limit: unavailable" in agent.render_instruction("Another task")
    command = asyncio.run(agent.exec_as_agent(object(), "codex exec -- task"))
    assert "unset EVOLVE_AGENT_DEADLINE_UNIX" in command
    assert "1100.000000" not in command


def test_codex_missing_budget_does_not_invent_a_deadline(tmp_path, monkeypatch):
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(Path(__file__).resolve().parents[1] / "seeds/codex/agent.py")
    agent = module.HarborAgent(logs_dir=tmp_path / "agent")
    assert "time limit: unavailable" in agent.render_instruction("Task")
    command = asyncio.run(agent.exec_as_agent(object(), "codex exec -- task"))
    assert "EVOLVE_AGENT_DEADLINE_UNIX=" not in command


def test_explicit_evaluator_version_overrides_codex_seed_default(tmp_path: Path, monkeypatch) -> None:
    _install_fake_harbor(monkeypatch)
    module = _load_target_agent(Path(__file__).resolve().parents[1] / "seeds/codex/agent.py")
    agent = module.HarborAgent(logs_dir=tmp_path / "logs", version="0.150.0")
    assert agent.kwargs["version"] == "0.150.0"
