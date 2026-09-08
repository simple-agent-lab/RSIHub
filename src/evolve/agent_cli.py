"""CLI surface for durable outer-agent control sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from .agent_driver import (
    AgentLimits,
    audit_arm_pair,
    clone_arm,
    execute_action,
    parse_action,
    resolve_interrupted,
    seal_submitted,
    session_status,
    start_session,
)
from .agent_launcher import (
    ControllerConfig,
    ControllerLimits,
    controller_status,
    launch_controller,
    resolve_controller_interrupted,
)
from .agent_queue import defer_action, drive_controller, resolve_deferred, resume_controller
from .runtime.model_broker import ModelBrokerConfig
from .runtime.sandbox import SandboxConfig


def build_agent_app(guard) -> typer.Typer:
    app = typer.Typer(add_completion=False, no_args_is_help=True, help="Operate an Agent Driven control session")

    @app.command()
    @guard
    def start(
        workspace: Path = typer.Argument(Path(".")),
        max_actions: int = typer.Option(20, "--max-actions", min=1),
        max_operator_calls: int = typer.Option(10, "--max-operator-calls", min=0),
        max_evaluations: int = typer.Option(3, "--max-evaluations", min=0),
        max_cost_usd: float | None = typer.Option(None, "--max-cost-usd", min=0.0),
        max_wall_s: float | None = typer.Option(None, "--max-wall-s", min=0.0),
        require_clean_start: bool = typer.Option(False, "--require-clean-start"),
        mode: str = typer.Option("batch", "--mode"),
        optimizer: Path | None = typer.Option(None, "--optimizer"),
        objective: str = typer.Option("", "--objective"),
    ) -> None:
        """Start or resume a bounded session from a certified parent."""
        state = start_session(
            workspace,
            AgentLimits(max_actions, max_operator_calls, max_evaluations, max_cost_usd, max_wall_s),
            require_clean_start=require_clean_start,
            mode=mode,
            optimizer=optimizer,
            objective=objective,
        )
        print(json.dumps(state, sort_keys=True, allow_nan=False))

    @app.command()
    @guard
    def status(workspace: Path = typer.Argument(Path("."))) -> None:
        """Show the live budget, champion, and interrupted action."""
        print(json.dumps(session_status(workspace), sort_keys=True, allow_nan=False))

    @app.command("schema")
    def schema(mode: str = typer.Option("batch", "--mode")) -> None:
        """Print the strict action shapes accepted by ``agent act``."""
        from .agent_driver import _ACTION_FIELDS

        if mode not in {"batch", "continuous"}:
            raise typer.BadParameter("mode must be batch or continuous")
        from .agent_research import FIELDS

        shapes = {
            name: {"required": sorted(required), "optional": sorted(optional)}
            for name, (required, optional) in _ACTION_FIELDS.items()
            if (name != "submit_champion" if mode == "continuous" else name not in FIELDS)
        }
        print(json.dumps(shapes, sort_keys=True))

    @app.command("audit-arms")
    @guard
    def audit_arms(left: Path, right: Path) -> None:
        """Verify two clean, independent arms share one frozen start."""
        print(json.dumps(audit_arm_pair(left, right), sort_keys=True, allow_nan=False))

    @app.command("clone-arm")
    @guard
    def clone(source: Path, destination: Path) -> None:
        """Copy a certified baseline into a fresh independent arm."""
        print(json.dumps(clone_arm(source, destination), sort_keys=True, allow_nan=False))

    @app.command("run-controller")
    @guard
    def run_controller(
        workspace: Path = typer.Argument(Path(".")),
        controller: str = typer.Option(..., "--controller", help="controller executable path or name"),
        controller_arg: list[str] = typer.Option([], "--controller-arg", help="repeat for each literal argument"),
        max_attempts: int = typer.Option(3, "--max-attempts", min=1),
        continuous: bool = typer.Option(
            False, "--continuous", help="execute deferred actions between controller attempts"
        ),
        max_controller_tokens: int | None = typer.Option(None, "--max-controller-tokens", min=1),
        max_controller_cost_usd: float | None = typer.Option(None, "--max-controller-cost-usd", min=0.0),
        max_controller_wall_s: float | None = typer.Option(None, "--max-controller-wall-s", min=0.0),
        max_total_cost_usd: float | None = typer.Option(None, "--max-total-cost-usd", min=0.0),
        sandbox_image: str | None = typer.Option(None, "--sandbox-image", help="immutable local Docker image ID"),
        sandbox_timeout: float = typer.Option(600.0, "--sandbox-timeout", min=0.1),
        model_handler: str | None = typer.Option(None, "--model-handler", help="trusted host model-handler executable"),
        model_handler_arg: list[str] = typer.Option([], "--model-handler-arg"),
        max_model_requests: int = typer.Option(100, "--max-model-requests", min=1),
        model_request_timeout: float = typer.Option(120.0, "--model-request-timeout", min=0.1),
    ) -> None:
        """Run one resumable, durably metered outer-controller attempt."""
        limits = ControllerLimits(
            max_attempts=max_attempts,
            max_tokens=max_controller_tokens,
            max_cost_usd=max_controller_cost_usd,
            max_wall_s=max_controller_wall_s,
            max_total_cost_usd=max_total_cost_usd,
        )
        broker = None
        if model_handler is not None:
            cost_limit = max_controller_cost_usd if max_controller_cost_usd is not None else max_total_cost_usd
            if cost_limit is None or cost_limit <= 0:
                raise typer.BadParameter("model handler requires a positive cumulative dollar budget")
            broker = ModelBrokerConfig(
                (model_handler, *model_handler_arg), max_model_requests, cost_limit, model_request_timeout
            )
        config = ControllerConfig(
            controller,
            tuple(controller_arg),
            limits,
            SandboxConfig(sandbox_image, sandbox_timeout) if sandbox_image is not None else None,
            broker,
        )
        runner = drive_controller if continuous else launch_controller
        print(json.dumps(runner(workspace, config), sort_keys=True, allow_nan=False))

    @app.command("controller-status")
    @guard
    def show_controller_status(workspace: Path = typer.Argument(Path("."))) -> None:
        """Show controller attempts and combined controller/mechanism spend."""
        print(json.dumps(controller_status(workspace), sort_keys=True, allow_nan=False))

    @app.command("resolve-controller-interrupted")
    @guard
    def resolve_controller(
        workspace: Path = typer.Argument(Path(".")),
        attempt: int = typer.Option(..., "--attempt", min=1),
        reason: str = typer.Option(..., "--reason"),
        total_tokens: int = typer.Option(..., "--total-tokens", min=0),
        cost_usd: float | None = typer.Option(None, "--cost-usd", min=0.0),
        wall_s: float = typer.Option(0.0, "--wall-s", min=0.0),
    ) -> None:
        """Mark one inspected interrupted controller attempt failed."""
        print(
            json.dumps(
                resolve_controller_interrupted(
                    workspace,
                    attempt,
                    reason,
                    total_tokens=total_tokens,
                    cost_usd=cost_usd,
                    wall_s=wall_s,
                ),
                sort_keys=True,
                allow_nan=False,
            )
        )

    @app.command()
    @guard
    def act(
        workspace: Path = typer.Argument(Path(".")),
        action: str = typer.Option(..., "--action", help="strict JSON action object"),
        defer: bool = typer.Option(False, "--defer", help="persist action for execution after controller exits"),
    ) -> None:
        """Validate, execute, and durably receipt one controller action."""
        try:
            payload = json.loads(action, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
            from .agent_research import record_rejection

            record_rejection(workspace, detail)
            raise typer.BadParameter(f"--action must be valid JSON: {detail}", param_hint="--action") from exc
        try:
            parsed = parse_action(payload)
        except RuntimeError as exc:
            from .agent_research import record_rejection

            record_rejection(workspace, str(exc))
            raise
        runner = (
            defer_action if defer or os.environ.get("EVOLVE_CONTROLLER_ACTION_MODE") == "deferred" else execute_action
        )
        print(json.dumps(runner(workspace, parsed), sort_keys=True, allow_nan=False))

    @app.command("resume-controller")
    @guard
    def resume_progression(workspace: Path = typer.Argument(Path("."))) -> None:
        """Resume from the saved controller configuration after resolving blocked work."""
        print(json.dumps(resume_controller(workspace), sort_keys=True, allow_nan=False))

    @app.command("resolve-deferred")
    @guard
    def resolve_handoff(
        workspace: Path = typer.Argument(Path(".")),
        reason: str = typer.Option(..., "--reason"),
    ) -> None:
        """Release an inspected deferred request without retrying its effects."""
        print(json.dumps(resolve_deferred(workspace, reason), sort_keys=True, allow_nan=False))

    @app.command("resolve-interrupted")
    @guard
    def resolve(
        workspace: Path = typer.Argument(Path(".")),
        action_id: str = typer.Option(..., "--action-id"),
        reason: str = typer.Option(..., "--reason"),
    ) -> None:
        """Mark one interrupted action failed after inspecting its side effects."""
        print(json.dumps(resolve_interrupted(workspace, action_id, reason), sort_keys=True, allow_nan=False))

    @app.command("seal-submitted")
    @guard
    def seal(
        workspace: Path = typer.Argument(Path(".")),
        confirm: bool = typer.Option(False, "--confirm", help="confirm paid final sealed evaluation"),
    ) -> None:
        """Run Seal after the controller has exited by submitting a champion."""
        if not confirm:
            raise typer.BadParameter("--confirm is required", param_hint="--confirm")
        print(json.dumps(seal_submitted(workspace), sort_keys=True, allow_nan=False))

    return app


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")
