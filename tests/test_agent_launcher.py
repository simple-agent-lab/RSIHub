import json
import sys
from pathlib import Path

import pytest
from conftest import init_workspace, run_evolve

from evolve.agent_driver import AgentLimits, start_session
from evolve.agent_launcher import (
    ControllerConfig,
    ControllerLimits,
    controller_status,
    launch_controller,
    resolve_controller_interrupted,
)


def _session(tmp_path: Path) -> Path:
    workspace, evolve_home = init_workspace(tmp_path)
    evaluated = run_evolve(
        "eval",
        str(workspace),
        "0",
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert evaluated.returncode == 0, evaluated.stderr
    start_session(workspace, AgentLimits(10, 2, 2, max_cost_usd=10, max_wall_s=3600))
    return workspace


def _controller(path: Path, *, invalid_first: bool = False, cost: str = "0.25") -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "from pathlib import Path\n"
        "attempt = int(os.environ['EVOLVE_CONTROLLER_ATTEMPT'])\n"
        "receipt = Path(os.environ['EVOLVE_CONTROLLER_USAGE_RECEIPT'])\n"
        + ("receipt.write_text('invalid') if attempt == 1 else " if invalid_first else "")
        + f"receipt.write_text(json.dumps({{'total_tokens': 10, 'cost_usd': {cost}}}))\n"
        "print(f'controller attempt {attempt}')\n"
    )
    path.chmod(0o755)
    return path


def test_controller_launcher_accumulates_usage_and_enforces_global_budget(tmp_path: Path) -> None:
    workspace = _session(tmp_path)
    script = _controller(tmp_path / "controller.py")
    config = ControllerConfig(
        str(script),
        (),
        ControllerLimits(max_attempts=3, max_tokens=15, max_cost_usd=1, max_wall_s=30, max_total_cost_usd=2),
    )

    first = launch_controller(workspace, config)
    assert first["status"] == "ready"
    assert first["controller_tokens"] == 10
    assert first["controller_cost_usd"] == 0.25
    second = launch_controller(workspace, config)
    assert second["status"] == "exhausted"
    assert second["exhausted_reasons"] == ["controller_tokens"]
    assert second["controller_tokens"] == 20
    assert (workspace / "runs/agent-driven/controller/attempt-2/stdout.log").read_text() == "controller attempt 2\n"
    with pytest.raises(RuntimeError, match="controller_tokens"):
        launch_controller(workspace, config)


def test_controller_launcher_requires_usage_receipt_and_can_resume(tmp_path: Path) -> None:
    workspace = _session(tmp_path)
    script = _controller(tmp_path / "controller.py", invalid_first=True)
    config = ControllerConfig(str(script), (), ControllerLimits(max_attempts=3))

    with pytest.raises(RuntimeError, match="valid usage receipt"):
        launch_controller(workspace, config)
    assert controller_status(workspace)["attempts_used"] == 1
    assert launch_controller(workspace, config)["attempts_used"] == 2


def test_controller_launcher_retains_unpriced_token_usage_without_a_dollar_limit(tmp_path: Path) -> None:
    workspace = _session(tmp_path)
    script = _controller(tmp_path / "controller.py", cost="None")
    config = ControllerConfig(str(script), (), ControllerLimits(max_attempts=2, max_tokens=100))

    status = launch_controller(workspace, config)

    assert status["controller_tokens"] == 10
    assert status["controller_cost_usd"] is None
    assert status["unpriced_controller_attempts"] == 1
    assert status["total_observed_cost_usd"] is None


def test_controller_interruption_requires_explicit_resolution(tmp_path: Path) -> None:
    workspace = _session(tmp_path)
    script = _controller(tmp_path / "controller.py")
    config = ControllerConfig(str(script), (), ControllerLimits(max_attempts=3))
    root = workspace / "runs/agent-driven/controller"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "command": str(script),
                "arguments": [],
                "limits": {
                    "max_attempts": 3,
                    "max_tokens": None,
                    "max_cost_usd": None,
                    "max_wall_s": None,
                    "max_total_cost_usd": None,
                },
            }
        )
    )
    (root / "attempts.jsonl").write_text(
        json.dumps({"schema_version": 1, "phase": "started", "attempt": 1, "timestamp": "now"}) + "\n"
    )

    with pytest.raises(RuntimeError, match="was interrupted"):
        launch_controller(workspace, config)
    resolved = resolve_controller_interrupted(workspace, 1, "host restarted", total_tokens=12, wall_s=3)
    assert resolved["phase"] == "failed"
    assert resolved["usage"] == {"total_tokens": 12, "cost_usd": None}
    assert launch_controller(workspace, config)["attempts_used"] == 2


def test_controller_cli_runs_without_a_shell(tmp_path: Path) -> None:
    workspace = _session(tmp_path)
    script = _controller(tmp_path / "controller.py")

    result = run_evolve(
        "agent",
        "run-controller",
        str(workspace),
        "--controller",
        sys.executable,
        "--controller-arg",
        str(script),
        "--max-attempts",
        "1",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["attempts_used"] == 1
