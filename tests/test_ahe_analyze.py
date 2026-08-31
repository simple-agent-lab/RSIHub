import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

from evolve.agent import AgentCommandError, AgentRunResult
from evolve.composition.catalog import resolve_operator, validate_operator_config
from evolve.config import load_config
from evolve.frozen.interfaces import OperatorContext

ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "library" / "analyze" / "ahe.py"
    spec = importlib.util.spec_from_file_location("ahe_analyze_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ctx(tmp_path: Path, *, genid: str = "1", parent: str = "0") -> OperatorContext:
    workspace = tmp_path / "workspace"
    checkout = workspace / "checkout"
    run_dir = workspace / "runs" / f"gen-{genid}"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "evolve.yaml").write_text(
        "operators:\n"
        "  mutate:\n"
        "    operator: ahe\n"
        "    timeout_s: 3600\n"
        "    config:\n"
        "      runner: harbor\n"
        "      agent: evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent\n"
        "      model: gpt-test\n"
        "      environment: docker\n"
        "      editable_roots: [target]\n"
    )
    return OperatorContext(
        workspace=workspace,
        checkout=checkout,
        run_dir=run_dir,
        genid=genid,
        parent=parent,
        round=None,
        fan_out=1,
        config={
            "field_limit": 120,
            "max_tasks": 90,
            "max_concurrent": 2,
            "timeout_per_task": 30,
            "debugger_max_retries": 2,
        },
        rng=random.Random(0),
    )


def _case(name: str, outcome: str, reward: float | None, *, task: str | None = None) -> dict:
    return {
        "trial_name": name,
        "task_name": task or name,
        "outcome": outcome,
        "reward": reward,
        "instruction": f"Fix {name}",
        "agent_messages": [f"inspect {name}", f"finish {name}"],
        "tool_calls": [{"name": "exec", "arguments": f"pytest {name}"}],
        "observations": [f"result for {name}"],
        "events": [{"index": 0, "type": "message", "message": f"inspect {name}"}],
        "verifier_output": f"verifier says {outcome}",
        "verifier_rewards": {"reward": reward},
        "exception": {},
        "usage": {"input_tokens": 10, "cost_usd": 0.01},
        "timing_s": {"agent_execution": 1.5},
    }


def _write_cases(run_dir: Path, cases: list[dict]) -> None:
    rollout = run_dir / "rollout"
    rollout.mkdir(parents=True, exist_ok=True)
    (rollout / "cases.json").write_text(json.dumps(cases))


def _archive_row(
    genid: str,
    score: float,
    tasks: dict[str, list[str]],
    *,
    eligible: bool = True,
) -> dict:
    return {
        "genid": genid,
        "score": score,
        "selection_eligible": eligible,
        "task_vector": {
            "schema_version": 1,
            "tasks": {
                task: {
                    "trials": [
                        {
                            "status": "benchmark_complete",
                            "reward": 1.0 if status == "passed" else 0.0,
                            "owner": "benchmark",
                        }
                        for status in statuses
                    ]
                }
                for task, statuses in tasks.items()
            },
        },
    }


