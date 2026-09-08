"""Evolvable Harbor wrapper around the installed Codex CLI agent."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
import tomllib
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import CliFlag
from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment

from evolve.integrations.harbor._time_budget import read_agent_time_budget

MODULE_ROOT = Path(__file__).resolve().parent
REMOTE_SKILLS_DIR = "/tmp/evolve-target-skills"
REMOTE_PLUGIN_MARKETPLACE = "/tmp/evolve-target-marketplace"
PLUGIN_NAME = "evolve-target"
PLUGIN_MARKETPLACE = "evolve-target"
AUTH_MODE_ENV = "EVOLVE_CODEX_AUTH_MODE"
AUTH_MODES = ("auto", "api", "auth_json")
AGENT_RUN_ENV = "EVOLVE_CODEX_AGENT_RUN_ID"


def _target_root(extra_env: object) -> Path:
    if not isinstance(extra_env, Mapping) or "EVOLVE_CANDIDATE_SOURCE" not in extra_env:
        return MODULE_ROOT
    candidate_source = extra_env["EVOLVE_CANDIDATE_SOURCE"]
    if candidate_source == "":
        return MODULE_ROOT
    if not isinstance(candidate_source, str):
        raise TypeError("EVOLVE_CANDIDATE_SOURCE must be a string")
    return Path(candidate_source).expanduser().resolve()


def _settings(target_root: Path) -> dict[str, Any]:
    with (target_root / "codex.toml").open("rb") as config_file:
        return tomllib.load(config_file)


def _table(settings: dict[str, Any], name: str) -> dict[str, Any]:
    value = settings.get(name)
    return value if isinstance(value, dict) else {}


class HarborAgent(Codex):
    """Codex with candidate-owned prompt, skills, and context policy."""

    CLI_FLAGS = [
        *Codex.CLI_FLAGS,
        CliFlag(
            "auto_compact_token_limit",
            cli="-c",
            type="int",
            format="-c model_auto_compact_token_limit={value}",
        ),
        CliFlag(
            "auto_compact_token_limit_scope",
            cli="-c",
            type="enum",
            choices=["total", "body_after_prefix"],
            format="-c model_auto_compact_token_limit_scope={value}",
        ),
        CliFlag(
            "tool_output_token_limit",
            cli="-c",
            type="int",
            format="-c tool_output_token_limit={value}",
        ),
    ]

    def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any) -> None:
        self._budget_logs_dir = logs_dir
        self._deadline_unix: float | None = None
        self._target_root = _target_root(kwargs.get("extra_env"))
        settings = _settings(self._target_root)
        codex = _table(settings, "codex")
        skills = _table(settings, "skills")
        compaction = _table(settings, "compaction")

        self._skills_enabled = bool(skills.get("enabled", True))
        configured_model = str(codex.get("model") or "gpt-5.4")
        resolved_model = (
            model_name or os.environ.get("EVOLVE_HARBOR_MODEL") or os.environ.get("OPENAI_MODEL") or configured_model
        )

        kwargs.setdefault("version", str(codex.get("version") or "") or None)
        kwargs.setdefault("reasoning_effort", codex.get("reasoning_effort", "high"))
        kwargs.setdefault("reasoning_summary", codex.get("reasoning_summary", "auto"))
        kwargs.setdefault("web_search", codex.get("web_search", "disabled"))
        kwargs.setdefault("prompt_template_path", self._target_root / "prompt.md")
        kwargs.setdefault("skills_dir", REMOTE_SKILLS_DIR if self._skills_enabled else None)

        if compaction.get("override_defaults", False):
            kwargs.setdefault("auto_compact_token_limit", compaction.get("auto_compact_token_limit"))
            kwargs.setdefault(
                "auto_compact_token_limit_scope",
                compaction.get("auto_compact_token_limit_scope", "total"),
            )
            kwargs.setdefault("tool_output_token_limit", compaction.get("tool_output_token_limit"))

        super().__init__(logs_dir=logs_dir, model_name=resolved_model, **kwargs)

    def render_instruction(self, instruction: str) -> str:
        self._deadline_unix = None
        rendered = super().render_instruction(instruction)
        budget = read_agent_time_budget(self._budget_logs_dir)
        if budget["status"] != "available":
            return rendered + "\n\nAgent execution time limit: unavailable; do not assume an unlimited budget."
        self._deadline_unix = time.time() + budget["limit_s"]
        return rendered + (
            f"\n\nAgent execution budget: {budget['limit_s']:g} seconds, including setup inside this run, "
            "reasoning, tools, and final checks. Harbor enforces the cutoff externally. "
            "EVOLVE_AGENT_DEADLINE_UNIX is an approximate Unix deadline measured at agent entry; "
            "setup before the first model call consumes this same budget. "
            "Read remaining seconds with: python3 -c 'import os,time; "
            'print(max(0, float(os.environ["EVOLVE_AGENT_DEADLINE_UNIX"])-time.time()))\'. '
            "This reports time, does not extend it, and is not proof the task is complete."
        )

    def _auth_mode(self) -> str:
        configured = (self._get_env(AUTH_MODE_ENV) or "auto").strip().lower()
        if configured not in AUTH_MODES:
            raise ValueError(f"{AUTH_MODE_ENV} must be one of: {', '.join(AUTH_MODES)}")
        if configured != "auto":
            return configured
        if self._get_env("CODEX_AUTH_JSON_PATH") or _truthy(self._get_env("CODEX_FORCE_AUTH_JSON")):
            return "auth_json"
        if self._get_env("OPENAI_BASE_URL") or self._get_env("OPENAI_API_BASE"):
            return "api"
        return "auth_json"

    def _api_base_url(self) -> str:
        base_url = self._get_env("OPENAI_BASE_URL") or self._get_env("OPENAI_API_BASE")
        if not base_url:
            raise ValueError(f"{AUTH_MODE_ENV}=api requires OPENAI_BASE_URL or OPENAI_API_BASE")
        if not self._get_env("OPENAI_API_KEY"):
            raise ValueError(f"{AUTH_MODE_ENV}=api requires OPENAI_API_KEY")
        return base_url

    def build_cli_flags(self) -> str:
        parts = [super().build_cli_flags()]
        if self._auth_mode() == "api":
            base_url = self._api_base_url()
            configs = [
                'model_provider="evolve_http"',
                'forced_login_method="api"',
                'model_providers.evolve_http.name="RSIHub HTTP Responses"',
                f'model_providers.evolve_http.base_url="{base_url}"',
                'model_providers.evolve_http.env_key="OPENAI_API_KEY"',
                'model_providers.evolve_http.wire_api="responses"',
                'model_providers.evolve_http.env_http_headers={"api-key"="OPENAI_API_KEY"}',
                "model_providers.evolve_http.supports_websockets=false",
            ]
            parts.extend(f"-c {shlex.quote(config)}" for config in configs)
        return " ".join(part for part in parts if part)

    def _resolve_auth_json_path(self) -> Path | None:
        if self._auth_mode() == "api":
            self._api_base_url()
            return None
        configured = self._get_env("CODEX_AUTH_JSON_PATH")
        codex_home = Path(self._get_env("CODEX_HOME") or Path.home() / ".codex")
        auth_path = Path(configured).expanduser() if configured else codex_home / "auth.json"
        if not auth_path.is_file():
            raise ValueError(f"Codex auth.json does not exist: {auth_path}")
        return auth_path

    def _build_register_skills_command(self) -> str | None:
        commands: list[str] = []
        base = super()._build_register_skills_command()
        if base:
            commands.append(base)
        marketplace = shlex.quote(REMOTE_PLUGIN_MARKETPLACE)
        load_codex = "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi;"
        commands.extend(
            [
                (f'{load_codex} CODEX_HOME="$CODEX_HOME" codex plugin marketplace add {marketplace} >/dev/null'),
                (
                    f'{load_codex} CODEX_HOME="$CODEX_HOME" codex plugin add '
                    f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE} >/dev/null"
                ),
            ]
        )
        return " && ".join(commands)

    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        marker = "codex exec "
        if marker in command and "--dangerously-bypass-hook-trust" not in command:
            command = command.replace(marker, marker + "--dangerously-bypass-hook-trust ", 1)
        if marker not in command:
            return await super().exec_as_agent(
                environment,
                command=command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )

        run_id = uuid.uuid4().hex
        if self._deadline_unix is not None:
            command = f"export EVOLVE_AGENT_DEADLINE_UNIX={self._deadline_unix:.6f}; {command}"
        else:
            command = f"unset EVOLVE_AGENT_DEADLINE_UNIX; {command}"
        command = f"export {AGENT_RUN_ENV}={run_id}; {command}"
        try:
            return await super().exec_as_agent(
                environment,
                command=command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
        except asyncio.CancelledError:
            await self._cleanup_cancelled_run(environment, run_id)
            raise

    async def _cleanup_cancelled_run(
        self,
        environment: BaseEnvironment,
        run_id: str,
    ) -> None:
        marker = shlex.quote(f"{AGENT_RUN_ENV}={run_id}")
        command = f"""
