import json
import re
import shlex
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    fixture_recipe_config,
    git,
    init_fixture_workspace,
    init_workspace_from_config,
    run_evolve,
)

from evolve.candidate import smoke as candidate_smoke_module
from evolve.candidate.smoke import SmokeMode, run_candidate_smoke
from evolve.runtime.uv import CandidateRuntimeResult, RuntimeMount
from evolve.workspace import InitOptions

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def smoke_checkout(
    tmp_path: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    rc: int = 0,
    create_script: bool = True,
) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "target").mkdir(parents=True)
    (checkout / "target" / "candidate.txt").write_text("candidate\n")
    (checkout / ".gitignore").write_text("runs/\n")
    (checkout / "evolve.yaml").write_text("surface:\n  include: [target/**]\n  exclude: []\n")
    if create_script:
        _write_executable(
            checkout / "evaluator" / "smoke.sh",
            "#!/bin/sh\n"
            "set -eu\n"
            f"printf '%s' {shlex.quote(stdout)}\n"
            f"printf '%s' {shlex.quote(stderr)} >&2\n"
            f"exit {rc}\n",
        )
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "test")
    git(checkout, "config", "user.email", "test@example.invalid")
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "parent")
    return checkout


def test_smoke_exposes_missing_module_from_snapshot(tmp_path: Path) -> None:
    checkout = smoke_checkout(tmp_path, stderr="ModuleNotFoundError: No module named 'fastapi'\n", rc=2)

    result = run_candidate_smoke(checkout, workspace=checkout)

    assert result.status == "failed"
    assert "No module named 'fastapi'" in result.stderr_path.read_text()


def test_smoke_runs_through_owned_process_helper(tmp_path: Path, monkeypatch) -> None:
    checkout = smoke_checkout(tmp_path)
    calls: list[tuple[list[str], dict[str, str]]] = []
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(candidate_smoke_module.time, "monotonic", lambda: next(ticks))

    def fake_run_owned(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_s: float | None = None,
    ) -> SimpleNamespace:
        calls.append((command, env))
        return SimpleNamespace(returncode=0, stdout="owned\n", stderr="", wall_s=0.01, timed_out=False)

    monkeypatch.setattr(candidate_smoke_module, "run_owned", fake_run_owned, raising=False)

    result = run_candidate_smoke(checkout, workspace=checkout)

    assert result.status == "passed"
    assert result.stdout_path.read_text() == "owned\n"
    assert json.loads((result.attempt_dir / "result.json").read_text())["duration_s"] == 2.0
    assert len(calls) == 1
    assert calls[0][1]["EVOLVE_ATTEMPT_ID"] == candidate_smoke_module.owned_attempt_id(
        checkout,
        result.attempt_dir,
    )
    assert calls[0][1]["EVOLVE_CANDIDATE_SMOKE_MODE"] == "full"