def _write_archive(ctx: OperatorContext, rows: list[dict]) -> None:
    (ctx.workspace / "archive.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def _fake_debugger(checkout, prompt, ctx, *, output_dir, job_name, timeout_s, input_files=None):
    del checkout, ctx, output_dir, job_name, timeout_s, input_files
    response = "ROOT CAUSE: retry policy" if "ROOT CAUSE:" in prompt else "KEY STRATEGY: inspect first"
    return AgentRunResult(response, "", response, 0, 0.1, {"usd": 0.25})


def test_ahe_groups_all_rollouts_per_task_and_prioritizes_failures() -> None:
    module = _module()
    cases = [
        _case("pass-a-1", "passed", 1.0, task="task-a"),
        _case("pass-a-2", "passed", 1.0, task="task-a"),
        _case("fail-b-1", "failed", 0.0, task="task-b"),
        _case("pass-b-2", "passed", 1.0, task="task-b"),
    ]

    jobs = module._build_jobs(cases, max_tasks=90)

    assert [job.task_name for job in jobs] == ["task-b", "task-a"]
    assert [case["trial_name"] for case in jobs[0].cases] == ["fail-b-1", "pass-b-2"]
    assert jobs[0].mode == "debug"
    assert jobs[1].mode == "summary"
    assert "PASS vs FAIL" in module._debugger_prompt(jobs[0])
    assert "REUSABLE PATTERN" in module._debugger_prompt(jobs[1])
    assert [job.task_name for job in module._build_jobs(cases, max_tasks=1)] == ["task-b"]


def test_ahe_excludes_infrastructure_only_tasks_and_applies_pass_threshold() -> None:
    module = _module()
    jobs = module._build_jobs(
        [
            _case("infra", "infra_error", None, task="task-infra"),
            _case("missing", "incomplete", None, task="task-missing"),
            _case("partial", "passed", 0.5, task="task-partial"),
        ],
        max_tasks=90,
        pass_threshold=1.0,
    )

    assert [job.task_name for job in jobs] == ["task-partial"]
    assert jobs[0].n_fail == 1
    assert jobs[0].n_pass == 0
    assert jobs[0].cases[0]["outcome"] == "failed"


def test_ahe_debugger_reuses_only_allowlisted_mutate_config(tmp_path: Path) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    ctx.config["debugger_agent_kwargs"] = {
        "reasoning_effort": "high",
        "max_tokens": 64000,
    }
    config = module._debugger_runner_config(ctx.checkout, ctx.config)

    assert config == {
        "runner": "harbor",
        "agent": "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent",
        "model": "gpt-test",
        "environment": "docker",
        "agent_kwargs": {"reasoning_effort": "high", "max_tokens": 64000},
        "max_retries": 0,
    }
    assert "editable_roots" not in config


@pytest.mark.parametrize(
    ("recipe", "expected_agent", "expected_model"),
    [
        (
            "ahe",
            "codex",
            "gpt-5.4",
        ),
        ("ahe_codex", "codex", "gpt-5.4"),
    ],
)
def test_ahe_production_recipes_reach_debugger_runner_with_nested_mutate_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recipe: str,
    expected_agent: str,
    expected_model: str,
) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    recipe_path = ROOT / "recipes" / recipe / "evolve.yaml"
    production = load_config(recipe_path)
    (ctx.checkout / "evolve.yaml").write_bytes(recipe_path.read_bytes())
    analyze = production["operators"]["analyze"]
    assert isinstance(analyze, dict)
    ctx.config.clear()
    ctx.config.update(analyze["config"])
    observed: list[dict[str, object]] = []

    def capture_runner(checkout, prompt, runner_ctx, **kwargs):
        observed.append(dict(runner_ctx.config))
        return _fake_debugger(checkout, prompt, runner_ctx, **kwargs)

    monkeypatch.setattr(module, "run_readonly_agent", capture_runner)
    job = module._build_jobs([_case("task-a", "failed", 0)], 90)[0]

    module._run_debugger_job(ctx.checkout, ctx, job)

    assert len(observed) == 1
    assert observed[0]["runner"] == "harbor"
    assert observed[0]["agent"] == expected_agent
    assert observed[0]["model"] == expected_model


def test_ahe_debugger_can_be_configured_without_mutate(tmp_path: Path) -> None:
    module = _module()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "evolve.yaml").write_text("operators: {}\n")
    analyzer_config = {
        "debugger": {
            "runner": "harbor",
            "agent": "debug-agent",
            "model": "debug-model",
            "environment": "docker",
            "agent_kwargs": {"reasoning_effort": "medium"},
            "max_retries": 2,
        }
    }

    config = module._debugger_runner_config(checkout, analyzer_config)

    assert config == {
        "runner": "harbor",
        "agent": "debug-agent",
        "model": "debug-model",
        "environment": "docker",
        "agent_kwargs": {"reasoning_effort": "medium"},
        "max_retries": 0,
    }


