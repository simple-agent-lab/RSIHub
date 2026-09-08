import importlib.util
import json
from pathlib import Path

HARBOR_EVALUATOR = Path(__file__).resolve().parents[1] / "scaffolds" / "evaluators" / "harbor"
spec = importlib.util.spec_from_file_location("harbor_artifacts", HARBOR_EVALUATOR / "harbor_artifacts.py")
assert spec is not None and spec.loader is not None
harbor_artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harbor_artifacts)
collect_harbor_artifacts = harbor_artifacts.collect_harbor_artifacts
write_harbor_artifacts = harbor_artifacts.write_harbor_artifacts


def write_trial(
    path: Path,
    *,
    task: str,
    trial: str,
    reward: float | None,
    exception_type: str | None = None,
    exception_message: str = "fixture failure",
    cost_usd: float = 0.0,
    agent_result: bool = True,
) -> None:
    path.mkdir(parents=True)
    payload = {
        "task_name": task,
        "trial_name": trial,
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
        "exception_info": (
            {"exception_type": exception_type, "exception_message": exception_message} if exception_type else None
        ),
        "agent_result": {"cost_usd": cost_usd} if agent_result else None,
    }
    (path / "result.json").write_text(json.dumps(payload))


def write_job_config(jobs: Path, *, max_retries: int, excluded: list[str] | None = None) -> None:
    job = jobs / "job"
    job.mkdir(parents=True, exist_ok=True)
    (job / "config.json").write_text(
        json.dumps({"retry": {"max_retries": max_retries, "exclude_exceptions": excluded or []}})
    )