def test_model_smoke_uses_detached_snapshot_without_workspace_mutation(tmp_path: Path, monkeypatch) -> None:
    checkout = smoke_checkout(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_owned(command, *, cwd, env, timeout_s=None):
        del command, timeout_s
        captured["cwd"] = cwd
        captured["env"] = env
        return SimpleNamespace(returncode=0, stdout="", stderr="", wall_s=0.01, timed_out=False)

    monkeypatch.setattr(candidate_smoke_module, "run_owned", fake_run_owned)
    before = git(checkout, "write-tree")

    result = run_candidate_smoke(
        checkout,
        workspace=checkout,
        mode=SmokeMode.MODEL,
    )

    assert result.status == "passed"
    assert git(checkout, "write-tree") == before
    assert captured["cwd"] != checkout
    assert captured["env"]["EVOLVE_CANDIDATE_SMOKE_MODE"] == "single"
    assert captured["env"]["EVOLVE_EVAL_SPLIT"] == "gate"
    payload = json.loads((result.attempt_dir / "result.json").read_text())
    assert payload["mode"] == "single"
    assert "stdout_path" not in payload
    assert "stderr_path" not in payload
    assert payload["artifacts"]["stdout"]["path"] == "stdout.log"
    assert payload["artifacts"]["stderr"]["path"] == "stderr.log"
    assert len(payload["artifacts"]["stdout"]["sha256"]) == 64
    assert stat.S_IMODE(result.attempt_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.stdout_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.stderr_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((result.attempt_dir / "result.json").stat().st_mode) == 0o600


def test_harbor_smoke_fails_when_task_result_contains_exception(tmp_path: Path, monkeypatch) -> None:
    checkout = smoke_checkout(tmp_path)
    (checkout / "evolve.yaml").write_text(
        "surface:\n  include: [target/**]\n  exclude: []\nevaluator:\n  engine: harbor\n"
    )
    git(checkout, "add", "evolve.yaml")
    git(checkout, "commit", "--amend", "--no-edit", "-q")

    def fake_run_owned(command, *, cwd, env, timeout_s=None):
        del command, cwd, timeout_s
        job = Path(env["EVOLVE_RUN_DIR"]) / "jobs" / "job-1"
        trial = job / "task-001__trial"
        trial.mkdir(parents=True)
        (job / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": 1,
                    "stats": {
                        "n_errored_trials": 1,
                        "n_cancelled_trials": 0,
                        "n_pending_trials": 0,
                        "n_running_trials": 0,
                    },
                }
            )
        )
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "dataset__task-001",
                    "exception_info": {
                        "exception_type": "EvolveRuntimeInfrastructureError",
                        "exception_message": "source directory setup failed\nsecret traceback",
                    },
                }
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="", wall_s=0.01, timed_out=False)

    monkeypatch.setattr(candidate_smoke_module, "run_owned", fake_run_owned)

    result = run_candidate_smoke(checkout, workspace=checkout, mode=SmokeMode.MODEL)

    assert result.status == "failed"
    assert result.returncode == 0
    stderr = result.stderr_path.read_text()
    assert "EVOLVE_HARBOR_SMOKE_FAILED" in stderr
    assert "EvolveRuntimeInfrastructureError: source directory setup failed" in stderr
    assert "secret traceback" not in stderr
    payload = json.loads((result.attempt_dir / "result.json").read_text())
    assert payload["harbor_task_audit"] == {
        "active_trials": 0,
        "cancelled_trials": 0,
        "errored_trials": 1,
        "expected_trials": 1,
        "invalid_results": 0,
        "job_results": 1,
        "required": True,
        "status": "failed",
        "task_exception_count": 1,
        "task_results": 1,
    }


def test_harbor_smoke_passes_only_with_complete_exception_free_task_result(tmp_path: Path, monkeypatch) -> None:
    checkout = smoke_checkout(tmp_path)
    (checkout / "evolve.yaml").write_text(
        "surface:\n  include: [target/**]\n  exclude: []\nevaluator:\n  engine: harbor\n"
    )
    git(checkout, "add", "evolve.yaml")
    git(checkout, "commit", "--amend", "--no-edit", "-q")

    def fake_run_owned(command, *, cwd, env, timeout_s=None):
        del command, cwd, timeout_s
        job = Path(env["EVOLVE_RUN_DIR"]) / "jobs" / "job-1"
        trial = job / "task-001__trial"
        trial.mkdir(parents=True)
        (job / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": 1,
                    "stats": {
                        "n_errored_trials": 0,
                        "n_cancelled_trials": 0,
                        "n_pending_trials": 0,
                        "n_running_trials": 0,
                    },
                }
            )
        )
        (trial / "result.json").write_text(json.dumps({"task_name": "dataset__task-001", "exception_info": None}))
        return SimpleNamespace(returncode=0, stdout="", stderr="", wall_s=0.01, timed_out=False)

    monkeypatch.setattr(candidate_smoke_module, "run_owned", fake_run_owned)

    result = run_candidate_smoke(checkout, workspace=checkout, mode=SmokeMode.MODEL)

    assert result.status == "passed"
    payload = json.loads((result.attempt_dir / "result.json").read_text())
    assert payload["harbor_task_audit"]["status"] == "passed"
    assert payload["harbor_task_audit"]["task_results"] == 1


