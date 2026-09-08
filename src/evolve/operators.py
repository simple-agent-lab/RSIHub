from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolve.mutate_budget import harbor_mutate_budget, uses_harbor_per_attempt_timeout

from .config import load_config
from .execution_runtime import (
    execution_runtime_config,
    prepare_execution_environment,
    resolve_execution_runtime,
)
from .runtime.process import _terminate


@dataclass(frozen=True)
class OperatorResult:
    name: str
    returncode: int
    stdout: str
    stderr: str
    wall_s: float


_OPERATOR_WRAPPER = """
from pathlib import Path
import sys

script = Path(sys.argv[1])
sys.argv = sys.argv[1:]
namespace = {"__name__": "__main__", "__file__": str(script), "__package__": None, "__cached__": None}
exec(compile(script.read_bytes(), str(script), "exec"), namespace)
"""


def _text(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _progress(message: str) -> None:
    if os.environ.get("EVOLVE_PROGRESS", "1") != "0":
        print(f"[evolve] {message}", flush=True)


def _operator_deadline_s(name: str, config_block: dict[str, Any], timeout_s: float) -> float:
    if name != "mutate" or not uses_harbor_per_attempt_timeout(config_block) or timeout_s <= 0:
        return timeout_s
    try:
        max_retries = max(0, int(config_block.get("max_retries", 0)))
    except (TypeError, ValueError):
        max_retries = 0
    return harbor_mutate_budget(timeout_s, max_retries).operator_s


def run_operator(
    *,
    name: str,
    checkout: Path,
    workspace: Path,
    genid: str,
    parent: str | None,
    run_dir: Path,
    config_block: dict[str, Any],
    timeout_s: float,
    round_number: int | None = None,
    operator_checkout: Path | None = None,
) -> OperatorResult:
    start = time.monotonic()
    deadline_s = _operator_deadline_s(name, config_block, timeout_s)
    source_checkout = operator_checkout or checkout
    script = source_checkout / "operators" / f"{name}.py"
    if not script.exists():
        return OperatorResult(
            name=name,
            returncode=127,
            stdout="",
            stderr=f"missing operator script: {script}",
            wall_s=time.monotonic() - start,
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    _progress(f"gen/{genid} {name}: started; artifacts: {run_dir}")
    from .operator_isolation import run_isolated_operator, session_sandbox

    sandbox = session_sandbox(workspace, deadline_s)
    if sandbox is not None:
        result = run_isolated_operator(
            name=name,
            checkout=checkout,
            source=source_checkout,
            workspace=workspace,
            genid=genid,
            parent=parent,
            run_dir=run_dir,
            config_block=config_block,
            sandbox=sandbox,
        )
        return OperatorResult(name, result.returncode, result.stdout, result.stderr, time.monotonic() - start)
    base_env = (
        {**os.environ, "EVOLVE_HOME": str((run_dir / "operator-home").resolve())}
        if checkout.resolve() != workspace.resolve()
        else os.environ
    )
    workspace_config = load_config(workspace / "evolve.yaml")
    execution_runtime = resolve_execution_runtime(
        execution_runtime_config(workspace_config["execution_runtime"]),
        base_env,
    )
    env: dict[str, str] = {
        **prepare_execution_environment(
            execution_runtime,
            base_env,
            runtime_root=workspace / "runs" / "runtime" / "execution",
        ),
        "EVOLVE_GENID": genid,
        "EVOLVE_PARENT": parent or "",
        "EVOLVE_STAGE_TIMEOUT_S": str(timeout_s),
        "EVOLVE_OPERATOR_TIMEOUT_S": str(deadline_s),
        "EVOLVE_RUN_DIR": str(run_dir.resolve()),
        "EVOLVE_WORKSPACE": str(workspace.resolve()),
        "EVOLVE_CHECKOUT": str(checkout.resolve()),
    }
    try:
        live_output = os.environ.get("EVOLVE_LIVE_OUTPUT") == "1"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _OPERATOR_WRAPPER,
                str(script.resolve()),
                "--config",
                json.dumps(config_block, allow_nan=False),
            ],
            cwd=checkout,
            env=env,
            text=True,
            stdout=None if live_output else subprocess.PIPE,
            stderr=None if live_output else subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=deadline_s)
        except subprocess.TimeoutExpired as exc:
            terminated_stdout, terminated_stderr = _terminate(process)
            stderr = _text(exc.stderr or terminated_stderr)
            if stderr:
                stderr = f"{stderr.rstrip()}\n"
            stderr += f"timeout after {deadline_s}s"
            result = OperatorResult(
                name=name,
                returncode=-1,
                stdout=_text(exc.stdout or terminated_stdout),
                stderr=stderr,
                wall_s=time.monotonic() - start,
            )
            _progress(f"gen/{genid} {name}: timed out after {result.wall_s:.1f}s")
            return result
        except BaseException:
            _terminate(process)
            raise
        result = OperatorResult(
            name=name,
            returncode=process.returncode or 0,
            stdout=stdout or "",
            stderr=stderr or "",
            wall_s=time.monotonic() - start,
        )
        _progress(f"gen/{genid} {name}: exit={result.returncode}, elapsed={result.wall_s:.1f}s")
        return result
    except OSError as error:
        return OperatorResult(
            name=name,
            returncode=127,
            stdout="",
            stderr=str(error),
            wall_s=time.monotonic() - start,
        )


def operator_timeout(operators_config: dict[str, Any], name: str) -> float:
    block = operators_config.get(name)
    if isinstance(block, dict):
        timeout_s = block.get("timeout_s")
        if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool):
            return float(timeout_s)
    default_timeout_s = operators_config.get("timeout_s")
    if isinstance(default_timeout_s, (int, float)) and not isinstance(default_timeout_s, bool):
        return float(default_timeout_s)
    return 600.0
