"""Orchestrate one Harbor rollout from task selection through evidence summary."""

from __future__ import annotations

import json
import os
from pathlib import Path

from evolve.frozen.interfaces import OperatorContext, RolloutOperator, RolloutResult
from evolve.host_runtime import uv_run
from evolve.integrations.harbor._runtime_plan import resolve_mounts, write_compose_plan, write_mount_plan
from evolve.integrations.harbor._time_budget import summarize_time_budgets
from evolve.splits import load_manifest

from .evidence import (
    _OUTCOME_ORDER,
    _batch_failure_case,
    _load_eval_env,
    _with_missing_result_placeholders,
    _write_json,
    collect_cases,
    execution_error_summary,
    require_rollout_cases,
)
from .execution import (
    _append_agent_env,
    _batch_command,
    _completed_rollout,
    _configured_max_retries,
    _float_value,
    _jobs_root,
    _positive_int,
    _reset_directory,
    _run_harbor,
    _select_train_tasks,
)
from .state import build_harbor_state


class HarborRollout(RolloutOperator):
    def rollout(self, checkout: Path, ctx: OperatorContext) -> RolloutResult:
        if completed := _completed_rollout(ctx):
            return completed
        harbor, harbor_env = uv_run(ctx.workspace, "harbor")
        eval_env = _load_eval_env(checkout)
        dedicated_tasks = ctx.config.get("path") or os.environ.get("EVOLVE_HARBOR_ROLLOUT_TASKS")
        manifest_path = checkout / "evaluator" / "splits.json"
        try:
            manifest = load_manifest(manifest_path)
        except RuntimeError:
            manifest = {}
        if dedicated_tasks and manifest.get("resolved"):
            raise SystemExit(
                "operators.rollout.path bypasses the frozen dataset split; remove it and use evaluator.dataset"
            )
        tasks = str(dedicated_tasks or eval_env.get("EVOLVE_HARBOR_TASKS") or "")
        agent = str(ctx.config.get("agent") or eval_env.get("EVOLVE_HARBOR_AGENT") or "")
        if not tasks:
            raise SystemExit(
                "harbor rollout requires evaluator.dataset, operators.rollout.path, or EVOLVE_HARBOR_ROLLOUT_TASKS"
            )
        if not agent:
            raise SystemExit("harbor rollout requires an agent in config or evaluator/eval.env")

        budget_tasks = _positive_int(ctx.config.get("budget_tasks"), 8)
        split_task_names: list[str] = []
        if not dedicated_tasks:
            split_name = str(ctx.config.get("split") or "train")
            if split_name != "train":
                raise SystemExit("harbor rollout may only consume the train split")
            try:
                split_task_names = _select_train_tasks(
                    manifest_path,
                    tasks,
                    budget_tasks,
                    ctx.config.get("task_names"),
                    sampling=str(ctx.config.get("task_sampling") or "head"),
                    sampling_key=f"{ctx.config.get('seed', 0)}:{ctx.genid}",
                )
            except Exception as exc:
                raise SystemExit(str(exc)) from exc
            budget_tasks = len(split_task_names)
        default_concurrent = _positive_int(eval_env.get("EVOLVE_HARBOR_N_CONCURRENT"), budget_tasks)
        n_concurrent = min(budget_tasks, _positive_int(ctx.config.get("n_concurrent"), default_concurrent))
        setup_timeout_multiplier = _float_value(
            ctx.config.get("agent_setup_timeout_multiplier"),
            _float_value(eval_env.get("EVOLVE_HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER"), 1),
        )
        agent_timeout_multiplier = _float_value(
            ctx.config.get("agent_timeout_multiplier"),
            _float_value(eval_env.get("EVOLVE_HARBOR_AGENT_TIMEOUT_MULTIPLIER"), 1),
        )
        verifier_timeout_multiplier = _float_value(
            ctx.config.get("verifier_timeout_multiplier"),
            _float_value(eval_env.get("EVOLVE_HARBOR_VERIFIER_TIMEOUT_MULTIPLIER"), 1),
        )
        max_retries = _configured_max_retries(ctx.config, eval_env)
        field_limit = _positive_int(ctx.config.get("field_limit"), 2000)
        pass_threshold = _float_value(ctx.config.get("pass_threshold"), 1.0)
        jobs_root = _jobs_root(ctx)
        jobs_dir = jobs_root / f"gen-{ctx.genid}"
        _reset_directory(jobs_dir)

        base_command = [
            *harbor,
            "run",
            "-p",
            tasks,
            "--agent",
            agent,
            "--ae",
            f"EVOLVE_CANDIDATE_SOURCE={(checkout / 'target').resolve()}",
            "--n-attempts",
            "1",
            "--agent-setup-timeout-multiplier",
            str(max(setup_timeout_multiplier, 1)),
            "--agent-timeout-multiplier",
            str(max(agent_timeout_multiplier, 1)),
            "--verifier-timeout-multiplier",
            str(max(verifier_timeout_multiplier, 1)),
            "--max-retries",
            str(max_retries),
            "-y",
        ]
        environment = ctx.config.get("environment")
        if environment:
            base_command.extend(["--env", str(environment)])
        environment_kwargs = ctx.config.get("environment_kwargs")
        if isinstance(environment_kwargs, dict):
            for key in sorted(environment_kwargs):
                value = environment_kwargs[key]
                base_command.extend(["--environment-kwarg", f"{key}={json.dumps(value, separators=(',', ':'))}"])
        if os.environ.get("EVOLVE_LIVE_OUTPUT") != "1":
            base_command.append("-q")
        base_command.extend(["--ae", f"EVOLVE_CANDIDATE_SOURCE={(checkout / 'target').resolve()}"])
        configured_cache = eval_env.get("EVOLVE_UV_CACHE_DIR") or os.environ.get("EVOLVE_UV_CACHE_DIR")
        uv_cache = (
            Path(configured_cache).expanduser() if configured_cache else ctx.workspace / "runs" / "runtime" / "uv-cache"
        )
        mounts = resolve_mounts(os.environ, uv_cache)
        write_mount_plan(ctx.run_dir, mounts)
        for overlay in write_compose_plan(ctx.run_dir, os.environ):
            base_command.extend(["--extra-docker-compose", overlay])
        uv_python = os.environ.get("EVOLVE_UV_PYTHON_INSTALL_DIR")
        base_command.extend(
            [
                "--mounts",
                json.dumps(mounts),
            ]
        )
        _append_agent_env(base_command, checkout, ctx.config)
        base_command.extend(["--ae", "UV_CACHE_DIR=/opt/evolve/uv/cache"])
        if uv_python:
            base_command.extend(["--ae", "UV_PYTHON_INSTALL_DIR=/installed-agent/uv-python"])
        model = ctx.config.get("model") or eval_env.get("EVOLVE_HARBOR_MODEL") or os.environ.get("EVOLVE_HARBOR_MODEL")
        if not model and os.environ.get("OPENAI_MODEL"):
            model = f"openai/{os.environ['OPENAI_MODEL']}"
        if model:
            base_command.extend(["--model", str(model)])
        include_task = ctx.config.get("include_task_name")

        rollout_dir = ctx.run_dir / "rollout"
        initial_command = _batch_command(
            base_command,
            jobs_dir=jobs_dir,
            n_concurrent=n_concurrent,
            budget_tasks=budget_tasks,
            task_selectors=split_task_names,
            include_task=include_task,
        )
        stop_limits = {
            name: ctx.config[key]
            for name, key in (("errors", "stop_after_errors"), ("failures", "stop_after_failures"))
            if key in ctx.config
        }
        run_options = {"jobs_dir": jobs_dir, "stop_limits": stop_limits} if stop_limits else {}
        returncode = _run_harbor(initial_command, checkout, rollout_dir / "harbor.log", harbor_env, **run_options)
        tasks_dir = Path(tasks) if Path(tasks).is_dir() else None
        cases = _with_missing_result_placeholders(
            collect_cases(
                jobs_dir,
                field_limit=field_limit,
                pass_threshold=pass_threshold,
                tasks_dir=tasks_dir,
                workspace=ctx.workspace,
                trajectory_archive_dir=rollout_dir / "trajectories",
            ),
            split_task_names,
        )
        if not cases:
            cases = [_batch_failure_case(rollout_dir / "harbor.log", returncode, field_limit)]

        _write_json(rollout_dir / "cases.json", cases)
        _write_json(
            rollout_dir / "harbor-state.json",
            build_harbor_state(
                cases,
                jobs_dir=jobs_dir,
                tasks_dir=tasks_dir,
                workspace=ctx.workspace,
                generation=str(ctx.genid),
                role="dedicated" if dedicated_tasks else "train",
                harbor_returncode=returncode,
            ),
        )
        require_rollout_cases(cases, returncode=returncode, harbor_log=rollout_dir / "harbor.log")

        rewards = [case["reward"] for case in cases if isinstance(case.get("reward"), (int, float))]
        counts = {name: sum(case["outcome"] == name for case in cases) for name in _OUTCOME_ORDER}
        summary = {
            "variant": "harbor",
            "split": "dedicated" if dedicated_tasks else "train",
            "harbor_returncode": returncode,
            "tasks_requested": budget_tasks,
            "tasks_observed": len(cases),
            "passed": counts["passed"],
            "failed": counts["failed"],
            **execution_error_summary(cases),
            **summarize_time_budgets(cases),
            "mean_observed_reward": round(sum(rewards) / len(rewards), 6) if rewards else None,
            "jobs_dir": str(jobs_dir),
        }
        return RolloutResult(
            summary=summary,
            artifacts=[
                "rollout/harbor.log",
                "rollout/cases.json",
                "rollout/harbor-state.json",
                f"harbor-jobs:{jobs_dir}",
            ],
        )