def test_harbor_smoke_fails_when_successful_process_writes_no_task_results(tmp_path: Path, monkeypatch) -> None:
    checkout = smoke_checkout(tmp_path)
    (checkout / "evolve.yaml").write_text(
        "surface:\n  include: [target/**]\n  exclude: []\nevaluator:\n  engine: harbor\n"
    )
    git(checkout, "add", "evolve.yaml")
    git(checkout, "commit", "--amend", "--no-edit", "-q")
    monkeypatch.setattr(
        candidate_smoke_module,
        "run_owned",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr="", wall_s=0.01, timed_out=False),
    )

    result = run_candidate_smoke(checkout, workspace=checkout, mode=SmokeMode.MODEL)

    assert result.status == "failed"
    assert "Harbor produced no task result artifacts" in result.stderr_path.read_text()


def test_smoke_prepares_and_injects_candidate_runtime(tmp_path: Path, monkeypatch) -> None:
    checkout = smoke_checkout(tmp_path)
    (checkout / "evolve.yaml").write_text(
        "surface:\n"
        "  include: [target/**]\n"
        "  exclude: []\n"
        "evaluator:\n"
        "  candidate_runtime: {variant: uv, project: target, python: '3.12'}\n"
    )
    git(checkout, "add", "evolve.yaml")
    git(checkout, "commit", "--amend", "--no-edit", "-q")
    runtime_calls = []
    run_env = {}

    def fake_prepare(materialized, attempt, runtime_root, candidate_commit, evaluator, *, env=None):
        del env
        runtime_calls.append((materialized, attempt, runtime_root, candidate_commit, evaluator))
        return CandidateRuntimeResult(
            "uv",
            "target",
            environment=(("UV_OFFLINE", "1"),),
            mounts=(RuntimeMount(tmp_path / "cache", "/opt/evolve/uv/cache"),),
        )

    def fake_run_owned(command, *, cwd, env, timeout_s=None):
        del command, cwd, timeout_s
        run_env.update(env)
        return SimpleNamespace(returncode=0, stdout="", stderr="", wall_s=0.01, timed_out=False)

    monkeypatch.setattr(candidate_smoke_module, "prepare_candidate_runtime", fake_prepare, raising=False)
    monkeypatch.setattr(candidate_smoke_module, "run_owned", fake_run_owned)

    result = run_candidate_smoke(checkout, workspace=checkout)

    assert result.status == "passed"
    assert len(runtime_calls) == 1
    assert runtime_calls[0][1] == result.attempt_dir
    assert runtime_calls[0][2] == checkout / "runs" / "runtime"
    assert runtime_calls[0][4]["candidate_runtime"]["variant"] == "uv"
    assert json.loads(run_env["EVOLVE_CANDIDATE_RUNTIME_ENV_JSON"]) == {"UV_OFFLINE": "1"}
    assert json.loads(run_env["EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON"])[0]["target"] == "/opt/evolve/uv/cache"
    assert (result.attempt_dir / "runtime-agent.env").is_file()
    assert (result.attempt_dir / "runtime-verifier.env").is_file()
    assert (result.attempt_dir / "runtime-environment-evidence.json").is_file()


def test_smoke_redacts_proxy_credential_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@example.invalid:8080")
    checkout = smoke_checkout(tmp_path, stderr="http://user:secret@example.invalid:8080 fastapi\n", rc=2)

    text = run_candidate_smoke(checkout, workspace=checkout).stderr_path.read_text()

    assert "secret" not in text
    assert "fastapi" in text


def test_smoke_redacts_model_endpoint_from_logs(tmp_path: Path, monkeypatch) -> None:
    endpoint = "https://model-sensitive.example/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    checkout = smoke_checkout(tmp_path, stderr=f"request failed against {endpoint}\n", rc=2)

    text = run_candidate_smoke(checkout, workspace=checkout).stderr_path.read_text()

    assert endpoint not in text
    assert "request failed against [REDACTED]" in text


