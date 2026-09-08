"""Durable host launcher for an external Agent Driven controller."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_driver import SESSION_DIR, session_status
from .agent_optimizer import controller_input, validate_load, verify_optimizer
from .runtime.model_broker import ModelBrokerConfig
from .runtime.process import run_owned
from .runtime.sandbox import SandboxConfig

_CONTROLLER_DIR = SESSION_DIR / "controller"


@dataclass(frozen=True)
class ControllerLimits:
    max_attempts: int
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_s: float | None = None
    max_total_cost_usd: float | None = None

    def validate(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise RuntimeError("controller max_attempts must be a positive integer")
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens < 1
        ):
            raise RuntimeError("controller max_tokens must be a positive integer")
        for name in ("max_cost_usd", "max_wall_s", "max_total_cost_usd"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            ):
                raise RuntimeError(f"controller {name} must be a positive finite number")


@dataclass(frozen=True)
class ControllerConfig:
    command: str
    arguments: tuple[str, ...]
    limits: ControllerLimits
    sandbox: SandboxConfig | None = None
    model_broker: ModelBrokerConfig | None = None

    def validate(self) -> None:
        self.limits.validate()
        if self.model_broker is not None:
            self.model_broker.validate()
            if self.sandbox is None or (self.limits.max_cost_usd is None and self.limits.max_total_cost_usd is None):
                raise RuntimeError("model broker requires isolation and a cumulative dollar budget")
        if self.sandbox is not None:
            self.sandbox.validate()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.sandbox.image):
                raise RuntimeError("isolated controller configuration requires an immutable image ID")
        if not self.command or "\x00" in self.command:
            raise RuntimeError("controller command must be a non-empty executable path or name")
        if any("\x00" in argument for argument in self.arguments):
            raise RuntimeError("controller arguments cannot contain NUL bytes")


def launch_controller(workspace: Path, config: ControllerConfig, *, deferred_actions: bool = False) -> dict[str, Any]:
    """Run one metered controller attempt and retain enough state to resume safely."""

    from .agent_handoff import recover_handoff

    workspace = workspace.resolve()
    recover_handoff(workspace)
    config.validate()
    if config.sandbox is not None and not deferred_actions:
        raise RuntimeError("isolated controllers require deferred action transport")
    root = workspace / _CONTROLLER_DIR
    root.mkdir(parents=True, exist_ok=True)
    with _controller_lock(root):
        manifest_path = root / "manifest.json"
        expected = {
            "schema_version": 1,
            "command": config.command,
            "arguments": list(config.arguments),
            "limits": asdict(config.limits),
        }
        if config.sandbox is not None:
            expected["sandbox"] = asdict(config.sandbox)
        if config.model_broker is not None:
            expected["model_broker"] = json.loads(json.dumps(asdict(config.model_broker)))
        if manifest_path.exists():
            manifest = _read_object(manifest_path)
            if manifest != expected:
                raise RuntimeError("controller launcher already exists with different command or limits")
        else:
            _atomic_json(manifest_path, expected)

        status = _status(workspace, expected)
        if status["pending_attempt"] is not None:
            raise RuntimeError(
                f"controller attempt {status['pending_attempt']} was interrupted; inspect it and resolve before resuming"
            )
        if status["session"]["status"] in {"submitted", "finished", "paused"}:
            return status
        if status["session"]["status"] in {"running", "interrupted"}:
            raise RuntimeError(f"Agent Driven session is {status['session']['status']}; resolve it before launch")
        if status["exhausted_reasons"]:
            raise RuntimeError("controller launcher budget is exhausted: " + ", ".join(status["exhausted_reasons"]))

        if status["session"].get("mode") == "continuous":
            verify_optimizer(workspace, status["session"]["research"]["active_optimizer"])
        attempt = status["attempts_used"] + 1
        attempt_dir = root / f"attempt-{attempt}"
        attempt_dir.mkdir()
        input_path = controller_input(workspace, status["session"], attempt_dir)
        usage_path = attempt_dir / "usage.json"
        from .agent_evidence import facts_bytes

        facts_path = attempt_dir / "session-facts.json"
        facts_path.write_bytes(facts_bytes(workspace))
        events_path = root / "attempts.jsonl"
        _append_event(
            events_path,
            {
                "schema_version": 1,
                "phase": "started",
                "attempt": attempt,
                "timestamp": _timestamp(),
                "command": [config.command, *config.arguments],
            },
        )
        remaining_wall = status["controller_wall_remaining_s"]
        environment: dict[str, str] = {
            **os.environ,
            "EVOLVE_AGENT_WORKSPACE": str(workspace),
            "EVOLVE_CONTROLLER_ACTION_MODE": "deferred" if deferred_actions else "synchronous",
            "EVOLVE_CONTROLLER_ATTEMPT": str(attempt),
            "EVOLVE_CONTROLLER_ATTEMPT_DIR": str(attempt_dir),
            "EVOLVE_CONTROLLER_USAGE_RECEIPT": str(usage_path),
            "EVOLVE_CONTROLLER_FACTS": str(facts_path),
        }
        environment.pop("EVOLVE_CONTROLLER_INPUT", None)
        if input_path is not None:
            environment["EVOLVE_CONTROLLER_INPUT"] = str(input_path)
        if config.sandbox is None:
            result = run_owned(
                [config.command, *config.arguments],
                cwd=workspace,
                env=environment,
                timeout_s=remaining_wall,
            )
        else:
            from .agent_isolation import run_isolated_controller

            broker = config.model_broker
            if broker is not None:
                remaining_costs = [broker.max_cost_usd]
                if config.limits.max_cost_usd is not None:
                    remaining_costs.append(config.limits.max_cost_usd - status["controller_cost_usd"])
                if config.limits.max_total_cost_usd is not None:
                    remaining_costs.append(config.limits.max_total_cost_usd - status["total_observed_cost_usd"])
                broker = replace(broker, max_cost_usd=min(remaining_costs))
            result = run_isolated_controller(
                workspace,
                status["session"],
                attempt_dir,
                config.sandbox,
                [config.command, *config.arguments],
                remaining_wall,
                broker=broker,
            )
        (attempt_dir / "stdout.log").write_text(result.stdout)
        (attempt_dir / "stderr.log").write_text(result.stderr)
        usage = None
        try:
            usage = _read_usage(usage_path)
            if (
                config.limits.max_cost_usd is not None
                or config.limits.max_total_cost_usd is not None
                or status["session"].get("mode") == "continuous"
            ) and usage["cost_usd"] is None:
                raise RuntimeError("controller usage cost_usd is required by the configured dollar budget")
        except Exception as exc:
            usage_error = str(exc)
        else:
            usage_error = None
        if input_path is not None:
            try:
                validate_load(input_path, attempt_dir / "method-load.json")
            except RuntimeError as exc:
                usage_error = str(exc)
        if config.sandbox is not None and result.returncode == 0 and not result.timed_out and usage_error is None:
            from .agent_isolation import accept_isolated_return

            try:
                accept_isolated_return(workspace, attempt_dir)
            except (RuntimeError, OSError, ValueError, TypeError) as exc:
                usage_error = f"isolated return rejected: {exc}"
        phase = "completed" if result.returncode == 0 and not result.timed_out and usage_error is None else "failed"
        event = {
            "schema_version": 1,
            "phase": phase,
            "attempt": attempt,
            "timestamp": _timestamp(),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "wall_s": round(result.wall_s, 6),
            "usage": usage,
            "usage_error": usage_error,
            "stdout": str((attempt_dir / "stdout.log").relative_to(workspace)),
            "stderr": str((attempt_dir / "stderr.log").relative_to(workspace)),
        }
        _append_event(events_path, event)
        status = _status(workspace, expected)
        if phase == "failed":
            detail = usage_error or (
                "controller timed out" if result.timed_out else f"controller exited {result.returncode}"
            )
            raise RuntimeError(f"controller attempt {attempt} failed: {detail}")
        return status


def controller_status(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    manifest = _read_object(workspace / _CONTROLLER_DIR / "manifest.json")
    return _status(workspace, manifest)


def resolve_controller_interrupted(
    workspace: Path,
    attempt: int,
    reason: str,
    *,
    total_tokens: int,
    cost_usd: float | None = None,
    wall_s: float = 0.0,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    root = workspace / _CONTROLLER_DIR
    with _controller_lock(root):
        manifest = _read_object(root / "manifest.json")
        status = _status(workspace, manifest)
        if status["pending_attempt"] != attempt:
            raise RuntimeError(f"controller attempt {attempt} is not the interrupted attempt")
        usage = _validated_usage(total_tokens, cost_usd)
        if isinstance(wall_s, bool) or not isinstance(wall_s, (int, float)) or not math.isfinite(wall_s) or wall_s < 0:
            raise RuntimeError("interrupted controller wall_s must be a non-negative finite number")
        event = {
            "schema_version": 1,
            "phase": "failed",
            "attempt": attempt,
            "timestamp": _timestamp(),
            "returncode": None,
            "timed_out": False,
            "wall_s": float(wall_s),
            "usage": usage,
            "usage_error": f"operator-confirmed interrupted controller: {reason}"[:1000],
        }
        _append_event(root / "attempts.jsonl", event)
        return event


def _status(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    limits = ControllerLimits(**manifest["limits"])
    events = _events(workspace / _CONTROLLER_DIR / "attempts.jsonl")
    started = [event for event in events if event.get("phase") == "started"]
    terminal_attempts = {
        int(event["attempt"]): event
        for event in events
        if event.get("phase") in {"completed", "failed"} and isinstance(event.get("attempt"), int)
    }
    pending = next(
        (int(event["attempt"]) for event in reversed(started) if int(event["attempt"]) not in terminal_attempts),
        None,
    )
    terminal = list(terminal_attempts.values())
    tokens = sum(int(event["usage"]["total_tokens"]) for event in terminal if isinstance(event.get("usage"), dict))
    usages = [event["usage"] for event in terminal if isinstance(event.get("usage"), dict)]
    priced_costs = [float(usage["cost_usd"]) for usage in usages if usage.get("cost_usd") is not None]
    unpriced_attempts = len(terminal) - len(usages) + sum(usage.get("cost_usd") is None for usage in usages)
    controller_cost = None if unpriced_attempts else round(sum(priced_costs), 9)
    controller_wall = round(sum(float(event.get("wall_s", 0)) for event in terminal), 6)
    session = session_status(workspace)
    total_cost = (
        None
        if controller_cost is None or session.get("unpriced_candidate_runs")
        else round(controller_cost + float(session["observed_cost_usd"]), 9)
    )
    exhausted = ["unpriced_candidate_cost"] if session.get("unpriced_candidate_runs") else []
    if len(started) >= limits.max_attempts:
        exhausted.append("attempts")
    if limits.max_tokens is not None and tokens >= limits.max_tokens:
        exhausted.append("controller_tokens")
    if limits.max_cost_usd is not None and controller_cost is not None and controller_cost >= limits.max_cost_usd:
        exhausted.append("controller_cost_usd")
    if (
        limits.max_cost_usd is not None or limits.max_total_cost_usd is not None or session.get("mode") == "continuous"
    ) and unpriced_attempts:
        exhausted.append("unpriced_controller_cost")
    if limits.max_wall_s is not None and controller_wall >= limits.max_wall_s:
        exhausted.append("controller_wall_s")
    if limits.max_total_cost_usd is not None and total_cost is not None and total_cost >= limits.max_total_cost_usd:
        exhausted.append("total_cost_usd")
    return {
        "schema_version": 1,
        "status": "interrupted" if pending is not None else "exhausted" if exhausted else "ready",
        "session": session,
        "attempts_used": len(started),
        "pending_attempt": pending,
        "controller_tokens": tokens,
        "controller_cost_usd": controller_cost,
        "unpriced_controller_attempts": unpriced_attempts,
        "controller_wall_s": controller_wall,
        "total_observed_cost_usd": total_cost,
        "controller_wall_remaining_s": (
            None if limits.max_wall_s is None else max(0.0, limits.max_wall_s - controller_wall)
        ),
        "limits": asdict(limits),
        "exhausted_reasons": exhausted,
    }


def _read_usage(path: Path) -> dict[str, int | float | None]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"controller did not write a valid usage receipt: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("controller usage receipt must be a JSON object")
    return _validated_usage(payload.get("total_tokens"), payload.get("cost_usd"))


def _validated_usage(tokens: object, cost: object) -> dict[str, int | float | None]:
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise RuntimeError("controller usage total_tokens must be a non-negative integer")
    if cost is not None and (
        isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0
    ):
        raise RuntimeError("controller usage cost_usd must be null or a non-negative finite number")
    return {"total_tokens": tokens, "cost_usd": None if cost is None else float(cost)}


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid controller receipt at line {line_number}") from exc
        if not isinstance(event, dict):
            raise RuntimeError(f"invalid controller receipt at line {line_number}")
        result.append(event)
    return result


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"missing or invalid controller manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid controller manifest: {path}")
    return payload


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _controller_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another controller launcher is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