def test_ahe_normalized_config_preserves_nested_debugger_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    normalized = validate_operator_config(
        resolve_operator("analyze", "ahe"),
        {
            "debugger": {
                "runner": "harbor",
                "agent": "debug-agent",
                "model": "debug-model",
                "timeout_s": 17,
            }
        },
    )
    ctx.config.clear()
    ctx.config.update(normalized)
    observed_timeouts: list[float] = []

    def capture_timeout(*args, **kwargs):
        observed_timeouts.append(kwargs["timeout_s"])
        return _fake_debugger(*args, **kwargs)

    monkeypatch.setattr(module, "run_readonly_agent", capture_timeout)
    job = module._build_jobs([_case("task-a", "failed", 0)], 90)[0]

    module._run_debugger_job(ctx.checkout, ctx, job)

    assert observed_timeouts == [17.0]


def test_ahe_miniswe_debugger_prompt_includes_submission_protocol() -> None:
    module = _module()
    job = module._build_jobs([_case("task-a", "failed", 0)], 90)[0]

    prompt = module._debugger_runner_prompt(job, {"agent": "mini-swe-agent"})

    assert "/logs/artifacts/ahe-debugger-response.md" in prompt
    assert "Every response must include a Bash tool call" in prompt
    assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in prompt
    assert "first write the complete requested report as reasoning text" not in prompt
    for agent in (
        "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent",
        "evolve.integrations.harbor.miniswe_task_file:FileTaskMiniSweAgent",
    ):
        assert "/logs/artifacts/ahe-debugger-response.md" in module._debugger_runner_prompt(job, {"agent": agent})
    assert module._debugger_runner_prompt(job, {"agent": "codex"}) == module._debugger_prompt(job)
    assert module._debugger_runner_prompt(job, {"agent": "custom:FileTaskMiniSweAgent"}) == module._debugger_prompt(job)


def test_ahe_debugger_keeps_trace_evidence_out_of_prompt() -> None:
    module = _module()
    case = _case("task-a", "failed", 0)
    case["instruction"] = "TRACE BODY MUST STAY ON DISK"
    job = module._build_jobs([case], 90)[0]

    prompt = module._debugger_prompt(job)
    evidence = module._debugger_evidence(job)

    assert "/app/task/inputs/trace-evidence.json" in prompt
    assert "TRACE BODY MUST STAY ON DISK" not in prompt
    assert "TRACE BODY MUST STAY ON DISK" in evidence


def test_ahe_debugger_mounts_one_trace_evidence_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    job = module._build_jobs([_case("task-a", "failed", 0)], 90)[0]
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return _fake_debugger(*args, **kwargs)

    monkeypatch.setattr(module, "run_readonly_agent", capture)

    module._run_debugger_job(ctx.checkout, ctx, job)

    assert list(captured["input_files"]) == ["trace-evidence.json"]
    assert '"task_name": "task-a"' in captured["input_files"]["trace-evidence.json"]


