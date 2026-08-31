import importlib
import json
import os
import random
import stat
from pathlib import Path

import pytest
from conftest import init_workspace

from evolve.feedback import write_feedback_bundle
from evolve.frozen.interfaces import OperatorContext
from evolve.trace_analysis import (
    ANALYZE_OPERATORS,
    _trajectory_only_cases,
    trajectory_signal_records,
    write_evidence_bundle,
)
from library._shared.harbor import evidence as harbor_evidence
from library._shared.harbor import execution as harbor_execution

ROOT = Path(__file__).resolve().parents[1]


def _harbor_rollout_module():
    return importlib.import_module("library._shared.harbor.rollout")


def _write_trial(
    jobs_dir: Path,
    *,
    name: str,
    reward: float | None,
    exception_type: str = "",
    exception_message: str = "",
) -> Path:
    trial = jobs_dir / name
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    payload = {
        "trial_name": name,
        "task_name": f"harbor/{name}",
        "agent_result": {
            "n_input_tokens": 100,
            "n_cache_tokens": 40,
            "n_output_tokens": 20,
            "cost_usd": 0.01,
        },
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
        "exception_info": (
            {"exception_type": exception_type, "exception_message": exception_message} if exception_type else None
        ),
        "agent_execution": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:02Z"},
    }
    (trial / "result.json").write_text(json.dumps(payload))
    trajectory = {
        "steps": [
            {"source": "user", "message": "<environment_context>noise</environment_context>"},
            {"source": "user", "message": f"Fix task {name}."},
            {
                "source": "agent",
                "message": "I will inspect the failure.",
                "tool_calls": [{"function_name": "exec", "arguments": {"command": "run tests"}}],
                "observation": {"results": [{"content": "tests failed: missing output"}]},
            },
        ]
    }
    (trial / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    (trial / "verifier" / "test-stdout.txt").write_text(
        "OPENAI_API_KEY=must-not-leak\nmissing required artifact output\n"
    )
    return trial


def test_harbor_rollout_distinguishes_task_agent_and_infra_failures(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_trial(jobs, name="task-failed", reward=0)
    _write_trial(jobs, name="task-partial", reward=0.5)
    _write_trial(jobs, name="task-passed", reward=1)
    _write_trial(
        jobs,
        name="verifier-timeout",
        reward=None,
        exception_type="VerifierTimeoutError",
        exception_message="Verifier execution timed out after 120 seconds",
    )
    _write_trial(
        jobs,
        name="agent-timeout",
        reward=None,
        exception_type="AgentTimeoutError",
        exception_message="Agent execution timed out",
    )
    _write_trial(
        jobs,
        name="runtime-infrastructure",
        reward=None,
        exception_type="EvolveRuntimeInfrastructureError",
        exception_message="external dependency sync failed",
    )

    cases = harbor_evidence.collect_cases(jobs)
    by_name = {case["trial_name"]: case for case in cases}

    assert by_name["task-failed"]["outcome"] == "failed"
    assert by_name["task-partial"]["outcome"] == "failed"
    assert by_name["task-passed"]["outcome"] == "passed"
    assert by_name["verifier-timeout"]["outcome"] == "infra_error"
    assert by_name["agent-timeout"]["outcome"] == "agent_error"
    assert by_name["runtime-infrastructure"]["outcome"] == "infra_error"
    assert by_name["task-failed"]["instruction"] == "Fix task task-failed."
    assert by_name["task-failed"]["tool_calls"][0]["name"] == "exec"
    assert "missing output" in by_name["task-failed"]["observations"][0]
    assert by_name["task-failed"]["events"][-1]["source"] == "agent"
    assert by_name["task-failed"]["artifact_inventory"]["agent"] == ["trajectory.json"]
    assert by_name["task-failed"]["execution"]["trajectory"]["format"] == "atif"
    assert by_name["task-failed"]["execution"]["trajectory"]["status"] == "available"
    assert "content" not in by_name["task-failed"]["execution"]["trajectory"]
    assert "must-not-leak" not in by_name["task-failed"]["verifier_output"]
    assert "[REDACTED]" in by_name["task-failed"]["verifier_output"]
    assert "json-secret" not in harbor_evidence._redact('{"OPENAI_API_KEY":"json-secret"}')


def test_harbor_rollout_promotes_artifact_rubric_evidence(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial = _write_trial(jobs, name="poster-task", reward=0.75)
    tasks = tmp_path / "tasks" / "poster-task"
    tasks.mkdir(parents=True)
    (tasks / "instruction.md").write_text("Create a research poster.\n")
    (trial / "agent" / "trajectory.json").unlink()
    (trial / "verifier" / "poster.svg").write_text('<svg viewBox="0 0 100 100"/>\n')
    (trial / "verifier" / "evaluation.json").write_text(
        json.dumps(
            {
                "score": 75,
                "raw_weighted_score": 80,
                "criteria": {"authored_visual_language": 2, "layout_balance_and_geometry": 4},
                "hard_failures": ["geometry_integrity: title exceeds viewBox"],
                "summary": "The palette is generic.",
                "improvement_feedback": "Use a restrained paper-specific palette.",
            }
        )
    )

    [case] = harbor_evidence.collect_cases(jobs, tasks_dir=tmp_path / "tasks")

    assert case["instruction"] == "Create a research poster."
    assert case["outputs"]["primary_artifact"] == "verifier/poster.svg"
    assert case["metrics"] == {"reward": 0.75, "score": 75.0, "raw_weighted_score": 80.0}
    assert {item["rubric_id"] for item in case["judgments"]} == {
        "authored_visual_language",
        "layout_balance_and_geometry",
        "geometry_integrity",
    }
    assert case["feedback"]["improvement"] == "Use a restrained paper-specific palette."
    assert case["execution"] == {
        "trajectory_available": False,
        "trajectory": {"format": "atif", "status": "missing"},
    }


def test_harbor_rollout_references_workspace_atif_without_copying_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    jobs = workspace / "runs" / "harbor-rollouts" / "gen-1"
    trial = _write_trial(jobs, name="task-a", reward=1)
    trajectory_path = trial / "agent" / "trajectory.json"
    archive = workspace / "runs" / "gen-1" / "rollout" / "trajectories"

    [case] = harbor_evidence.collect_cases(
        jobs,
        workspace=workspace,
        trajectory_archive_dir=archive,
    )

    reference = case["execution"]["trajectory"]
    assert case["execution"]["trajectory_available"] is True
    assert reference == {
        "format": "atif",
        "status": "available",
        "path": trajectory_path.relative_to(workspace).as_posix(),
        "sha256": harbor_evidence._file_sha256(trajectory_path),
        "steps": 3,
    }
    assert not archive.exists()


def test_harbor_rollout_archives_external_atif_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    jobs = tmp_path / "external-jobs"
    source = _write_trial(jobs, name="task-a", reward=1) / "agent" / "trajectory.json"
    archive = workspace / "runs" / "gen-1" / "rollout" / "trajectories"

    [case] = harbor_evidence.collect_cases(
        jobs,
        workspace=workspace,
        trajectory_archive_dir=archive,
    )
    [same_case] = harbor_evidence.collect_cases(
        jobs,
        workspace=workspace,
        trajectory_archive_dir=archive,
    )

    reference = case["execution"]["trajectory"]
    retained = workspace / reference["path"]
    assert retained.parent == archive
    assert retained.read_bytes() == source.read_bytes()
    assert reference["sha256"] == harbor_evidence._file_sha256(source)
    assert same_case["execution"]["trajectory"] == reference
    assert len(list(archive.glob("*.json"))) == 1


def test_harbor_rollout_redacts_configured_proxy_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = "http://private-user:private-password@proxy.example.invalid:8118"
    monkeypatch.setenv("HTTPS_PROXY", proxy)

    redacted = harbor_evidence._redact(f"dependency download through {proxy} timed out")

    assert proxy not in redacted
    assert redacted == "dependency download through [REDACTED] timed out"


def test_harbor_rollout_child_creates_private_files(tmp_path: Path) -> None:
    output = tmp_path / "child-output"
    log = tmp_path / "harbor.log"

    returncode = harbor_execution._run_harbor(
        ["/bin/sh", "-c", 'printf private > "$OUTPUT_PATH"'],
        tmp_path,
        log,
        {**os.environ, "OUTPUT_PATH": str(output)},
    )

    assert returncode == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_harbor_rollout_accepts_infrastructure_failures_as_trace_evidence(tmp_path: Path) -> None:
    harbor_log = tmp_path / "harbor.log"

    harbor_evidence.require_rollout_cases(
        [{"outcome": "infra_error"}, {"outcome": "incomplete"}],
        returncode=1,
        harbor_log=harbor_log,
    )


def test_harbor_rollout_defaults_jobs_to_workspace_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EVOLVE_ROLLOUT_JOBS_DIR", raising=False)
    ctx = OperatorContext(
        workspace=tmp_path,
        checkout=tmp_path,
        run_dir=tmp_path / "runs" / "gen-1",
        genid="1",
        parent="0",
        round=None,
        fan_out=1,
        config={},
        rng=random.Random(0),
    )

    assert harbor_execution._jobs_root(ctx) == tmp_path / "runs" / "harbor-rollouts"


def test_harbor_rollout_explicit_zero_retries_overrides_environment() -> None:

    assert (
        harbor_execution._configured_max_retries(
            {"max_retries": 0},
            {"EVOLVE_HARBOR_MAX_RETRIES": "9"},
        )
        == 0
    )
    assert harbor_execution._configured_max_retries({}, {"EVOLVE_HARBOR_MAX_RETRIES": "2"}) == 2


def test_harbor_rollout_reuses_only_a_complete_explicitly_enabled_stage(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "gen-1"
    rollout = run_dir / "rollout"
    rollout.mkdir(parents=True)
    summary = {
        "variant": "harbor",
        "tasks_observed": 1,
        "infra_tasks": ["task-a"],
    }
    artifacts = ["rollout/harbor.log", "rollout/cases.json"]
    (rollout / "summary.json").write_text(json.dumps(summary))
    (rollout / "artifacts.json").write_text(json.dumps(artifacts))
    (rollout / "cases.json").write_text(json.dumps([{"task_name": "task-a"}]))
    ctx = OperatorContext(
        workspace=tmp_path,
        checkout=tmp_path,
        run_dir=run_dir,
        genid="1",
        parent="0",
        round=None,
        fan_out=1,
        config={"reuse_completed": True},
        rng=random.Random(0),
    )

    reused = harbor_execution._completed_rollout(ctx)

    assert reused is not None
    assert reused.summary == summary
    assert reused.artifacts == artifacts
    ctx.config["reuse_completed"] = False
    assert harbor_execution._completed_rollout(ctx) is None


def test_harbor_rollout_preserves_missing_selected_results_as_trace_cases() -> None:
    cases = harbor_evidence._with_missing_result_placeholders(
        [
            {
                "task_name": "terminal-bench/task-a",
                "reward": 1.0,
                "outcome": "passed",
            }
        ],
        ["task-a", "task-b"],
    )

    assert len(cases) == 2
    missing = cases[1]
    assert missing["task_name"] == "task-b"
    assert missing["outcome"] == "incomplete"
    assert missing["exception"]["type"] == "MissingRolloutResult"
    assert "source_attempt" not in missing


def test_harbor_rollout_canonicalizes_harbor_dataset_prefix_without_a_false_missing_case() -> None:
    observed = "sierra-research/tau3-bench__tau3-banking_knowledge-task-083"
    canonical = "tau3-banking_knowledge-task-083"

    cases = harbor_evidence._with_missing_result_placeholders(
        [{"task_name": observed, "reward": 0.0, "outcome": "failed"}],
        [canonical],
    )

    assert cases == [
        {
            "task_name": canonical,
            "observed_task_name": observed,
            "reward": 0.0,
            "outcome": "failed",
        }
    ]


def test_harbor_rollout_task_canonicalization_prefers_exact_and_most_specific_ids() -> None:
    selected = ["task-001", "dataset__task-001"]

    assert harbor_evidence._canonical_task_name("dataset__task-001", selected) == "dataset__task-001"
    assert harbor_evidence._canonical_task_name("registry/dataset__task-001", selected) == "dataset__task-001"
    assert harbor_evidence._canonical_task_name("registry__dataset__task-001", selected) == "dataset__task-001"
    assert harbor_evidence._canonical_task_name("registry__unknown", selected) is None


def test_harbor_rollout_preserves_batch_failure_when_no_task_result_exists(tmp_path: Path) -> None:
    harbor_log = tmp_path / "harbor.log"
    harbor_log.write_text("docker network unavailable\n")

    case = harbor_evidence._batch_failure_case(harbor_log, 1, 2000)

    assert case["outcome"] == "infra_error"
    assert case["exception"]["type"] == "HarborBatchError"
    assert "docker network unavailable" in case["raw_agent_output"]


def test_harbor_rollout_reads_codex_session_jsonl_when_trajectory_is_absent(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial = _write_trial(jobs, name="codex-session", reward=0)
    (trial / "agent" / "trajectory.json").unlink()
    session = trial / "agent" / "sessions" / "2026" / "session.jsonl"
    session.parent.mkdir(parents=True)
    rows = [
        {"timestamp": "t1", "type": "event_msg", "payload": {"type": "user_message", "message": "Fix it."}},
        {"timestamp": "t2", "type": "event_msg", "payload": {"type": "agent_message", "message": "Inspecting."}},
        {
            "timestamp": "t3",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"pytest"}',
                "call_id": "c1",
            },
        },
        {
            "timestamp": "t4",
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "1 failed", "call_id": "c1"},
        },
        {"timestamp": "t5", "type": "event_msg", "payload": {"type": "agent_message", "message": "Done."}},
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))

    case = harbor_evidence.collect_cases(jobs)[0]

    assert case["instruction"] == "Fix it."
    assert case["agent_messages"] == ["Inspecting.", "Done."]
    assert case["tool_calls"] == [{"name": "exec_command", "arguments": '{"cmd":"pytest"}'}]
    assert case["observations"] == ["1 failed"]
    assert [event["type"] for event in case["events"]] == [
        "message",
        "message",
        "tool_call",
        "tool_result",
        "message",
    ]


def test_harbor_rollout_bounds_codex_session_events_to_the_latest_trace_window(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial = _write_trial(jobs, name="long-codex-session", reward=0)
    (trial / "agent" / "trajectory.json").unlink()
    session = trial / "agent" / "sessions" / "session.jsonl"
    session.parent.mkdir(parents=True)
    rows = [
        {
            "timestamp": f"t{index}",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": f"message-{index}"},
        }
        for index in range(100)
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))

    case = harbor_evidence.collect_cases(jobs)[0]

    assert len(case["events"]) == 32
    assert case["events"][0]["message"] == "message-68"
    assert case["events"][-1]["message"] == "message-99"
    assert len(case["trajectory_events"]) == 100
    assert case["trajectory_events"][0]["message"] == "message-0"


def test_harbor_rollout_bounds_trajectory_events_to_the_latest_trace_window(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    trial = _write_trial(jobs, name="long-trajectory", reward=0)
    trajectory = {"steps": [{"source": "agent", "message": f"message-{index}"} for index in range(100)]}
    (trial / "agent" / "trajectory.json").write_text(json.dumps(trajectory))

    case = harbor_evidence.collect_cases(jobs)[0]

    assert len(case["events"]) == 32
    assert case["events"][0]["message"] == "message-68"
    assert case["events"][-1]["message"] == "message-99"
    assert len(case["trajectory_events"]) == 100
    assert case["trajectory_events"][0]["message"] == "message-0"


def test_feedback_bundle_exposes_current_rollout_to_mutator(tmp_path: Path) -> None:
    workspace, _ = init_workspace(tmp_path)
    run_dir = workspace / "runs" / "gen-1"
    (run_dir / "analyze").mkdir(parents=True)
    (run_dir / "analyze" / "feedback.md").write_text("# Trace Analysis Feedback\n\nfailed task evidence\n")

    manifest = write_feedback_bundle(workspace=workspace, run_dir=run_dir)

    copied = run_dir / "feedback" / "failures" / "analyze.md"
    assert copied.read_text().endswith("failed task evidence\n")
    assert "[current trace analysis](failures/analyze.md)" in (run_dir / "feedback" / "index.md").read_text()
    assert "feedback/failures/analyze.md" in manifest


def test_analyze_operators_share_raw_harbor_facts(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _write_trial(jobs, name="missing-output-a", reward=0)
    _write_trial(jobs, name="missing-output-b", reward=0)
    _write_trial(jobs, name="passing", reward=1)
    cases = harbor_evidence.collect_cases(jobs)

    for operator in ANALYZE_OPERATORS:
        run_dir = tmp_path / operator
        selected, artifacts = write_evidence_bundle(
            run_dir,
            cases,
            operator=operator,
            max_chars=100_000,
        )
        manifest = json.loads((run_dir / "analyze" / "evidence" / "manifest.json").read_text())
        assert manifest["analyze_operator"] == operator
        assert "selected_variant" not in manifest
        if operator == "trajectory_only":
            assert "Agent Behavior Analysis" in selected
            assert "analyze/evidence/raw_traces.jsonl" not in artifacts
            assert "analyze/evidence/trajectory_only.json" in artifacts
        else:
            assert f"Analyze Operator: {operator}" in selected
            assert "analyze/evidence/raw_traces.jsonl" in artifacts
            assert set(manifest["operators"]) == set(ANALYZE_OPERATORS) - {"trajectory_only"}
        assert "variants" not in manifest

    patterns = json.loads(
        (tmp_path / "failure_patterns" / "analyze" / "evidence" / "failure_patterns.json").read_text()
    )
    assert patterns[0]["support"] == 2
    assert patterns[0]["signature"]["terminal_cause"] == "missing_artifact"
    passing = json.loads(
        (tmp_path / "failure_patterns" / "analyze" / "evidence" / "passing_behaviors.json").read_text()
    )
    assert passing[0]["task_name"] == "harbor/passing"


def test_trajectory_only_matches_aevolve_behavior_only_evidence(tmp_path: Path) -> None:
    cases = [
        {
            "task_name": "terminal/task-a",
            "outcome": "failed",
            "reward": 0,
            "verifier_output": "secret ground-truth failure",
            "instruction": "Do a benchmark-specific thing",
            "events": [
                {
                    "source": "agent",
                    "message": "",
                    "tool_calls": [
                        {
                            "name": "bash",
                            "arguments": json.dumps({"command": "pytest -q"}),
                        }
                    ],
                    "observations": ["ERROR: one test failed"],
                },
                {
                    "source": "agent",
                    "message": "finished",
                    "tool_calls": [
                        {
                            "name": "bash",
                            "arguments": json.dumps({"command": "git diff"}),
                        }
                    ],
                    "observations": ["diff output"],
                },
                {"source": "agent", "message": "Implemented and verified the requested change."},
            ],
        }
    ]

    selected, artifacts = write_evidence_bundle(
        tmp_path,
        cases,
        operator="trajectory_only",
        max_chars=100_000,
        judge_verdicts=[
            {
                "score": 2,
                "category": "software-engineering",
                "outcome": "Tests still failed.",
                "failure_reason": "The agent stopped after the first failing test run.",
            }
        ],
    )

    evidence = tmp_path / "analyze" / "evidence"
    records = json.loads((evidence / "trajectory_only.json").read_text())
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert records[0]["task_id"] == "terminal/task-a"
    assert records[0]["signals"]["n_turns"] == 3
    assert records[0]["signals"]["n_tool_calls"] == 2
    assert records[0]["signals"]["n_errors"] == 1
    assert records[0]["signals"]["submitted"] is False
    assert records[0]["signals"]["completed"] is True
    assert records[0]["signals"]["completion_signal"] == "final_response"
    assert records[0]["signals"]["final_response"] == "Implemented and verified the requested change."
    assert records[0]["judge_verdict"]["score"] == 2
    assert records[0]["judge_verdict"]["failure_reason"].startswith("The agent stopped")
    assert "[start] bash(pytest -q)" in records[0]["compressed_trajectory"]
    assert "ERROR: one test failed" in records[0]["compressed_trajectory"]
    assert "Completion: final_response" in records[0]["compressed_trajectory"]
    assert "[final response] Implemented and verified the requested change." in records[0]["compressed_trajectory"]
    assert "Submitted: False" not in records[0]["compressed_trajectory"]
    assert "secret ground-truth failure" not in selected
    assert "Do a benchmark-specific thing" not in selected
    assert "reward" not in selected
    assert manifest["ground_truth_exposed"] is False
    assert not (evidence / "raw_traces.jsonl").exists()
    assert artifacts == [
        "analyze/evidence/manifest.json",
        "analyze/evidence/trajectory_only.json",
        "analyze/evidence/selected.md",
    ]


def test_trajectory_only_follows_recent_parent_lineage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    run_dir = workspace / "runs" / "gen-2"
    prior = workspace / "runs" / "gen-1" / "rollout"
    prior.mkdir(parents=True)
    prior_cases = [{"task_name": "prior-a"}, {"task_name": "prior-b"}]
    (prior / "cases.json").write_text(json.dumps(prior_cases))
    workspace.mkdir(exist_ok=True)
    (workspace / "archive.jsonl").write_text(
        json.dumps({"genid": "0", "parent": None}) + "\n" + json.dumps({"genid": "1", "parent": "0"}) + "\n"
    )
    ctx = OperatorContext(
        workspace=workspace,
        checkout=workspace,
        run_dir=run_dir,
        genid="2",
        parent="1",
        round=None,
        fan_out=1,
        config={"history_cycles": 2, "max_observations": 3},
        rng=random.Random(0),
    )

    combined = _trajectory_only_cases(ctx, [{"task_name": "current-a"}, {"task_name": "current-b"}])

    assert [case["task_name"] for case in combined] == ["prior-b", "current-a", "current-b"]


def test_trajectory_only_prefers_explicit_submit_over_final_response() -> None:
    records = trajectory_signal_records(
        [
            {
                "task_name": "task-a",
                "events": [
                    {"source": "agent", "type": "message", "message": "done"},
                    {"type": "tool_call", "name": "submit", "arguments": {"answer": "42"}},
                ],
            }
        ]
    )

    assert records[0]["signals"]["completion_signal"] == "explicit_submit"
    assert records[0]["signals"]["submitted"] is True
    assert "Completion: explicit_submit" in records[0]["compressed_trajectory"]
    assert "[submitted] 42" in records[0]["compressed_trajectory"]


def test_trajectory_only_does_not_treat_pre_tool_commentary_as_final_response() -> None:
    records = trajectory_signal_records(
        [
            {
                "task_name": "task-a",
                "events": [
                    {
                        "source": "agent",
                        "message": "I will run one last check.",
                        "tool_calls": [{"name": "bash", "arguments": {"command": "pytest -q"}}],
                        "observations": ["1 passed"],
                    }
                ],
            }
        ]
    )

    assert records[0]["signals"]["completion_signal"] == "none"
    assert records[0]["signals"]["completed"] is False


def test_feedback_history_uses_analyze_operator_key(tmp_path: Path) -> None:
    workspace, _ = init_workspace(tmp_path)
    historical = workspace / "runs" / "gen-0" / "analyze" / "evidence"
    historical.mkdir(parents=True)
    (historical / "metrics.json").write_text(json.dumps({"trials": 2}))
    (historical / "manifest.json").write_text(json.dumps({"analyze_operator": "failure_patterns"}))
    run_dir = workspace / "runs" / "gen-1"
    evidence = run_dir / "analyze" / "evidence"
    evidence.mkdir(parents=True)
    (run_dir / "analyze" / "feedback.md").write_text("duplicate selected view\n")
    (evidence / "selected.md").write_text("# selected profile evidence\n")
    (evidence / "manifest.json").write_text(json.dumps({"analyze_operator": "failure_patterns"}))
    (evidence / "metrics.json").write_text(json.dumps({"trials": 1}))

    manifest = write_feedback_bundle(workspace=workspace, run_dir=run_dir)

    assert (run_dir / "feedback" / "evidence" / "selected.md").read_text().startswith("# selected")
    assert "feedback/evidence/history.json" in manifest
    history = json.loads((run_dir / "feedback" / "evidence" / "history.json").read_text())
    assert history[0]["raw_evidence_dir"] == "runs/gen-0/analyze/evidence"
    assert history[0]["analyze_operator"] == "failure_patterns"
    assert "analyze_variant" not in history[0]
    index = (run_dir / "feedback" / "index.md").read_text()
    assert "[selected trace evidence](evidence/selected.md)" in index
    assert "[current trace analysis]" not in index
