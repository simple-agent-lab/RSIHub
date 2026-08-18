"""Harbor candidate adapter for DeepSeek Harness (dsh).

Runs on the HOST inside the workspace's locked runtime (like every
``evaluator.agent``). Per trial it:

1. resolves the candidate profile from ``EVOLVE_CANDIDATE_SOURCE``
   (= ``<eval-checkout>/target``, injected by the harbor engine),
2. spawns a dsh session via the dsh Python SDK (``runners/rollout_driver.py``
   subprocess; the SDK starts dsh's Node runtime),
3. bridges the session's bash tool into the task container via
   ``docker exec`` (see ``runners/compositions/rollout.base.cordis.yml``),
4. after the session, converts the dsh session log into
   ``<logs>/trajectory.json`` so RSIHub's analyze operators
   (trace_browser / failure_patterns / trajectory_only) can read it.

Endpoint governance: the adapter maps ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``
(frozen into the workspace identity at init) onto dsh's ``DEEPSEEK_BASE_URL`` /
``DEEPSEEK_API_KEY`` so model routing is owned by the workspace contract.

The frozen harness pieces (drivers and cordis compositions) live in
``runners/`` next to this file and are resolved relative to it; the recipe's
``surface.exclude`` keeps them, this adapter, and the converter out of the
mutable surface.

Optional restricted-network compensations (all no-ops when unset):
  DSH_ASSETS_DIR             dir with ``uv``/``uvx`` binaries and ``py313.tar.gz``
                             to preload into containers whose graders cannot
                             reach github
  DSH_CONTAINER_APT_MIRROR   apt mirror base URL rewritten into sources.list
  DSH_CONTAINER_PIP_INDEX    pip index URL written to /etc/pip.conf
  DSH_CONTAINER_PROXY        http(s) proxy exported inside the container
  DSH_CONTAINER_NO_PROXY     no_proxy list (default: localhost,127.0.0.1)

This file ships in the seed but sits in ``surface.exclude``: a candidate that
edits it is rejected as ``invalid_proposal``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from harbor.agents.base import BaseAgent


class DshAgent(BaseAgent):
    SUPPORTS_ATIF = False

    @staticmethod
    def name() -> str:
        return "dsh-candidate"

    def version(self) -> str | None:
        return "0.3.0"

    def _env(self, key: str, default: str | None = None) -> str | None:
        value = self._extra_env.get(key)
        if value is None:
            value = os.environ.get(key)
        return default if value is None else value

    @staticmethod
    def _runners_dir() -> Path:
        return Path(__file__).resolve().parent / "runners"

    # ------------------------------------------------------------------
    # Optional container bootstrap. TB2 graders assume container network
    # access; in restricted environments the snippets below compensate
    # without touching scoring. Every snippet is gated on its env var.
    # ------------------------------------------------------------------
    _APT_SNIPPET = r"""
BM={mirror}
FILES="/etc/apt/sources.list $(ls /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null)"
if [ -f /etc/os-release ] && grep -qi ubuntu /etc/os-release; then
  sed -i -E "s|https?://[a-z0-9.]*archive.ubuntu.com/ubuntu|$BM/ubuntu|g; s|https?://security.ubuntu.com/ubuntu|$BM/ubuntu|g; s|https?://ports.ubuntu.com/ubuntu-ports|$BM/ubuntu-ports|g" $FILES 2>/dev/null
else
  sed -i -E "s|https?://deb.debian.org/debian-security|$BM/debian-security|g; s|https?://security.debian.org/debian-security|$BM/debian-security|g; s|https?://security.debian.org|$BM/debian-security|g; s|https?://deb.debian.org/debian|$BM/debian|g" $FILES 2>/dev/null
fi
"""

    _PIP_SNIPPET = r"""
{{ echo "[global]"; echo "index-url = {index}"; }} > /etc/pip.conf 2>/dev/null
"""

    _ASSETS_SNIPPET = r"""
if [ -f /tmp/py313.tar.gz ]; then
  mkdir -p /opt && tar xzf /tmp/py313.tar.gz -C /opt 2>/dev/null
  ln -sf /opt/py313/bin/python3.13 /usr/local/bin/python3.13 2>/dev/null