def test_ahe_debugger_retries_and_fails_visibly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    job = module._build_jobs([_case("task-a", "failed", 0)], 90)[0]
    attempts = 0

    def flaky(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise AgentCommandError("temporary", returncode=1)
        return _fake_debugger(*args, **kwargs)

    monkeypatch.setattr(module, "run_readonly_agent", flaky)
    assert module._run_debugger_job(ctx.checkout, ctx, job).response.startswith("ROOT CAUSE")
    assert attempts == 3

    monkeypatch.setattr(
        module,
        "run_readonly_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AgentCommandError("failed", returncode=1)),
    )
    with pytest.raises(AgentCommandError, match="failed"):
        module._run_debugger_job(ctx.checkout, ctx, job)


def test_ahe_debugger_zero_retries_means_one_total_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    ctx.config["debugger_max_retries"] = 0
    ctx.config.pop("retry_attempts", None)
    job = module._build_jobs([_case("task-a", "failed", 0)], 90)[0]
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AgentCommandError("failed", returncode=1)

    monkeypatch.setattr(module, "run_readonly_agent", fail_once)

    with pytest.raises(AgentCommandError, match="failed"):
        module._run_debugger_job(ctx.checkout, ctx, job)

    assert calls == 1


def test_ahe_debugger_safe_wrapper_records_single_attempt_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    ctx.config["debugger_max_retries"] = 0
    job = module._build_jobs([_case("task-a", "failed", 0)], 90)[0]
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AgentCommandError("debugger unavailable", returncode=1)

    monkeypatch.setattr(module, "run_readonly_agent", fail_once)
    result = module._run_debugger_job_safe(ctx.checkout, ctx, job)

    assert calls == 1
    assert result.error == "debugger unavailable"
    assert result.response.startswith("ANALYSIS UNAVAILABLE:")


def test_ahe_debugger_stage_keeps_all_tasks_when_individual_jobs_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    jobs = module._build_jobs(
        [
            _case("fail-a", "failed", 0, task="task-a"),
            _case("fail-b", "failed", 0, task="task-b"),
            _case("pass-c", "passed", 1, task="task-c"),
        ],
        90,
    )

    def run_one(_checkout, _ctx, job):
        if job.task_name in {"task-a", "task-c"}:
            raise AgentCommandError(
                f"debugger failed for {job.task_name}",
                usage={"usd": 0.1, "wall_s": 2},
            )
        return module.DebuggerResult(job, "ROOT CAUSE: task-b diagnosis", {"usd": 0.2})

    monkeypatch.setattr(module, "_run_debugger_job", run_one)

    results = module._run_debugger_jobs(ctx.checkout, ctx, jobs)

    assert [result.job.task_name for result in results] == ["task-a", "task-b", "task-c"]
    assert results[0].error == "debugger failed for task-a"
    assert results[1].error is None
    assert results[2].error == "debugger failed for task-c"
    assert results[0].response.startswith("ANALYSIS UNAVAILABLE:")
    assert results[2].usage["usd"] == 0.1


def test_ahe_analyzer_writes_official_reports_and_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    _write_cases(
        ctx.run_dir,
        [_case("fail-1", "failed", 0, task="task-a"), _case("pass-1", "passed", 1, task="task-b")],
    )
    monkeypatch.setattr(module, "run_readonly_agent", _fake_debugger)

    result = module.AheAnalyze().analyze(ctx.checkout, ctx)

    analysis = ctx.run_dir / "analyze" / "analysis"
    detail = (analysis / "detail" / "task-a.md").read_text()
    selected = (ctx.run_dir / "analyze" / "evidence" / "selected.md").read_text()
    cases = (ctx.run_dir / "analyze" / "evidence" / "cases.jsonl").read_text()
    assert "ROOT CAUSE" in detail
    assert "task-a" in (analysis / "overview.md").read_text()
    assert "ROOT CAUSE" in selected
    assert "runs/gen-1/analyze/analysis/detail/task-a.md" in selected
    assert "## Bounded cases" not in selected
    assert "## Bounded cases" in detail
    assert '"trial_name": "fail-1"' in detail
    assert '"trial_name": "fail-1"' in cases
    change = json.loads((analysis / "change_evaluation.json").read_text())
    assert change["status"] == "baseline"
    assert result.summary["tasks"] == 2
    assert result.summary["debugger_usd"] == 0.5
    assert result.summary["debugger_errors"] == 0
    assert "analyze/analysis/detail/task-a.md" in result.artifacts


def test_ahe_analyzer_preserves_infra_evidence_without_debugging_or_causal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="2", parent="1")
    prior = ctx.workspace / "runs" / "gen-1"
    _write_cases(prior, [_case("prior", "failed", 0, task="task-a")])
    _write_cases(ctx.run_dir, [_case("current", "infra_error", None, task="task-a")])
    monkeypatch.setattr(
        module,
        "run_readonly_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("infra evidence must not invoke debugger")),
    )

    result = module.AheAnalyze().analyze(ctx.checkout, ctx)

    assert result.summary["observed"] == 1
    assert result.summary["selected"] == 0
    assert result.summary["tasks"] == 0
    overview = json.loads((ctx.run_dir / "analyze/evidence/overview.json").read_text())
    assert overview["outcomes"] == {"infra_error": 1}
    assert (ctx.run_dir / "analyze/evidence/cases.jsonl").read_text() == ""
    change = json.loads((ctx.run_dir / "analyze/analysis/change_evaluation.json").read_text())
    assert change["transitions"] == {"task-a": "unknown"}
    assert change["unattributed_regressions"] == []


