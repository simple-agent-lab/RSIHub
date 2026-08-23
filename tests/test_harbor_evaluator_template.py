import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from harbor.agents.installed.codex import Codex

from evolve.config import scaffold_root
from evolve.splits import build_manifest
from evolve.workspace import _eval_env, _eval_sh


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_evaluator_helpers(evaluator: Path) -> None:
    for name in ("harbor_artifacts.py", "parse_score.py", "cleanup_harbor.py"):
        (evaluator / name).write_text((scaffold_root() / "evaluators" / "harbor" / name).read_text())


def _write_fake_uv(bin_dir: Path) -> None:
    _write_executable(
        bin_dir / "uv",
        "#!/bin/sh\n"
        '[ "$1" = run ] || exit 90\n'
        "shift\n"
        '[ "$1" = --project ] || exit 91\n'
        "shift 2\n"
        '[ "$1" = --frozen ] || exit 92\n'
        "shift\n"
        '[ "$1" = --python ] || exit 93\n'
        "shift\n"
        '[ "$1" = "$EVOLVE_FRAMEWORK_PYTHON" ] || exit 94\n'
        "shift\n"
        'exec "$@"\n',
    )


def test_harbor_evaluator_uses_locked_workspace_runtime() -> None:
    text = _eval_sh("harbor", "fixture")

    assert "PYTHONPATH" not in text
    assert text.count('--python "$EVOLVE_FRAMEWORK_PYTHON"') == 5
    assert 'python "$PWD/.evolve/launch_splits.py"' in text
    assert 'harbor "$@"' in text
    assert '"$PWD/.evolve/launch_splits.py"' in text
    assert "export EVOLVE_UV_BINARY" in text