fi
if [ -f /tmp/uv ]; then chmod +x /tmp/uv /tmp/uvx 2>/dev/null; cp /tmp/uv /usr/local/bin/uv 2>/dev/null; cp /tmp/uvx /usr/local/bin/uvx 2>/dev/null; fi
for H in /root "$HOME" /home/*; do
  [ -d "$H" ] || continue
  mkdir -p "$H/.local/bin" 2>/dev/null
  {{ printf 'export PATH="/usr/local/bin:%s/.local/bin:$PATH"\n' "$H"
    echo 'export UV_PYTHON_DOWNLOADS=never'
  }} > "$H/.local/bin/env" 2>/dev/null
  cp /tmp/uv "$H/.local/bin/uv" 2>/dev/null; cp /tmp/uvx "$H/.local/bin/uvx" 2>/dev/null; chmod +x "$H/.local/bin/uv" "$H/.local/bin/uvx" 2>/dev/null
done
"""

    _PROXY_SNIPPET = r"""
PXY={proxy}
NPXY="{no_proxy}"
{{ printf 'export http_proxy=%s https_proxy=%s HTTP_PROXY=%s HTTPS_PROXY=%s\n' "$PXY" "$PXY" "$PXY" "$PXY"
  printf 'export no_proxy=%s NO_PROXY=%s\n' "$NPXY" "$NPXY"; }} >> /etc/bash.bashrc 2>/dev/null
for H in /root "$HOME" /home/*; do
  [ -f "$H/.local/bin/env" ] || continue
  {{ printf 'export http_proxy=%s https_proxy=%s HTTP_PROXY=%s HTTPS_PROXY=%s\n' "$PXY" "$PXY" "$PXY" "$PXY"
    printf 'export no_proxy=%s NO_PROXY=%s\n' "$NPXY" "$NPXY"; }} >> "$H/.local/bin/env" 2>/dev/null
done
"""

    def _bootstrap_command(self) -> str | None:
        parts: list[str] = []
        mirror = self._env("DSH_CONTAINER_APT_MIRROR")
        if mirror:
            parts.append(self._APT_SNIPPET.format(mirror=mirror))
        pip_index = self._env("DSH_CONTAINER_PIP_INDEX")
        if pip_index:
            parts.append(self._PIP_SNIPPET.format(index=pip_index))
        if self._env("DSH_ASSETS_DIR"):
            parts.append(self._ASSETS_SNIPPET)
        proxy = self._env("DSH_CONTAINER_PROXY")
        if proxy:
            parts.append(
                self._PROXY_SNIPPET.format(
                    proxy=proxy,
                    no_proxy=self._env("DSH_CONTAINER_NO_PROXY", "localhost,127.0.0.1"),
                )
            )
        if not parts:
            return None
        return "set +e\n" + "\n".join(parts) + "\ntrue\n"

    async def setup(self, environment) -> None:
        try:
            assets_dir = self._env("DSH_ASSETS_DIR")
            if assets_dir:
                for name, dest in (("uv", "/tmp/uv"), ("uvx", "/tmp/uvx"), ("py313.tar.gz", "/tmp/py313.tar.gz")):
                    src = Path(assets_dir) / name
                    if src.is_file():
                        await environment.upload_file(str(src), dest)
            command = self._bootstrap_command()
            if command:
                await environment.exec(command=command, user="root", timeout_sec=120)
        except Exception as error:  # best-effort: fall back to the container as-is
            self.logger.warning("container bootstrap skipped: %r", error)

    async def _container_id(self, environment) -> str:
        result = await environment._run_docker_compose_command(["ps", "-q", "main"], check=True)
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("cannot resolve task container id via docker compose ps")
        return lines[0]

    def _candidate_dir(self) -> Path:
        source = self._env("EVOLVE_CANDIDATE_SOURCE")
        if not source:
            raise RuntimeError("EVOLVE_CANDIDATE_INVALID: EVOLVE_CANDIDATE_SOURCE missing")
        candidate = Path(source).expanduser().resolve()
        if not (candidate / "profile.cordis.yml").is_file():
            raise RuntimeError(f"EVOLVE_CANDIDATE_INVALID: no profile.cordis.yml under {candidate}")
        return candidate

    async def run(self, instruction, environment, context) -> None:
        container = await self._container_id(environment)
        candidate = self._candidate_dir()
        runners = self._runners_dir()

        cordis = Path(self._env("DSH_ROLLOUT_CORDIS", str(runners / "compositions" / "rollout.base.cordis.yml")))
        if not cordis.is_file():
            raise RuntimeError(f"rollout cordis composition missing: {cordis}")
        driver = runners / "rollout_driver.py"
        if not driver.is_file():
            raise RuntimeError(f"rollout driver missing: {driver}")

        logs = Path(self.logs_dir)
        logs.mkdir(parents=True, exist_ok=True)
        workspace = logs / "host-workspace"
        workspace.mkdir(exist_ok=True)
        task_file = logs / "instruction.txt"
        task_file.write_text(instruction)

        model = self.model_name or "deepseek-v4-flash"
        if "/" in model:
            model = model.split("/", 1)[1]

        env = os.environ.copy()
        env.update(self._extra_env)
        # Endpoint is governed by the workspace's frozen identity.
        base_url = self._env("OPENAI_BASE_URL")
        if base_url:
            env["DEEPSEEK_BASE_URL"] = base_url
        api_key = self._env("OPENAI_API_KEY")
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        env.update(
            {
                "DSH_CONTAINER": container,
                "DSH_CANDIDATE_DIR": str(candidate),
                "DSH_ROLLOUT_CORDIS": str(cordis),
                "DSH_MODEL": model,
                "DSH_TASK_FILE": str(task_file),
                "DSH_HOST_WORKSPACE": str(workspace),
                "DSH_SESSION_ROOT": str(logs / "sessions"),
                "DSH_SESSION_ID": (self.session_id or "task").replace("/", "_"),
                "DSH_FINAL_RESPONSE": str(logs / "final_response.txt"),
            }
        )

        timeout = float(self._env("DSH_TASK_TIMEOUT_SEC", "1800") or "1800")
        driver_log = open(logs / "driver.log", "ab")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(driver),
            stdout=driver_log,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

        def _kill() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
            self.logger.info("dsh driver exited rc=%s (task %s)", returncode, self.session_id)
        except TimeoutError:
            self.logger.warning("dsh driver timed out after %ss; killing process group", timeout)
            _kill()
            await proc.wait()
        except asyncio.CancelledError:
            _kill()
            raise
        finally:
            driver_log.close()
            self._write_trajectory(logs)

    def _write_trajectory(self, logs: Path) -> None:
        """Convert the dsh session log into trajectory.json (best-effort)."""
        try:
            from dsh_trajectory import convert_session
        except ImportError:
            try:
                from target.dsh_trajectory import convert_session  # type: ignore[no-redef]
            except ImportError:
                self.logger.warning("dsh_trajectory converter unavailable; skipping")
                return
        try:
            convert_session(logs / "sessions", logs / "trajectory.json")
        except Exception as error:
            self.logger.warning("trajectory conversion failed: %r", error)