@pytest.mark.parametrize(
    ("predicted", "fixed", "realized", "expected"),
    [
        (["a"], ["a"], [], "EFFECTIVE"),
        (["a", "b"], ["a"], [], "PARTIALLY_EFFECTIVE"),
        (["a"], ["a"], ["risk"], "MIXED"),
        (["a"], [], [], "INEFFECTIVE"),
        (["a"], [], ["risk"], "HARMFUL"),
    ],
)
def test_ahe_change_verdict_matches_upstream(predicted, fixed, realized, expected) -> None:
    module = _module()

    assert module._change_verdict(predicted, fixed, realized) == expected


def test_ahe_archive_analysis_reports_best_ever_and_stability(tmp_path: Path) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="3", parent="2")
    _write_archive(
        ctx,
        [
            _archive_row("0", 0.20, {"always-pass": ["passed", "passed"], "flip": ["failed", "failed"]}),
            _archive_row("1", 0.30, {"always-pass": ["passed", "passed"], "flip": ["passed", "passed"]}),
            _archive_row("2", 0.25, {"always-pass": ["passed", "passed"], "flip": ["failed", "failed"]}),
            _archive_row("ignored", 0.99, {"always-pass": ["failed", "failed"]}, eligible=False),
        ],
    )

    analysis = module._archive_analysis(ctx)

    assert analysis["best_ever"] == {"genid": "1", "score": 0.3}
    assert analysis["stability"]["stable_pass"] == ["always-pass"]
    assert analysis["stability"]["unstable"] == ["flip"]
    assert analysis["stability"]["possibly_unstable"] == []


def test_ahe_archive_analysis_marks_two_observation_flip_as_possibly_unstable(tmp_path: Path) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="2", parent="1")
    _write_archive(
        ctx,
        [
            _archive_row("0", 0.20, {"flip": ["failed", "failed"]}),
            _archive_row("1", 0.25, {"flip": ["passed", "passed"]}),
        ],
    )

    analysis = module._archive_analysis(ctx)

    assert analysis["stability"]["possibly_unstable"] == ["flip"]
    assert analysis["stability"]["unstable"] == []


def test_ahe_archive_analysis_classifies_canonical_benchmark_complete_rewards(tmp_path: Path) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="2", parent="1")
    rows = []
    for genid, passing_reward in (("0", 1.0), ("1", 0.0)):
        rows.append(
            {
                "genid": genid,
                "score": passing_reward,
                "selection_eligible": True,
                "task_vector": {
                    "schema_version": 1,
                    "tasks": {
                        "flip": {
                            "trials": [
                                {
                                    "status": "benchmark_complete",
                                    "reward": passing_reward,
                                    "owner": "benchmark",
                                }
                            ]
                        },
                        "always-fail": {
                            "trials": [{"status": "benchmark_complete", "reward": 0.0, "owner": "benchmark"}]
                        },
                        "infra": {
                            "trials": [{"status": "infrastructure_failed", "reward": None, "owner": "infrastructure"}]
                        },
                    },
                },
            }
        )
    _write_archive(ctx, rows)

    analysis = module._archive_analysis(ctx)

    assert analysis["stability"]["possibly_unstable"] == ["flip"]
    assert analysis["stability"]["stable_fail"] == ["always-fail"]
    assert analysis["stability"]["infra_only"] == ["infra"]