def test_harbor_score_parser_uses_framework_python(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.8,
            agent="custom:Agent",
        )
    )
    (evaluator / "splits.json").write_text(json.dumps({"resolved": False}))
    _write_evaluator_helpers(evaluator)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(
        fake_bin / "python3",
        "#!/bin/sh\n"
        'if [ "${1:-}" = evaluator/parse_score.py ]; then\n'
        "  printf 'system python is incompatible\\n' >&2\n"
        "  exit 97\n"
        "fi\n"
        'exec "$EVOLVE_FRAMEWORK_PYTHON" "$@"\n',
    )
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --jobs-dir ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial"\n'
        'printf \'%s\\n\' \'{"task_name":"task-a","trial_name":"trial","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial/result.json"\n',
    )
    run_dir = tmp_path / "run"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "EVOLVE_RUN_DIR": str(run_dir),
        "EVOLVE_ATTEMPT_ID": "parser-runtime-test",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "EVOLVE_CANDIDATE_RUNTIME_ENV_JSON": "{}",
        "EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": "[]",
    }

    result = subprocess.run(
        [str(evaluator / "eval.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert (run_dir / "status").read_text() == "complete\n"


def test_local_execution_runtime_refuses_implicit_docker_fallback(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    run_dir = tmp_path / "run"
    evaluator.mkdir()
    run_dir.mkdir()
    (evaluator / "eval.env").write_text("EVOLVE_EXECUTION_BACKEND=local\n")
    script = evaluator / "eval.sh"
    _write_executable(script, _eval_sh("harbor", "fixture"))

    result = subprocess.run(
        [str(script)],
        cwd=tmp_path,
        env={**os.environ, "EVOLVE_RUN_DIR": str(run_dir)},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 3
    assert "refusing Docker fallback" in result.stderr
    assert (run_dir / "status").read_text() == "infra_failed\n"


def test_harbor_evaluator_runs_resolved_tasks_from_the_verified_snapshot() -> None:
    text = _eval_sh("harbor", "fixture")

    assert 'dataset_snapshot="$EVOLVE_RUN_DIR/task-dataset"' in text
    assert "EVOLVE_HARBOR_TASKS=$dataset_snapshot" in text
    assert "EVOLVE_HARBOR_DATASET_MODE=path" in text
    assert "cleanup_dataset_snapshot" in text


def test_harbor_evaluator_snapshot_closes_source_mutation_window(tmp_path: Path) -> None:
    dataset = tmp_path / "tasks"
    task = dataset / "task-a"
    task.mkdir(parents=True)
    source_task_file = task / "task.toml"
    source_task_file.write_text('version = "1.0"\nsource = "frozen"\n')
    manifest = build_manifest(
        str(dataset),
        {"train": 0.0, "gate": 1.0, "sealed": 0.0, "seed": 1},
        base_dir=tmp_path,
        sampling="static",
        gate_limit=1,
    )
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            str(dataset),
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.8,
            agent="custom:Agent",
        )
    )
    (evaluator / "splits.json").write_text(json.dumps(manifest))
    _write_evaluator_helpers(evaluator)
    launcher = tmp_path / ".evolve" / "launch_splits.py"
    launcher.parent.mkdir()
    launcher.write_text((scaffold_root() / "workspace" / "launch_splits.py").read_text())

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "uv",
        "#!/bin/sh\n"
        '[ "$1" = run ] || exit 90\n'
        "shift\n"
        '[ "$1" = --project ] || exit 91\n'
        "shift 2\n"
        '[ "$1" = --frozen ] || exit 92\n'
        "shift\n"
        '[ "$1" = --python ] || exit 93\n'
        "shift\n"
        '[ "$1" = "$EVOLVE_FRAMEWORK_PYTHON" ] || exit 94\n'
        "shift\n"
        'if [ "$1" = python ]; then\n'
        "  shift\n"
        '  "$EVOLVE_FRAMEWORK_PYTHON" "$@"\n'
        "  command_rc=$?\n"
        "  printf '%s\\n' 'version = \"2.0\"' 'source = \"mutated\"' > \"$SOURCE_TASK_FILE\"\n"
        '  exit "$command_rc"\n'
        "fi\n"
        'exec "$@"\n',
    )
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        "dataset_path=\n"
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-p" ]; then shift; dataset_path=$1; fi\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'printf \'%s\\n\' "$dataset_path" > "$HARBOR_DATASET_CAPTURE"\n'
        'cat "$dataset_path/task-a/task.toml" > "$HARBOR_CONTENT_CAPTURE"\n'
        'mkdir -p "$jobs_dir/trial"\n'
        'printf \'%s\\n\' \'{"task_name":"task-a","trial_name":"trial","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial/result.json"\n',
    )
    _write_executable(fake_bin / "docker", "#!/bin/sh\nexit 0\n")
    run_dir = tmp_path / "run"
    dataset_capture = tmp_path / "dataset-capture"
    content_capture = tmp_path / "content-capture"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "EVOLVE_RUN_DIR": str(run_dir),
        "EVOLVE_ATTEMPT_ID": "snapshot-test",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "EVOLVE_CANDIDATE_RUNTIME_ENV_JSON": "{}",
        "EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": "[]",
        "SOURCE_TASK_FILE": str(source_task_file),
        "HARBOR_DATASET_CAPTURE": str(dataset_capture),
        "HARBOR_CONTENT_CAPTURE": str(content_capture),
    }

    result = subprocess.run([str(evaluator / "eval.sh")], cwd=tmp_path, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert dataset_capture.read_text().strip() == str(run_dir / "task-dataset")
    assert content_capture.read_text() == 'version = "1.0"\nsource = "frozen"\n'
    assert source_task_file.read_text() == 'version = "2.0"\nsource = "mutated"\n'
    assert not (run_dir / "task-dataset").exists()


def test_harbor_evaluator_passes_agent_timeout_multiplier() -> None:
    text = _eval_sh("harbor", "fixture")

    assert '--agent-timeout-multiplier "$EVOLVE_HARBOR_AGENT_TIMEOUT_MULTIPLIER"' in text


def test_harbor_evaluator_passes_verifier_timeout_multiplier() -> None:
    text = _eval_sh("harbor", "fixture")

    assert '--verifier-timeout-multiplier "$EVOLVE_HARBOR_VERIFIER_TIMEOUT_MULTIPLIER"' in text


def test_harbor_evaluator_ignores_ambient_frozen_control_overrides(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.9,
            agent="custom:Agent",
            setup_timeout_multiplier=1,
            agent_timeout_multiplier=1,
            verifier_timeout_multiplier=1,
            max_retries=0,
        )
    )
    (evaluator / "agent.env").write_text("")
    (evaluator / "verifier.env").write_text("")
    (evaluator / "environment.kwargs").write_text("")
    (evaluator / "splits.json").write_text('{"resolved":false}\n')
    _write_evaluator_helpers(evaluator)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial"\n'
        'printf \'%s\\n\' \'{"task_name":"task","trial_name":"trial","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial/result.json"\n'
        "exit 7\n",
    )
    _write_executable(fake_bin / "docker", "#!/bin/sh\nexit 0\n")

    args_capture = tmp_path / "args"
    run_dir = tmp_path / "run"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "EVOLVE_RUN_DIR": str(run_dir),
        "EVOLVE_ATTEMPT_ID": "ambient-override-test",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "EVOLVE_CANDIDATE_RUNTIME_ENV_JSON": "{}",
        "EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": "[]",
        "EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE": "2",
        "EVOLVE_HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER": "9",
        "EVOLVE_HARBOR_AGENT_TIMEOUT_MULTIPLIER": "9",
        "EVOLVE_HARBOR_VERIFIER_TIMEOUT_MULTIPLIER": "9",
        "EVOLVE_HARBOR_MAX_RETRIES": "9",
        "EVOLVE_LIVE_OUTPUT": "1",
    }

    result = subprocess.run(
        [str(evaluator / "eval.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    args = args_capture.read_text().splitlines()
    assert args[args.index("-n") + 1] == "2"
    assert "--agent-setup-timeout-multiplier" not in args
    assert "--agent-timeout-multiplier" not in args
    assert "--verifier-timeout-multiplier" not in args
    assert "--max-retries" not in args
    metrics = json.loads((run_dir / "metrics.json").read_text())["dimensions"]
    assert metrics["harbor_rc"] == 7


def test_harbor_evaluator_forwards_workspace_openai_environment() -> None:
    text = _eval_sh("harbor", "fixture")

    assert "OPENAI_API_KEY OPENAI_BASE_URL OPENAI_API_BASE" in text
    assert 'set -- "$@" --ae "$credential_name=$credential_value"' in text


def test_harbor_evaluator_consumes_certified_runtime_environment_files() -> None:
    text = _eval_sh("harbor", "fixture")

    assert 'done < "$EVOLVE_RUN_DIR/runtime-agent.env"' in text
    assert 'done < "$EVOLVE_RUN_DIR/runtime-verifier.env"' in text
    # Subscription isolation is the final authority even if a runtime input is hostile.
    assert text.index('done < "$EVOLVE_RUN_DIR/runtime-agent.env"') < text.index(
        'if [ "${EVOLVE_HARBOR_CODEX_SUBSCRIPTION:-0}" = "1" ]; then'
    )


def test_harbor_evaluator_forwards_protected_agent_kwargs() -> None:
    text = _eval_sh("harbor", "fixture")

    assert "if [ -f evaluator/agent.kwargs ]; then" in text
    assert 'set -- "$@" --agent-kwarg "$agent_kwarg"' in text


def test_harbor_evaluator_isolates_codex_subscription_from_ambient_api_credentials(
    tmp_path: Path,
) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.8,
            agent="custom:Agent",
        )
    )
    (evaluator / "agent.kwargs").write_text("reasoning_effort=high\n")
    _write_evaluator_helpers(evaluator)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        '[ "$OPENAI_API_KEY" = "ambient-api-key" ] || exit 81\n'
        '[ "$OPENAI_BASE_URL" = "https://ambient-base.invalid/v1" ] || exit 82\n'
        '[ "$OPENAI_API_BASE" = "https://ambient-api-base.invalid/v1" ] || exit 83\n'
        'touch "$HARBOR_PARENT_ENV_MARKER"\n'
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial"\n'
        'printf \'%s\\n\' \'{"task_name":"task","trial_name":"trial","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial/result.json"\n',
    )
    _write_executable(fake_bin / "docker", "#!/bin/sh\nexit 0\n")

    args_capture = tmp_path / "args"
    parent_env_marker = tmp_path / "parent-env-retained"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "HARBOR_PARENT_ENV_MARKER": str(parent_env_marker),
        "EVOLVE_RUN_DIR": str(tmp_path / "run"),
        "EVOLVE_ATTEMPT_ID": "subscription-isolation",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "EVOLVE_CANDIDATE_RUNTIME_ENV_JSON": "{}",
        "EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": "[]",
    }
    env.update(
        {
            "EVOLVE_HARBOR_CODEX_SUBSCRIPTION": "1",
            "CODEX_FORCE_AUTH_JSON": "1",
            "OPENAI_API_KEY": "ambient-api-key",
            "OPENAI_BASE_URL": "https://ambient-base.invalid/v1",
            "OPENAI_API_BASE": "https://ambient-api-base.invalid/v1",
            "HTTP_PROXY": "http://proxy.invalid:8118",
            "HTTPS_PROXY": "http://proxy.invalid:8118",
        }
    )

    result = subprocess.run(
        [str(evaluator / "eval.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    args = args_capture.read_text().splitlines()
    assert args[args.index("--agent-kwarg") + 1] == "reasoning_effort=high"
    agent_environment = [args[index + 1] for index, value in enumerate(args) if value == "--ae"]
    openai_names = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"}
    assert [entry for entry in agent_environment if entry.partition("=")[0] in openai_names] == [
        "OPENAI_API_KEY=",
        "OPENAI_BASE_URL=",
        "OPENAI_API_BASE=",
    ]
    for ambient_value in (
        "ambient-api-key",
        "https://ambient-base.invalid/v1",
        "https://ambient-api-base.invalid/v1",
    ):
        assert all(ambient_value not in entry for entry in agent_environment)
    assert parent_env_marker.exists()
    assert "CODEX_FORCE_AUTH_JSON=1" in agent_environment
    assert "HTTP_PROXY=http://proxy.invalid:8118" in agent_environment
    assert "HTTPS_PROXY=http://proxy.invalid:8118" in agent_environment


def test_installed_harbor_codex_empty_extra_env_shadows_parent_base_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient-base.invalid/v1")

    agent = Codex(
        logs_dir=tmp_path,
        extra_env={"OPENAI_BASE_URL": ""},
    )

    assert agent._get_env("OPENAI_BASE_URL") == ""


def test_harbor_evaluator_prefers_explicit_agent_proxy_over_ambient_proxy(
    tmp_path: Path,
) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.8,
            agent="custom:Agent",
        )
    )
    (evaluator / "agent.env").write_text(
        "NO_PROXY=172.17.0.1,127.0.0.1,localhost\nno_proxy=172.17.0.1,127.0.0.1,localhost\n"
    )
    _write_evaluator_helpers(evaluator)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial"\n'
        'printf \'%s\\n\' \'{"task_name":"task","trial_name":"trial","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial/result.json"\n',
    )
    args_capture = tmp_path / "args"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "EVOLVE_RUN_DIR": str(tmp_path / "run"),
        "EVOLVE_ATTEMPT_ID": "proxy-precedence",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "NO_PROXY": "::1,fe80::/10",
        "no_proxy": "::1,fe80::/10",
    }

    result = subprocess.run(
        [str(evaluator / "eval.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    args = args_capture.read_text().splitlines()
    agent_environment = [args[index + 1] for index, value in enumerate(args) if value == "--ae"]
    verifier_environment = [args[index + 1] for index, value in enumerate(args) if value == "--ve"]
    assert not any(entry.startswith("EVOLVE_UV_BINARY=") for entry in agent_environment)
    assert not any(entry.startswith("EVOLVE_UV_BINARY=") for entry in verifier_environment)
    expected = "172.17.0.1,127.0.0.1,localhost,model.example"
    assert [value for value in agent_environment if value.startswith("NO_PROXY=")][-1] == f"NO_PROXY={expected}"
    assert [value for value in agent_environment if value.startswith("no_proxy=")][-1] == f"no_proxy={expected}"
    assert f"NO_PROXY={expected}" in verifier_environment
    assert f"no_proxy={expected}" in verifier_environment


def test_harbor_evaluator_forwards_dependency_proxies_with_model_bypass_and_skips_docker_cleanup(
    tmp_path: Path,
) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.8,
            agent="custom:Agent",
            environment="evolve.harbor_local:LocalEnvironment",
        )
    )
    (evaluator / "environment.kwargs").write_text('workdir="/workspace"\n')
    (evaluator / "verifier.env").write_text("JUDGE_MODEL=gpt-5.4-mini-2026-03-17\n")
    _write_evaluator_helpers(evaluator)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial"\n'
        'printf \'%s\\n\' \'{"task_name":"task","trial_name":"trial","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial/result.json"\n',
    )
    _write_executable(fake_bin / "docker", '#!/bin/sh\nprintf called > "$DOCKER_MARKER"\n')
    args_capture = tmp_path / "args"
    docker_marker = tmp_path / "docker-called"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "DOCKER_MARKER": str(docker_marker),
        "EVOLVE_RUN_DIR": str(tmp_path / "run"),
        "EVOLVE_ATTEMPT_ID": "local-attempt",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "OPENAI_BASE_URL": "https://model.example/v1",
        "http_proxy": "http://dependency-proxy.example:8118",
        "https_proxy": "http://dependency-proxy.example:8118",
        "no_proxy": ".internal.example",
        "NO_PROXY": ".upper.example",
    }

    result = subprocess.run([str(evaluator / "eval.sh")], cwd=tmp_path, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    args = args_capture.read_text().splitlines()
    assert args[args.index("--env") + 1] == "evolve.harbor_local:LocalEnvironment"
    assert args[args.index("--environment-kwarg") + 1] == 'workdir="/workspace"'
    agent_environment = [args[index + 1] for index, value in enumerate(args) if value == "--ae"]
    verifier_environment = [args[index + 1] for index, value in enumerate(args) if value == "--ve"]
    expected_proxy_environment = {
        "http_proxy=http://dependency-proxy.example:8118",
        "HTTP_PROXY=http://dependency-proxy.example:8118",
        "https_proxy=http://dependency-proxy.example:8118",
        "HTTPS_PROXY=http://dependency-proxy.example:8118",
        "no_proxy=.internal.example,.upper.example,model.example",
        "NO_PROXY=.internal.example,.upper.example,model.example",
    }
    assert expected_proxy_environment.issubset(agent_environment)
    assert expected_proxy_environment.issubset(verifier_environment)
    assert "JUDGE_MODEL=gpt-5.4-mini-2026-03-17" in verifier_environment
    assert "JUDGE_MODEL=gpt-5.4-mini-2026-03-17" not in agent_environment
    assert not docker_marker.exists()
    run_dir = tmp_path / "run"
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / "candidate-runtime.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "jobs").stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / "jobs" / "trial" / "result.json").stat().st_mode) == 0o600


def test_harbor_stage_limit_and_anchor_task_file_override(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    (evaluator / "tasks").mkdir(parents=True)
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "swebenchpro@1.0"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "swebenchpro@1.0",
            n_concurrent=16,
            tasks_per_round=4,
            trials=1,
            partial_floor=0.8,
            agent="mini-swe-agent",
            dataset_mode="registry",
            task_file="evaluator/tasks/train.txt",
        )
        + "EVOLVE_HARBOR_ANCHOR_TASK_FILE=evaluator/tasks/sealed.txt\n"
    )
    (evaluator / "tasks" / "train.txt").write_text("train-task\n")
    (evaluator / "tasks" / "sealed.txt").write_text("sealed-a\nsealed-b\n")
    (evaluator / "splits.json").write_text('{"resolved":false}\n')
    _write_evaluator_helpers(evaluator)
    evolve_dir = tmp_path / ".evolve"
    evolve_dir.mkdir()
    (evolve_dir / "launch_splits.py").write_text((scaffold_root() / "workspace" / "launch_splits.py").read_text())

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(fake_bin / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then\n'
        "    shift\n"
        "    jobs_dir=$1\n"
        "  fi\n"
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial-a" "$jobs_dir/trial-b"\n'
        'printf \'%s\\n\' \'{"task_name":"sealed-a","trial_name":"trial-a","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial-a/result.json"\n'
        'printf \'%s\\n\' \'{"task_name":"sealed-b","trial_name":"trial-b","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial-b/result.json"\n',
    )

    args_capture = tmp_path / "harbor-args.txt"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "EVOLVE_RUN_DIR": str(tmp_path / "run"),
        "EVOLVE_EVAL_KIND": "anchor",
        "EVOLVE_TASK_LIMIT": "2",
        "EVOLVE_ATTEMPT_ID": "anchor-attempt",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
    }
    result = subprocess.run([str(evaluator / "eval.sh")], cwd=tmp_path, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    args = args_capture.read_text().splitlines()
    assert args.count("--include-task-name") == 2
    assert "sealed-a" in args
    assert "sealed-b" in args
    assert "train-task" not in args
    assert args[args.index("--n-tasks") + 1] == "2"
    assert (tmp_path / "run" / "metrics.json").read_text().count('"expected_trials": 2') == 1


def test_harbor_run_plan_escapes_literal_task_name_patterns(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.8,
            agent="custom:Agent",
        )
    )
    (evaluator / "splits.json").write_text('{"resolved":false}\n')
    _write_evaluator_helpers(evaluator)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial"\n'
        'printf \'%s\\n\' \'{"task_name":"task[1]","trial_name":"trial","verifier_result":{"rewards":{"reward":1}}}\' > "$jobs_dir/trial/result.json"\n',
    )
    run_plan = tmp_path / "run-plan.json"
    run_plan.write_text(json.dumps({"tasks": ["task[1]"], "expected_trials": 1}))
    args_capture = tmp_path / "args"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "EVOLVE_RUN_DIR": str(tmp_path / "run"),
        "EVOLVE_RUN_PLAN": str(run_plan),
        "EVOLVE_ATTEMPT_ID": "literal-task-pattern",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "EVOLVE_CANDIDATE_RUNTIME_ENV_JSON": "{}",
        "EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": "[]",
    }

    result = subprocess.run([str(evaluator / "eval.sh")], cwd=tmp_path, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    args = args_capture.read_text().splitlines()
    included = [args[index + 1] for index, value in enumerate(args) if value == "--include-task-name"]
    assert included == ["task[[]1]"]


def test_resolved_split_task_limit_is_authoritative_for_zero_reward(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for name in ("task-a", "task-b", "task-c"):
        task = dataset / name
        task.mkdir()
        (task / "task.toml").write_text(f'version = "1.0"\nname = "{name}"\n')
    manifest = build_manifest(
        str(dataset),
        {"train": 1.0, "gate": 0.0, "sealed": 0.0, "seed": 0},
        base_dir=tmp_path,
        sampling="static",
        gate_limit=0,
    )
    expected_task = manifest["tasks"]["train"][0]
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", str(dataset)))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            str(dataset),
            n_concurrent=1,
            tasks_per_round=3,
            trials=1,
            partial_floor=0.8,
            agent="target.agent:HarborAgent",
        )
    )
    (evaluator / "splits.json").write_text(json.dumps(manifest))
    _write_evaluator_helpers(evaluator)
    evolve_dir = tmp_path / ".evolve"
    evolve_dir.mkdir()
    (evolve_dir / "launch_splits.py").write_text((scaffold_root() / "workspace" / "launch_splits.py").read_text())

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(fake_bin / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        'mkdir -p "$jobs_dir/trial"\n'
        f"printf '%s\\n' '{json.dumps({'task_name': expected_task, 'trial_name': 'trial', 'verifier_result': {'rewards': {'reward': 0.0}}})}' > \"$jobs_dir/trial/result.json\"\n",
    )

    run_dir = tmp_path / "run"
    args_capture = tmp_path / "args"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "EVOLVE_RUN_DIR": str(run_dir),
        "EVOLVE_WORKSPACE": str(tmp_path),
        "EVOLVE_ATTEMPT_ID": "limited-zero-reward",
        "EVOLVE_EVAL_SPLIT": "train",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "EVOLVE_TASK_LIMIT": "1",
    }

    result = subprocess.run([str(evaluator / "eval.sh")], cwd=tmp_path, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert (run_dir / "status").read_text().strip() == "complete"
    assert json.loads((run_dir / "task-split.json").read_text())["tasks"] == [expected_task]
    metrics = json.loads((run_dir / "metrics.json").read_text())["dimensions"]
    assert metrics == {
        "completed_trials": 1,
        "expected_trials": 1,
        "harbor_rc": 0,
        "missing_trials": 0,
        "pass_rate": 0.0,
    }


def test_harbor_smoke_is_install_only_and_exposes_raw_diagnostics(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=8,
            tasks_per_round=8,
            trials=2,
            partial_floor=0.8,
            agent="evolve.integrations.harbor.miniswe_candidate:MiniSweSourceAgent",
        )
    )
    _write_evaluator_helpers(evaluator)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    _write_executable(
        fake_bin / "harbor",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" > "$HARBOR_ARGS_CAPTURE"\n'
        "jobs_dir=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--jobs-dir" ]; then shift; jobs_dir=$1; fi\n'
        "  shift || true\n"
        "done\n"
        "printf '%s\\n' \"ModuleNotFoundError: No module named 'fastapi'\" >&2\n"
        "exit 7\n",
    )
    run_dir = tmp_path / "run"
    cache = tmp_path / "shared-cache"
    python_dir = tmp_path / "shared-python"
    runtime_mounts = [
        {
            "type": "bind",
            "source": str(cache),
            "target": "/opt/evolve/uv/cache",
            "read_only": False,
        },
        {
            "type": "bind",
            "source": str(python_dir),
            "target": "/opt/evolve/uv/python",
            "read_only": False,
        },
    ]
    runtime_env = {
        "UV_CACHE_DIR": "/opt/evolve/uv/cache",
        "UV_LINK_MODE": "copy",
        "UV_OFFLINE": "1",
        "UV_PYTHON_INSTALL_DIR": "/opt/evolve/uv/python",
    }
    args_capture = tmp_path / "args"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HARBOR_ARGS_CAPTURE": str(args_capture),
        "EVOLVE_RUN_DIR": str(run_dir),
        "EVOLVE_CANDIDATE_SMOKE_MODE": "full",
        "EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": json.dumps(runtime_mounts),
        "EVOLVE_CANDIDATE_RUNTIME_ENV_JSON": json.dumps(runtime_env),
        "EVOLVE_TASK_LIMIT": "8",
        "EVOLVE_ATTEMPT_ID": "smoke-attempt",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
    }

    result = subprocess.run([str(evaluator / "eval.sh")], cwd=tmp_path, env=env, text=True, capture_output=True)

    assert result.returncode == 7
    assert "ModuleNotFoundError: No module named 'fastapi'" in result.stderr
    args = args_capture.read_text().splitlines()
    assert "--install-only" in args
    assert "EVOLVE_CANDIDATE_SMOKE_MODE=full" in args
    assert f"EVOLVE_CANDIDATE_SOURCE={tmp_path / 'target'}" in args
    assert args[args.index("--n-tasks") + 1] == "8"
    assert args[args.index("--n-attempts") + 1] == "1"
    assert args[args.index("-n") + 1] == "8"
    mounts = json.loads(args[args.index("--mounts") + 1])
    assert mounts == runtime_mounts
    agent_environment = [args[index + 1] for index, value in enumerate(args) if value == "--ae"]
    verifier_environment = [args[index + 1] for index, value in enumerate(args) if value == "--ve"]
    for key, value in runtime_env.items():
        assert f"{key}={value}" in agent_environment
        if key == "UV_OFFLINE":
            assert f"{key}={value}" not in verifier_environment
        else:
            assert f"{key}={value}" in verifier_environment
    assert not (run_dir / "harbor-result.json").exists()
    assert not (run_dir / "score").exists()


