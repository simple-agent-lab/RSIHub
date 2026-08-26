import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import fixture_recipe_config, init_fixture_workspace, init_workspace_from_config

import evolve.runtime.process as runtime_module
from evolve import workspace as workspace_module
from evolve.config import scaffold_root
from evolve.evaluation import Outcome
from evolve.evaluation import execution as execution_module
from evolve.evaluation.execution import _expected_trials, evaluate
from evolve.evaluation.legacy import effective_task_set_identity, task_set_identity
from evolve.feedback import write_feedback_bundle
from evolve.runtime import attempt_dir


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_pid_exit(pid: int) -> bool:
    for _ in range(100):
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.01)
    return not _pid_is_alive(pid)


def test_owned_process_kills_child_group_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "spawn.py"
    script.write_text(
        "import pathlib, signal, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "signal.pause()\n"
    )

    result = runtime_module.run_owned(
        [sys.executable, str(script), str(pid_file)],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_s=1.0,
    )

    assert result.timed_out is True
    assert _wait_for_pid_exit(int(pid_file.read_text()))


def test_owned_process_cleans_group_before_propagating_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = KeyboardInterrupt("cancel")

    class InterruptedProcess:
        pid = 42
        calls = 0
        completed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise cancellation
            self.completed = True
            return "", ""

    process = InterruptedProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(runtime_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runtime_module, "_process_tree", lambda pid: {pid})
    monkeypatch.setattr(runtime_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(KeyboardInterrupt) as excinfo:
        runtime_module.run_owned(["ignored"], cwd=tmp_path, env={})

    assert excinfo.value is cancellation
    assert signals == [(42, signal.SIGTERM)]
    assert process.completed is True


def test_owned_process_escalates_cleanup_timeout_before_propagating_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = KeyboardInterrupt("cancel")

    class SlowCleanupProcess:
        pid = 43
        calls = 0
        completed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise cancellation
            if self.calls == 2:
                raise subprocess.TimeoutExpired("ignored", timeout)
            self.completed = True
            return "", ""

    process = SlowCleanupProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(runtime_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runtime_module, "_process_tree", lambda pid: {pid})
    monkeypatch.setattr(runtime_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(KeyboardInterrupt) as excinfo:
        runtime_module.run_owned(["ignored"], cwd=tmp_path, env={})

    assert excinfo.value is cancellation
    assert signals == [(43, signal.SIGTERM), (43, signal.SIGKILL)]
    assert process.completed is True


def test_signal_process_tree_uses_known_root_group_without_resolving_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list[int] = []
    signals: list[tuple[int, signal.Signals]] = []

    def getpgid(pid: int) -> int:
        lookups.append(pid)
        return pid

    monkeypatch.setattr(runtime_module.os, "getpgrp", lambda: 100)
    monkeypatch.setattr(runtime_module.os, "getpgid", getpgid)
    monkeypatch.setattr(runtime_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    runtime_module._signal_process_tree({42, 44}, signal.SIGTERM, fallback_group=42)

    assert lookups == [44]
    assert set(signals) == {(42, signal.SIGTERM), (44, signal.SIGTERM)}


def test_signal_process_tree_ignores_inaccessible_process_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inaccessible(_pid: int) -> int:
        raise PermissionError

    def denied(_group: int, _sig: signal.Signals) -> None:
        raise PermissionError

    monkeypatch.setattr(runtime_module.os, "getpgrp", lambda: 100)
    monkeypatch.setattr(runtime_module.os, "getpgid", inaccessible)
    monkeypatch.setattr(runtime_module.os, "killpg", denied)

    runtime_module._signal_process_tree({42, 44}, signal.SIGTERM, fallback_group=42)


def test_attempt_ids_include_full_attempt_and_workspace_identity(tmp_path: Path) -> None:
    first_workspace = tmp_path / "one" / "same-name"
    second_workspace = tmp_path / "two" / "same-name"
    candidate = attempt_dir(
        first_workspace,
        purpose="candidate",
        generation="7",
        candidate_commit="abc",
        attempt=1,
    )
    anchor = attempt_dir(
        first_workspace,
        purpose="anchor",
        generation="7",
        candidate_commit="abc",
        attempt=1,
    )
    other_candidate = attempt_dir(
        first_workspace,
        purpose="candidate",
        generation="7",
        candidate_commit="def",
        attempt=1,
    )
    other_workspace = attempt_dir(
        second_workspace,
        purpose="candidate",
        generation="7",
        candidate_commit="abc",
        attempt=1,
    )

    identifiers = [
        runtime_module.owned_attempt_id(first_workspace, candidate),
        runtime_module.owned_attempt_id(first_workspace, anchor),
        runtime_module.owned_attempt_id(first_workspace, other_candidate),
        runtime_module.owned_attempt_id(second_workspace, other_workspace),
    ]

    assert len(set(identifiers)) == 4
    assert identifiers[0] == runtime_module.owned_attempt_id(first_workspace, candidate)
    assert all(re.fullmatch(r"[a-z0-9_-]{1,80}", identifier) for identifier in identifiers)


def test_cleanup_removes_only_exact_trial_compose_project(tmp_path: Path) -> None:
    jobs = tmp_path / "current-jobs"
    trial = jobs / "trial"
    trial.mkdir(parents=True)
    (trial / "config.json").write_text('{"trial_name": "Task/ABC.1"}\n')
    for name, value in (("array", "[]"), ("null", "null"), ("string", '"trial"'), ("invalid", "{")):
        malformed = jobs / name
        malformed.mkdir()
        (malformed / "config.json").write_text(value)
    undecodable = jobs / "undecodable"
    undecodable.mkdir()
    (undecodable / "config.json").write_bytes(b"\xff\xfe")
    outside = tmp_path / "other-jobs" / "trial"
    outside.mkdir(parents=True)
    (outside / "config.json").write_text('{"trial_name": "unowned"}\n')
    calls = tmp_path / "docker-calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "printf 'CALL\\n' >> \"$DOCKER_CALLS\"\n"
        'printf \'<%s>\\n\' "$@" >> "$DOCKER_CALLS"\n'
        "if [ \"$1\" = ps ]; then printf 'owned-container\\n'; fi\n"
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    script = Path(scaffold_root()) / "evaluators" / "harbor" / "cleanup_harbor.py"

    result = subprocess.run(
        [sys.executable, str(script), str(jobs)],
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "DOCKER_CALLS": str(calls)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "CALL",
        "<ps>",
        "<-aq>",
        "<--filter>",
        "<label=com.docker.compose.project=task-abc-1__env>",
        "CALL",
        "<rm>",
        "<-f>",
        "<owned-container>",
    ]


def test_console_uses_locked_workspace_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_fixture_workspace(workspace)

    text = (workspace / "evolve").read_text()

    assert "EVOLVE_UV_BINARY" in text
    assert '--python "$EVOLVE_FRAMEWORK_PYTHON" python' in text
    assert 'run --project "$HERE" --frozen python' in text
    assert '"$HERE/.evolve/launch_evolve.py"' in text
    assert "PYTHONPATH" not in text
    assert (workspace / ".evolve" / "launch_evolve.py").is_file()
    assert (workspace / "evaluator" / "cleanup_harbor.py").is_file()


def test_workspace_agent_guidance_distinguishes_python_from_dependency_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_fixture_workspace(workspace)

    guidance = (workspace / "AGENTS.md").read_text()

    assert "UV_PYTHON_DOWNLOADS=never" in guidance
    assert "Set `UV_OFFLINE=1` only after" in guidance
    assert "empty dependency cache cannot run offline" in guidance
    assert "infrastructure/provisioning failure" in guidance


def test_init_and_feedback_do_not_create_prediction_contracts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_fixture_workspace(workspace)
    archive_row = json.loads((workspace / "archive.jsonl").read_text())
    run_dir = workspace / "runs" / "contract-check"

    manifest = write_feedback_bundle(workspace=workspace, run_dir=run_dir)

    assert "predicted_fixes" not in archive_row
    assert "verified_fixes" not in archive_row
    assert not (run_dir / "feedback" / "falsification.md").exists()
    assert "feedback/falsification.md" not in manifest


def test_console_shell_quotes_unusual_uv_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "uv-args"
    fake_uv = tmp_path / "uv-$(touch PWNED)-`touch BACKTICK`-'quoted-\\path"
    fake_uv.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURE"\n')
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    workspace = tmp_path / "workspace"
    init_fixture_workspace(workspace)
    env = os.environ.copy()
    env["EVOLVE_UV_BINARY"] = str(fake_uv)
    env["CAPTURE"] = str(capture)

    result = subprocess.run(
        [str(workspace / "evolve"), "probe"],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "run",
        "--project",
        str(workspace),
        "--frozen",
        "python",
        str(workspace / ".evolve" / "launch_evolve.py"),
        "probe",
    ]
    assert not (workspace / "PWNED").exists()
    assert not (workspace / "BACKTICK").exists()


def test_attempt_paths_never_replace_prior_evidence(tmp_path: Path) -> None:
    first = attempt_dir(
        tmp_path,
        purpose="candidate",
        generation="7",
        candidate_commit="abc",
        attempt=1,
    )
    first.mkdir(parents=True)
    (first / "marker").write_text("first")

    second = attempt_dir(
        tmp_path,
        purpose="candidate",
        generation="7",
        candidate_commit="abc",
        attempt=2,
    )

    assert first == tmp_path / "runs/evaluations/candidate/gen-7/candidate-abc/attempt-1"
    assert second != first
    assert (first / "marker").read_text() == "first"
    with pytest.raises(FileExistsError, match="attempt already exists"):
        attempt_dir(
            tmp_path,
            purpose="candidate",
            generation="7",
            candidate_commit="abc",
            attempt=1,
        )


@pytest.mark.parametrize("value", ["../escape", "a/b", "", "."])
def test_attempt_identity_rejects_unsafe_path_components(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        attempt_dir(
            tmp_path,
            purpose=value,
            generation="7",
            candidate_commit="abc",
            attempt=1,
        )


def test_init_commits_evaluator_owned_runtime_pin(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    init_fixture_workspace(workspace)

    runtime = json.loads((workspace / "evaluator/runtime.json").read_text())
    assert (workspace / "evaluator/runtime.pin").read_text() == f"{runtime['digest']}\n"
    assert not (workspace / "target/runtime.pin").exists()


def test_default_expected_trials_match_generated_evaluator_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = fixture_recipe_config("hill_climb-smoke", "workspace")
    config["evaluator"].pop("tasks_per_round")
    config["evaluator"].pop("k", None)
    config["evaluator"]["repetitions"] = 2
    monkeypatch.setenv("EVAL_STUB", "1")
    workspace = tmp_path / "workspace"
    init_workspace_from_config(workspace_module.InitOptions(workspace), config)

    record = evaluate(workspace, "gen/0", "0", purpose="genesis")

    assert record.outcome is Outcome.BENCHMARK_COMPLETE
    assert record.expected_trials == 4
    assert len(record.trials) == 4


def test_anchor_expected_trials_use_selected_sealed_tasks() -> None:
    assert _expected_trials({"k": 2, "tasks_per_round": 4}, None, selected_tasks=1) == 2


def test_expected_trials_use_repetitions_for_new_configs() -> None:
    assert _expected_trials({"repetitions": 3, "tasks_per_round": 4}, None, selected_tasks=2) == 6


def test_effective_task_identity_uses_limited_split_members(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    evaluator_dir = checkout / "evaluator"
    evaluator_dir.mkdir(parents=True)
    (evaluator_dir / "splits.json").write_text(
        json.dumps(
            {
                "version": 1,
                "resolved": True,
                "sampling": "static",
                "gate_tasks_per_round": 0,
                "tasks": {"train": ["third", "first", "second"], "gate": [], "sealed": []},
            }
        )
    )
    evaluator = {"dataset": "fixture", "evaluation_split": "train", "k": 2}

    identity = effective_task_set_identity(checkout, evaluator, purpose="candidate", task_limit=1)

    assert identity.members == ("third",)
    assert _expected_trials(evaluator, 1, selected_tasks=len(identity.members)) == 2


def test_effective_task_identity_limits_declared_task_file_before_canonicalizing(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    task_file = checkout / "evaluator" / "tasks.txt"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("# declared order\nthird\nfirst\nsecond\n")
    evaluator = {"dataset": "fixture", "task_file": "evaluator/tasks.txt", "k": 1}

    identity = effective_task_set_identity(checkout, evaluator, task_limit=2)

    assert identity.members == ("first", "third")


def test_runtime_task_selection_must_match_planned_members_after_normalization(tmp_path: Path) -> None:
    identity = task_set_identity("fixture", 1, ("first", "third"))
    selection = tmp_path / "task-split.json"
    selection.write_text(json.dumps({"split": "train", "tasks": ["third", "first"]}))

    assert execution_module._runtime_selection_matches(tmp_path, identity) is True

    selection.write_text(json.dumps({"split": "train", "tasks": ["third"]}))
    assert execution_module._runtime_selection_matches(tmp_path, identity) is False


def test_runtime_task_selection_accepts_unique_harbor_qualified_names(tmp_path: Path) -> None:
    identity = task_set_identity("fixture", 1, ("tau3-banking_knowledge-task-001",))
    selection = tmp_path / "task-split.json"
    selection.write_text(
        json.dumps(
            {
                "split": "train",
                "tasks": ["sierra-research/tau3-bench__tau3-banking_knowledge-task-001"],
            }
        )
    )

    assert execution_module._runtime_selection_matches(tmp_path, identity) is True


def test_runtime_task_selection_rejects_ambiguous_harbor_suffix(tmp_path: Path) -> None:
    identity = task_set_identity("fixture", 1, ("task-001", "suite__task-001"))
    selection = tmp_path / "task-split.json"
    selection.write_text(json.dumps({"split": "train", "tasks": ["vendor__suite__task-001"]}))

    assert execution_module._runtime_selection_matches(tmp_path, identity) is False