def test_ahe_archive_analysis_uses_configured_pass_threshold(tmp_path: Path) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="1", parent="0")
    ctx.config["pass_threshold"] = 0.5
    _write_archive(
        ctx,
        [
            {
                "genid": "0",
                "score": 0.5,
                "selection_eligible": True,
                "task_vector": {
                    "schema_version": 1,
                    "tasks": {
                        "partial": {
                            "trials": [
                                {
                                    "status": "benchmark_complete",
                                    "reward": 0.5,
                                    "owner": "benchmark",
                                }
                            ]
                        }
                    },
                },
            }
        ],
    )

    analysis = module._archive_analysis(ctx)

    assert analysis["stability"]["stable_pass"] == ["partial"]


def test_ahe_archive_analysis_does_not_score_mixed_infrastructure_trials(tmp_path: Path) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="1", parent="0")
    _write_archive(
        ctx,
        [
            {
                "genid": "0",
                "score": 0,
                "selection_eligible": True,
                "task_vector": {
                    "schema_version": 1,
                    "tasks": {
                        "mixed": {
                            "trials": [
                                {"status": "benchmark_complete", "reward": 1.0, "owner": "benchmark"},
                                {"status": "infrastructure_failed", "reward": 0.0, "owner": "infrastructure"},
                            ]
                        }
                    },
                },
            }
        ],
    )

    analysis = module._archive_analysis(ctx)

    assert analysis["stability"]["infra_only"] == ["mixed"]


def test_ahe_analyzer_renders_archive_analysis_in_overview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="2", parent="1")
    _write_archive(
        ctx,
        [
            _archive_row("0", 0.20, {"stable": ["failed", "failed"]}),
            _archive_row("1", 0.25, {"stable": ["failed", "failed"]}),
        ],
    )
    _write_cases(ctx.run_dir, [_case("current", "failed", 0, task="stable")])
    prior = ctx.workspace / "runs" / "gen-1"
    _write_cases(prior, [_case("prior", "failed", 0, task="stable")])
    manifest = prior / "mutate" / "change_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"changes": [{"id": "chg-1", "predicted_fixes": [], "risk_tasks": []}]}))
    monkeypatch.setattr(module, "run_readonly_agent", _fake_debugger)

    module.AheAnalyze().analyze(ctx.checkout, ctx)

    overview = (ctx.run_dir / "analyze/analysis/overview.md").read_text()
    assert "## Best Ever" in overview
    assert "generation 1" in overview
    assert "## Task Stability" in overview
    assert "stable fail (1): stable" in overview


def test_ahe_analyzer_attributes_prior_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="2", parent="1")
    prior = ctx.workspace / "runs" / "gen-1"
    _write_cases(
        prior,
        [_case("old-a", "failed", 0, task="task-a"), _case("old-b", "passed", 1, task="task-b")],
    )
    manifest_dir = prior / "mutate"
    manifest_dir.mkdir()
    (manifest_dir / "change_manifest.json").write_text(
        json.dumps(
            {
                "changes": [
                    {
                        "id": "chg-1",
                        "description": "retry failed commands",
                        "files": ["target/environment.py"],
                        "predicted_fixes": ["task-a"],
                        "risk_tasks": ["task-b"],
                    },
                ]
            }
        )
    )
    _write_cases(
        ctx.run_dir,
        [_case("new-a", "passed", 1, task="task-a"), _case("new-b", "failed", 0, task="task-b")],
    )
    monkeypatch.setattr(module, "run_readonly_agent", _fake_debugger)

    module.AheAnalyze().analyze(ctx.checkout, ctx)

    change = json.loads((ctx.run_dir / "analyze" / "analysis" / "change_evaluation.json").read_text())
    assert change["transitions"] == {"task-a": "fail_to_pass", "task-b": "pass_to_fail"}
    assert change["prediction_results"]["task-a"] == "confirmed"
    assert change["risk_results"]["task-b"] == "realized"
    assert change["change_evaluations"] == [
        {
            "actually_fixed": ["task-a"],
            "change_id": "chg-1",
            "description": "retry failed commands",
            "files": ["target/environment.py"],
            "predicted_fixes": ["task-a"],
            "predicted_risks": ["task-b"],
            "risk_realized": ["task-b"],
            "still_failed": [],
            "verdict": "MIXED",
        }
    ]
    assert change["unattributed_regressions"] == []