def test_harbor_single_smoke_forces_one_task_attempt_and_worker() -> None:
    text = _eval_sh("harbor", "fixture")

    assert 'if [ "${EVOLVE_CANDIDATE_SMOKE_MODE:-}" = "single" ]; then' in text


def test_harbor_legacy_cache_mount_matches_adapter_default() -> None:
    text = _eval_sh("harbor", "fixture")

    assert '"target":"/opt/evolve/uv/cache"' in text
    assert '"target":"/installed-agent/uv-cache"' not in text


def test_harbor_rejects_malformed_candidate_runtime_before_launch(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_executable(evaluator / "eval.sh", _eval_sh("harbor", "fixture"))
    (evaluator / "eval.env").write_text(
        _eval_env(
            "experiment",
            "fixture",
            n_concurrent=1,
            tasks_per_round=1,
            trials=1,
            partial_floor=0.8,
            agent="mini-swe-agent",
        )
    )
    _write_evaluator_helpers(evaluator)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_uv(fake_bin)
    harbor_called = tmp_path / "harbor-called"
    _write_executable(fake_bin / "harbor", f"#!/bin/sh\ntouch {harbor_called}\n")
    run_dir = tmp_path / "run"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "EVOLVE_RUN_DIR": str(run_dir),
        "EVOLVE_ATTEMPT_ID": "bad-runtime",
        "EVOLVE_FRAMEWORK_PYTHON": sys.executable,
        "EVOLVE_CANDIDATE_RUNTIME_ENV_JSON": "[]",
        "EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": '{"not":"mounts"}',
    }

    result = subprocess.run(
        [str(evaluator / "eval.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 3
    assert (run_dir / "status").read_text().strip() == "infra_failed"
    assert not harbor_called.exists()


def test_harbor_retry_excludes_only_non_retryable_trial_failures() -> None:
    text = _eval_sh("harbor", "fixture")

    assert "--retry-exclude AgentTimeoutError" in text
    assert "--retry-exclude EvolveCandidateInvalidError" in text
    assert "--retry-exclude ApiUsageLimitError" in text
    assert "retry-exclude VerifierTimeoutError" not in text


def test_score_parser_accepts_complete_final_vector_after_nonzero_harbor_exit(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_evaluator_helpers(evaluator)
    (evaluator / "eval.env").write_text("EVOLVE_HARBOR_EXPECTED_TRIALS=2\nEVOLVE_HARBOR_ATTEMPTS=1\n")
    jobs = tmp_path / "jobs"
    job = jobs / "job"
    job.mkdir(parents=True)
    (job / "config.json").write_text(
        json.dumps({"retry": {"max_retries": 1, "exclude_exceptions": ["AgentTimeoutError"]}})
    )
    (job / "timeout").mkdir()
    (job / "timeout" / "result.json").write_text(
        json.dumps(
            {
                "task_name": "case-a",
                "trial_name": "one",
                "agent_result": {"cost_usd": 0},
                "exception_info": {"exception_type": "VerifierTimeoutError", "exception_message": "late"},
            }
        )
    )
    (job / "success").mkdir()
    (job / "success" / "result.json").write_text(
        json.dumps(
            {
                "task_name": "case-b",
                "trial_name": "one",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )
    run_dir = tmp_path / "run"

    result = subprocess.run(
        [sys.executable, str(evaluator / "parse_score.py"), str(jobs), str(run_dir), "7"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert (run_dir / "status").read_text().strip() == "complete"
    metrics = json.loads((run_dir / "metrics.json").read_text())["dimensions"]
    assert metrics["harbor_rc"] == 7
    assert metrics["completed_trials"] == 2


def test_score_parser_prefers_frozen_selection_over_explicit_task_limit(tmp_path: Path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    _write_evaluator_helpers(evaluator)
    (evaluator / "eval.env").write_text("EVOLVE_HARBOR_EXPECTED_TRIALS=30\nEVOLVE_HARBOR_ATTEMPTS=1\n")
    jobs = tmp_path / "jobs"
    trial = jobs / "job" / "trial"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "case-a",
                "trial_name": "trial",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "task-split.json").write_text(json.dumps({"tasks": [f"case-{index}" for index in range(30)]}))
    env = {**os.environ, "EVOLVE_HARBOR_EXPECTED_TRIALS": "1"}

    result = subprocess.run(
        [sys.executable, str(evaluator / "parse_score.py"), str(jobs), str(run_dir), "0"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 3, result.stderr
    assert (run_dir / "status").read_text().strip() == "infra_failed"
    metrics = json.loads((run_dir / "metrics.json").read_text())["dimensions"]
    assert metrics["expected_trials"] == 30
    assert metrics["completed_trials"] == 1
    assert metrics["missing_trials"] == 29


def test_harbor_shell_uses_canonical_parser_result() -> None:
    text = _eval_sh("harbor", "fixture")

    assert '[ "$harbor_rc" -eq 0 ] || exit 3' not in text
    assert 'exit "$parser_rc"' in text
    assert 'harbor "$@" 2>&1 | tee' not in text
    assert 'harbor "$@" > "$live_fifo" 2>&1 || harbor_rc=$?' in text