marker={marker}
pids=
for envfile in /proc/[0-9]*/environ; do
  [ -r "$envfile" ] || continue
  if tr '\\000' '\\n' <"$envfile" 2>/dev/null | grep -Fqx -- "$marker"; then
    pid=${{envfile#/proc/}}
    pid=${{pid%/environ}}
    [ "$pid" = "$$" ] || pids="$pids $pid"
  fi
done
if [ -n "$pids" ]; then
  kill -TERM $pids 2>/dev/null || true
  sleep 1
  kill -KILL $pids 2>/dev/null || true
fi
""".strip()
        try:
            await asyncio.shield(
                super().exec_as_agent(
                    environment,
                    command=command,
                    timeout_sec=10,
                )
            )
        except Exception:
            pass

    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        skills = self._target_root / "skills"
        marketplace_root = self._target_root / ".agents"
        marketplace = marketplace_root / "plugins" / "marketplace.json"
        plugins = self._target_root / "plugins"
        remote_directories: list[str] = []
        if self._skills_enabled and skills.is_dir():
            remote_directories.append(REMOTE_SKILLS_DIR)
        if marketplace.is_file() and plugins.is_dir():
            remote_directories.extend(
                [
                    f"{REMOTE_PLUGIN_MARKETPLACE}/.agents",
                    f"{REMOTE_PLUGIN_MARKETPLACE}/plugins",
                ]
            )
        if remote_directories:
            await self.exec_as_agent(
                environment,
                command="mkdir -p " + " ".join(shlex.quote(path) for path in remote_directories),
            )
        if self._skills_enabled and skills.is_dir():
            await environment.upload_dir(source_dir=skills, target_dir=REMOTE_SKILLS_DIR)
        if marketplace.is_file() and plugins.is_dir():
            await environment.upload_dir(
                source_dir=marketplace_root,
                target_dir=f"{REMOTE_PLUGIN_MARKETPLACE}/.agents",
            )
            await environment.upload_dir(
                source_dir=plugins,
                target_dir=f"{REMOTE_PLUGIN_MARKETPLACE}/plugins",
            )


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})