def test_ahe_analyzer_resolves_unique_short_manifest_task_names_to_canonical_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="2", parent="1")
    fixed_task = "sierra-research/tau3-bench__tau3-banking_knowledge-task-001"
    regressed_task = "sierra-research/tau3-bench__tau3-banking_knowledge-task-002"
    prior = ctx.workspace / "runs" / "gen-1"
    _write_cases(
        prior,
        [
            _case("old-a", "failed", 0, task=fixed_task),
            _case("old-b", "passed", 1, task=regressed_task),
        ],
    )
    manifest_dir = prior / "mutate"
    manifest_dir.mkdir()
    (manifest_dir / "change_manifest.json").write_text(
        json.dumps(
            {
                "changes": [
                    {
                        "id": "chg-1",
                        "description": "improve runtime handling",
                        "files": ["target/environment.py"],
                        "predicted_fixes": ["tau3-banking_knowledge-task-001"],
                        "risk_tasks": ["tau3-banking_knowledge-task-002"],
                    }
                ]
            }
        )
    )
    _write_cases(
        ctx.run_dir,
        [
            _case("new-a", "passed", 1, task=fixed_task),
            _case("new-b", "failed", 0, task=regressed_task),
        ],
    )
    monkeypatch.setattr(module, "run_readonly_agent", _fake_debugger)

    module.AheAnalyze().analyze(ctx.checkout, ctx)

    change = json.loads((ctx.run_dir / "analyze/analysis/change_evaluation.json").read_text())
    assert change["prediction_results"] == {fixed_task: "confirmed"}
    assert change["risk_results"] == {regressed_task: "realized"}
    assert change["change_evaluations"][0]["predicted_fixes"] == [fixed_task]
    assert change["change_evaluations"][0]["actually_fixed"] == [fixed_task]
    assert change["change_evaluations"][0]["predicted_risks"] == [regressed_task]
    assert change["change_evaluations"][0]["risk_realized"] == [regressed_task]
    assert change["unattributed_regressions"] == []


def test_ahe_analyzer_computes_transitions_without_prior_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    ctx = _ctx(tmp_path, genid="2", parent="1")
    prior = ctx.workspace / "runs/gen-1"
    _write_cases(prior, [_case("old-a", "failed", 0, task="task-a")])
    _write_cases(ctx.run_dir, [_case("new-a", "passed", 1, task="task-a")])
    monkeypatch.setattr(module, "run_readonly_agent", _fake_debugger)

    module.AheAnalyze().analyze(ctx.checkout, ctx)

    change = json.loads((ctx.run_dir / "analyze/analysis/change_evaluation.json").read_text())
    assert change["transitions"] == {"task-a": "fail_to_pass"}
    assert change["manifest"] is None
    assert change["prediction_results"] == {}
    assert change["risk_results"] == {}


def test_ahe_bounds_and_redacts_case_fields() -> None:
    module = _module()
    secret = "OPENAI_API_KEY=must-not-leak " + "x" * 200
    normalized = module._normalize(
        _case("secret", "failed", 0)
        | {
            "instruction": secret,
            "agent_messages": [secret] * 100,
            "usage": {"password": "bare-secret-value"},
        },
        40,
    )
    rendered = json.dumps(normalized)
    assert "must-not-leak" not in rendered
    assert "bare-secret-value" not in rendered
    assert "[REDACTED]" in rendered
    assert module.TRUNCATION_KEY in rendered


def test_ahe_missing_cases_fails_instead_of_falling_back(tmp_path: Path) -> None:
    module = _module()
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="missing rollout cases"):
        module.AheAnalyze().analyze(ctx.checkout, ctx)