def test_collect_harbor_artifacts_groups_trials_and_classifies_infra(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    write_trial(jobs / "case-a__one", task="case-a", trial="one", reward=1.0)
    write_trial(jobs / "case-a__two", task="case-a", trial="two", reward=0.0)
    write_trial(
        jobs / "case-b__one",
        task="case-b",
        trial="one",
        reward=None,
        exception_type="VerifierTimeoutError",
    )

    vector, artifacts, scoring_rewards = collect_harbor_artifacts(jobs)

    assert [trial["reward"] for trial in vector["tasks"]["case-a"]["trials"]] == [1.0, 0.0]
    assert vector["tasks"]["case-b"]["trials"][0]["status"] == "infrastructure_failed"
    assert scoring_rewards == [1.0, 0.0]
    assert artifacts["jobs_dir"] == str(jobs.resolve())
    assert "config" not in json.dumps(artifacts).lower()


def test_verifier_uv_tool_cache_miss_is_infrastructure_not_reward_zero(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial_dir = jobs / "case-a__one"
    write_trial(trial_dir, task="case-a", trial="one", reward=0.0)
    verifier = trial_dir / "verifier"
    verifier.mkdir()
    (verifier / "test-stdout.txt").write_text(
        "× No solution found when resolving tool dependencies:\n"
        "╰─▶ Because pytest was not found in the cache and you require pytest==8.4.1,\n"
        "    we can conclude that your requirements are unsatisfiable.\n"
        "hint: Packages were unavailable because the network was disabled.\n"
    )

    vector, _artifacts, scoring_rewards = collect_harbor_artifacts(jobs)

    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "infrastructure_failed"
    assert trial["reward"] is None
    assert trial["owner"] == "evaluator"
    assert trial["exception_type"] == "VerifierDependencyError"
    assert scoring_rewards == []


def test_verifier_uv_tool_download_failure_is_infrastructure_not_reward_zero(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial_dir = jobs / "case-a__one"
    write_trial(trial_dir, task="case-a", trial="one", reward=0.0)
    verifier = trial_dir / "verifier"
    verifier.mkdir()
    (verifier / "test-stdout.txt").write_text(
        "error: Failed to download: https://files.pythonhosted.org/packages/pytest.whl\n"
        "  Caused by: Request failed after 3 retries\n"
        "  Caused by: operation timed out\n"
    )

    vector, _artifacts, scoring_rewards = collect_harbor_artifacts(jobs)

    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "infrastructure_failed"
    assert trial["reward"] is None
    assert trial["owner"] == "evaluator"
    assert trial["exception_type"] == "VerifierDependencyError"
    assert scoring_rewards == []


def test_verifier_bootstrap_http_failure_is_infrastructure_not_reward_zero(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial_dir = jobs / "case-a__one"
    write_trial(trial_dir, task="case-a", trial="one", reward=0.0)
    verifier = trial_dir / "verifier"
    verifier.mkdir()
    (verifier / "test-stdout.txt").write_text("curl: (22) The requested URL returned error: 504\n")

    vector, _artifacts, scoring_rewards = collect_harbor_artifacts(jobs)

    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "infrastructure_failed"
    assert trial["reward"] is None
    assert trial["owner"] == "evaluator"
    assert trial["exception_type"] == "VerifierDependencyError"
    assert scoring_rewards == []


def test_positive_verifier_reward_wins_over_recovered_bootstrap_output(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial_dir = jobs / "case-a__one"
    write_trial(trial_dir, task="case-a", trial="one", reward=1.0)
    verifier = trial_dir / "verifier"
    verifier.mkdir()
    (verifier / "test-stdout.txt").write_text("curl: (22) The requested URL returned error: 504\nretry succeeded\n")

    vector, _artifacts, scoring_rewards = collect_harbor_artifacts(jobs)

    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "benchmark_complete"
    assert trial["reward"] == 1.0
    assert trial["owner"] == "benchmark"
    assert "exception_type" not in trial
    assert scoring_rewards == [1.0]


def test_collect_harbor_artifacts_scores_agent_timeouts_as_zero(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    write_trial(
        jobs / "case-a__one",
        task="case-a",
        trial="one",
        reward=None,
        exception_type="AgentTimeoutError",
    )

    vector, _artifacts, scoring_rewards = collect_harbor_artifacts(jobs)

    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "timeout"
    assert trial["reward"] == 0.0
    assert trial["owner"] == "benchmark_agent"
    assert scoring_rewards == [0.0]


def test_network_only_timeout_is_not_a_benchmark_zero(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    directory = jobs / "case-a__one"
    write_trial(directory, task="case-a", trial="one", reward=0.0, exception_type="AgentTimeoutError")
    agent = directory / "agent"
    agent.mkdir()
    trajectory = agent / "trajectory.json"
    trajectory.write_text(json.dumps({"steps": [{"source": "user", "message": "task"}]}))
    error = {
        "type": "error",
        "message": "Reconnecting... waiting for network (Connection failed: error sending request)",
    }
    (agent / "codex.txt").write_text((json.dumps(error) + "\n") * 3)
    vector, _, rewards = collect_harbor_artifacts(jobs)
    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "infrastructure_failed"
    assert trial["owner"] == "agent_runtime"
    assert trial["exception_type"] == "AgentTimeoutError"
    assert rewards == []

    # A recovered connection followed by real agent work keeps the normal timeout rule.
    trajectory.write_text(json.dumps({"steps": [{"source": "agent", "message": "working"}]}))
    vector, _, rewards = collect_harbor_artifacts(jobs)
    assert vector["tasks"]["case-a"]["trials"][0]["status"] == "timeout"
    assert rewards == [0.0]


def test_final_retried_verifier_timeout_scores_zero_and_preserves_sibling(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    write_job_config(jobs, max_retries=1, excluded=["AgentTimeoutError"])
    write_trial(
        jobs / "job" / "case-a__one",
        task="case-a",
        trial="one",
        reward=None,
        exception_type="VerifierTimeoutError",
    )
    write_trial(jobs / "job" / "case-b__one", task="case-b", trial="one", reward=1.0)

    vector, _artifacts, rewards = collect_harbor_artifacts(jobs)

    timeout = vector["tasks"]["case-a"]["trials"][0]
    assert timeout["status"] == "timeout"
    assert timeout["reward"] == 0.0
    assert timeout["owner"] == "benchmark_verifier"
    assert vector["tasks"]["case-b"]["trials"][0]["reward"] == 1.0
    assert rewards == [0.0, 1.0]


def test_final_retried_verifier_timeout_uses_root_job_config_with_trial_configs(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "config.json").write_text(
        json.dumps({"retry": {"max_retries": 1, "exclude_exceptions": ["AgentTimeoutError"]}})
    )
    for task in ("case-a", "case-b"):
        trial = jobs / f"{task}__one"
        write_trial(
            trial,
            task=task,
            trial="one",
            reward=None if task == "case-a" else 1.0,
            exception_type="VerifierTimeoutError" if task == "case-a" else None,
        )
        (trial / "config.json").write_text(json.dumps({"trial_name": f"{task}__one"}))

    vector, _artifacts, rewards = collect_harbor_artifacts(jobs)

    timeout = vector["tasks"]["case-a"]["trials"][0]
    assert timeout["status"] == "timeout"
    assert timeout["reward"] == 0.0
    assert timeout["owner"] == "benchmark_verifier"
    assert rewards == [0.0, 1.0]


def test_verifier_timeout_without_retry_or_agent_result_remains_infrastructure(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    write_job_config(jobs, max_retries=0)
    write_trial(
        jobs / "job" / "case-a__one",
        task="case-a",
        trial="one",
        reward=None,
        exception_type="VerifierTimeoutError",
    )
    write_trial(
        jobs / "job" / "case-b__one",
        task="case-b",
        trial="one",
        reward=None,
        exception_type="VerifierTimeoutError",
        agent_result=False,
    )

    vector, _artifacts, rewards = collect_harbor_artifacts(jobs)

    assert vector["tasks"]["case-a"]["trials"][0]["status"] == "infrastructure_failed"
    assert vector["tasks"]["case-b"]["trials"][0]["status"] == "infrastructure_failed"
    assert rewards == []


def test_exception_precedes_reward_and_failed_cost_is_preserved(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    write_trial(
        jobs / "case-a__one",
        task="case-a",
        trial="one",
        reward=0.0,
        exception_type="NonZeroAgentExitCodeError",
        exception_message="ModuleNotFoundError: No module named 'fastapi'",
        cost_usd=0.25,
    )
    run_dir = tmp_path / "run"

    rewards = write_harbor_artifacts(jobs, run_dir)

    trial = json.loads((run_dir / "task_vector.json").read_text())["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "infrastructure_failed"
    assert trial["reward"] is None
    assert trial["owner"] == "ambiguous"
    assert rewards == []
    assert json.loads((run_dir / "cost.json").read_text()) == {"usd": 0.25}


def test_explicit_candidate_marker_classifies_candidate_invalid(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    write_trial(
        jobs / "case-a__one",
        task="case-a",
        trial="one",
        reward=0.0,
        exception_type="NonZeroAgentExitCodeError",
        exception_message="EVOLVE_CANDIDATE_INVALID: missing declared dependency",
    )

    vector, _artifacts, rewards = collect_harbor_artifacts(jobs)

    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "candidate_invalid"
    assert trial["reward"] is None
    assert trial["owner"] == "candidate"
    assert rewards == []


def test_candidate_error_code_uses_only_explicit_marker() -> None:
    assert (
        harbor_artifacts.candidate_error_code(
            {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "EVOLVE_CANDIDATE_INVALID: model_path_import_failed",
            }
        )
        == "model_path_import_failed"
    )
    assert (
        harbor_artifacts.candidate_error_code(
            {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "ModuleNotFoundError: No module named 'fastapi'",
            }
        )
        is None
    )


def test_missing_tool_output_history_is_candidate_invalid(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    write_trial(
        jobs / "case-a__one",
        task="case-a",
        trial="one",
        reward=None,
        exception_type="NonZeroAgentExitCodeError",
        exception_message=(
            "litellm.BadRequestError: invalid request: No tool output found for function call call_123."
        ),
    )

    vector, _artifacts, rewards = collect_harbor_artifacts(jobs)

    trial = vector["tasks"]["case-a"]["trials"][0]
    assert trial["status"] == "candidate_invalid"
    assert trial["owner"] == "candidate"
    assert trial["reward"] is None
    assert rewards == []


def test_collect_harbor_artifacts_omits_traceback_text_from_exception_messages(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial = jobs / "case-a__one"
    write_trial(trial, task="case-a", trial="one", reward=None, exception_type="VerifierTimeoutError")
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text())
    result["exception_info"]["exception_message"] = "Traceback (most recent call last):\nsecret-bearing frame"
    result_path.write_text(json.dumps(result))

    vector, _artifacts, _scoring_rewards = collect_harbor_artifacts(jobs)

    assert "exception_message" not in vector["tasks"]["case-a"]["trials"][0]


def test_collect_harbor_artifacts_redacts_configured_proxy_literal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proxy = "http://private-user:private-password@proxy.example.invalid:8118"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    jobs = tmp_path / "jobs"
    write_trial(
        jobs / "case-a__one",
        task="case-a",
        trial="one",
        reward=None,
        exception_type="RuntimeError",
        exception_message=f"dependency download through {proxy} timed out",
    )

    vector, _artifacts, _scoring_rewards = collect_harbor_artifacts(jobs)

    message = vector["tasks"]["case-a"]["trials"][0]["exception_message"]
    assert proxy not in message
    assert message == "dependency download through [REDACTED] timed out"


def test_write_harbor_artifacts_indexes_only_retained_safe_files(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial = jobs / "case-a__one"
    write_trial(trial, task="case-a", trial="one", reward=1.0)
    (trial / "trial.log").write_text("retained trace\n")
    (trial / ".env").write_text("EVOLVE_FAKE_SECRET=secret\n")
    (trial / "config.json").write_text('{"proxy": "secret"}\n')
    verifier = trial / "verifier"
    verifier.mkdir()
    retained = {
        "evaluation.json": b'{"feedback":"make the figure larger"}\n',
        "judge.json": b'{"score":0.8}\n',
        "poster.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>\n',
        "poster.png": b"safe-png-fixture",
    }
    for name, payload in retained.items():
        (verifier / name).write_bytes(payload)
    run_dir = tmp_path / "run"

    assert write_harbor_artifacts(jobs, run_dir) == [1.0]

    artifacts = json.loads((run_dir / "evaluation_artifacts.json").read_text())
    indexed = artifacts["trials"][0]["files"]
    indexed_paths = {entry["path"] for entry in indexed}
    assert indexed_paths == {
        "case-a__one/evolve-replay.json",
        *(f"case-a__one/verifier/{name}" for name in retained),
    }
    assert "case-a__one/trial.log" not in indexed_paths
    assert "case-a__one/result.json" not in indexed_paths
    assert all("verifier/test-stdout.txt" not in path for path in indexed_paths)
    serialized = json.dumps(artifacts).lower()
    assert ".env" not in serialized
    assert "config" not in serialized


def test_write_harbor_artifacts_derives_safe_verifier_diagnostics(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial = jobs / "case-a__one"
    write_trial(trial, task="case-a", trial="one", reward=0.0)
    raw_trial_result = json.loads((trial / "result.json").read_text())
    raw_trial_result["verifier_result"]["private_expected_action"] = "ground-truth-secret"
    (trial / "result.json").write_text(json.dumps(raw_trial_result))
    verifier = trial / "verifier"
    verifier.mkdir()
    (verifier / "result.json").write_text(
        json.dumps(
            {
                "reward": 0.0,
                "reward_basis": ["DB"],
                "reward_info": {
                    "action_checks": [
                        {
                            "action": {
                                "name": "private_expected_action",
                                "arguments": {"customer_name": "ground-truth-secret"},
                            },
                            "action_match": False,
                            "action_reward": 0.0,
                            "tool_type": "write",
                        }
                    ],
                    "db_check": {"db_match": False, "db_reward": 0.0},
                    "reward": 0.0,
                    "reward_basis": ["DB"],
                    "reward_breakdown": {"DB": 0.0},
                },
                "runtime_initialization": {
                    "accepted": {"max_errors": 10, "max_steps": 200, "seed": None},
                    "accepted_once_count": 1,
                    "phase": "started",
                    "rejected_mutations": {"uninitialized_start": 1},
                },
                "status": "mismatch",
            }
        )
    )
    run_dir = tmp_path / "run"

    write_harbor_artifacts(jobs, run_dir)

    diagnostics_path = verifier / "diagnostics.json"
    diagnostics = json.loads(diagnostics_path.read_text())
    assert diagnostics["status"] == "mismatch"
    assert diagnostics["reward_basis"] == ["DB"]
    assert diagnostics["reward_info"]["action_checks"] == [
        {"action_match": False, "action_reward": 0.0, "tool_type": "write"}
    ]
    assert diagnostics["reward_info"]["db_check"] == {"db_match": False, "db_reward": 0.0}
    assert diagnostics["runtime_initialization"]["rejected_mutations"] == {"uninitialized_start": 1}
    serialized = json.dumps(diagnostics)
    assert "private_expected_action" not in serialized
    assert "ground-truth-secret" not in serialized

    artifacts = json.loads((run_dir / "evaluation_artifacts.json").read_text())
    indexed_paths = {entry["path"] for entry in artifacts["trials"][0]["files"]}
    replay_envelope = json.loads((trial / "evolve-replay.json").read_text())
    assert "case-a__one/verifier/diagnostics.json" in indexed_paths
    assert "case-a__one/verifier/result.json" not in indexed_paths
    assert "case-a__one/result.json" not in indexed_paths
    assert "ground-truth-secret" not in json.dumps(replay_envelope)