def test_smoke_without_evaluator_script_is_unsupported(tmp_path: Path) -> None:
    checkout = smoke_checkout(tmp_path, create_script=False)

    assert run_candidate_smoke(checkout, workspace=checkout).status == "unsupported"


def test_smoke_executes_uncommitted_snapshot_in_detached_checkout(tmp_path: Path) -> None:
    checkout = smoke_checkout(tmp_path)
    _write_executable(
        checkout / "evaluator" / "smoke.sh",
        "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$PWD\"\ncat target/candidate.txt\n",
    )
    git(checkout, "add", "evaluator/smoke.sh")
    git(checkout, "commit", "--amend", "--no-edit", "-q")
    (checkout / "target" / "candidate.txt").write_text("uncommitted candidate\n")

    result = run_candidate_smoke(checkout, workspace=checkout)

    lines = result.stdout_path.read_text().splitlines()
    assert result.status == "passed"
    assert lines[0] != str(checkout)
    assert lines[1] == "uncommitted candidate"
    assert result.snapshot_tree == git(checkout, "rev-parse", f"{result.snapshot_tree}^{{tree}}")


def test_smoke_redacts_secret_environment_values_but_preserves_traceback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sensitive-value")
    checkout = smoke_checkout(
        tmp_path,
        stderr="token setup failed for sk-sensitive-value\nTraceback: useful frame 17\n",
        rc=1,
    )

    text = run_candidate_smoke(checkout, workspace=checkout).stderr_path.read_text()

    assert "sk-sensitive-value" not in text
    assert "token setup failed for [REDACTED]" in text
    assert "Traceback: useful frame 17" in text


def test_smoke_redacts_common_secret_forms_without_rewriting_diagnostics(tmp_path: Path) -> None:
    checkout = smoke_checkout(
        tmp_path,
        stderr="request failed for sk-standalone-secret token=standalone-token-value\nImportError: useful module\n",
        rc=1,
    )

    text = run_candidate_smoke(checkout, workspace=checkout).stderr_path.read_text()

    assert "sk-standalone-secret" not in text
    assert "standalone-token-value" not in text
    assert "request failed for [REDACTED] token=[REDACTED]" in text
    assert "ImportError: useful module" in text


def test_candidate_smoke_cli_returns_three_when_unsupported(tmp_path: Path) -> None:
    checkout = smoke_checkout(tmp_path, create_script=False)

    result = run_evolve(
        "candidate-smoke",
        "--full",
        "--checkout",
        str(checkout),
        env={"EVOLVE_WORKSPACE": str(checkout)},
    )

    assert result.returncode == 3
    assert "candidate-smoke: unsupported" in result.stdout


def test_init_generates_executable_smoke_only_for_harbor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVOLVE_HOME", str(tmp_path / "evolve-home"))
    harbor = tmp_path / "harbor"
    init_fixture_workspace(harbor)

    smoke = harbor / "evaluator" / "smoke.sh"
    assert smoke.read_text() == (
        "#!/bin/sh\n"
        "set -eu\n"
        ': "${EVOLVE_RUN_DIR:?EVOLVE_RUN_DIR is required}"\n'
        "export EVOLVE_CANDIDATE_SMOKE_MODE=full\n"
        "exec ./evaluator/eval.sh\n"
    )
    assert smoke.stat().st_mode & stat.S_IXUSR

    local_config = fixture_recipe_config("hill_climb-smoke", "local")
    assert isinstance(local_config["evaluator"], dict)
    local_config["evaluator"]["engine"] = "local"
    local_config["evaluator"].pop("agent", None)
    local = tmp_path / "local"
    with pytest.raises(ValueError, match="unsupported evaluator.engine: local"):
        init_workspace_from_config(InitOptions(workspace=local), local_config)

    assert not (local / "evaluator" / "smoke.sh").exists()
