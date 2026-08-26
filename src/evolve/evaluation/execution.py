from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import evaluator_boolean, evaluator_repetitions, evaluator_sampling, experiment_id, load_config
from ..execution_runtime import (
    execution_runtime_config,
    prepare_execution_environment,
    resolve_execution_runtime,
)
from ..git import evaluator_tree, git, git_stdout
from ..host_runtime import clean_python_env
from ..preflight import PreflightFailureCategory, PreflightStatus, run_preflight
from ..runtime import OwnedResult, attempt_dir, next_attempt, owned_attempt_id, run_owned
from ..runtime.environment import resolve_evaluator_runtime_environment, write_harbor_environment_inputs
from ..runtime.uv import CandidateRuntimeResult, prepare_candidate_runtime
from .contract import (
    ContractMode,
    ContractResolutionContext,
    evaluation_contract_mode,
    evaluation_split_name,
    resolve_evaluation_contract,
    trusted_evaluator_config,
    verify_candidate_runtime_receipt,
    write_evaluation_contract,
)
from .diagnostics import contract_trials, freeze_diagnostics, materialize_setup_failure
from .evidence import read_cost, read_setup_evidence, read_task_vector, trial_results
from .legacy import TaskSetIdentity, effective_task_set_identity
from .results import EvaluationRecord, Outcome, classify_evaluation, write_attempt_summary
from .run_plan import EvaluationRunPlan


def _read_task_vector(run_dir: Path) -> dict[str, Any] | None:
    """Compatibility boundary for direct evaluator-evidence validation."""
    return read_task_vector(run_dir)


def _runtime_receipt_reference(workspace: Path, receipt: Path | None) -> dict[str, str] | None:
    """Compatibility name for the compact hashed receipt reference."""
    return _receipt_reference(workspace, receipt)


class EvaluationInterrupted(BaseException):
    """Carries a cancelled attempt to the driver for append-before-reraise."""


def evaluate(
    workspace: Path,
    tag: str,
    genid: str,
    *,
    purpose: str = "candidate",
    attempt: int | None = None,
    task_limit: int | None = None,
) -> EvaluationRecord:
    evaluator_sampling(workspace)
    start = time.monotonic()
    candidate_commit = git_stdout(workspace, "rev-parse", f"{tag}^{{commit}}")
    evaluator_fingerprint = evaluator_tree(workspace, tag)
    if evaluator_fingerprint != evaluator_tree(workspace, "gen/0"):
        raise RuntimeError(f"evaluator tree for {tag} differs from gen/0")
    evaluation_worktrees = workspace / "runs" / "evaluation-worktrees"
    evaluation_worktrees.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evolve-eval-", dir=evaluation_worktrees) as tempdir:
        checkout = Path(tempdir) / "checkout"
        git(workspace, "worktree", "add", "--detach", str(checkout), candidate_commit)
        cleanup_needed = True
        try:
            config = load_config(checkout / "evolve.yaml")
            execution_runtime = resolve_execution_runtime(execution_runtime_config(config["execution_runtime"]))
            contract = None
            mode = evaluation_contract_mode(workspace)
            evaluator = (
                trusted_evaluator_config(workspace)
                if mode is ContractMode.STRICT
                else load_config(checkout / "evolve.yaml")["evaluator"]
            )
            timeout_zero = evaluator_boolean(evaluator, "benchmark_timeout_is_zero")
            if mode is ContractMode.STRICT:
                contract = resolve_evaluation_contract(
                    ContractResolutionContext(
                        workspace=workspace,
                        candidate_commit=candidate_commit,
                        purpose=purpose,
                        generation=genid,
                        task_limit=task_limit,
                    )
                )
                task_set_hash = contract.task_set_digest
                task_members = contract.task_members
                expected = len(contract.trial_identities)
            else:
                task_set = effective_task_set_identity(
                    checkout,
                    evaluator,
                    purpose=purpose,
                    task_limit=task_limit,
                )
                task_set_hash = task_set.digest
                task_members = task_set.members
                expected = _expected_trials(
                    evaluator,
                    task_limit,
                    selected_tasks=len(task_set.members) if task_set.members else None,
                )
            runtime_fingerprint = hashlib.sha256((checkout / "evaluator" / "runtime.pin").read_bytes()).hexdigest()
            if attempt is None:
                attempt = next_attempt(
                    workspace,
                    purpose=purpose,
                    generation=genid,
                    candidate_commit=candidate_commit,
                )
            run_dir = attempt_dir(
                workspace,
                purpose=purpose,
                generation=genid,
                candidate_commit=candidate_commit,
                attempt=attempt,
            )
            run_dir.mkdir(parents=True)
            run_plan = EvaluationRunPlan(
                schema_version=1,
                experiment_id=experiment_id(workspace),
                generation=genid,
                candidate_commit=candidate_commit,
                purpose=purpose,
                canonical=purpose in {"candidate", "genesis", "anchor"},
                tasks=task_members,
                attempts_per_task=evaluator_repetitions(evaluator),
                expected_trials=expected,
                concurrency=max(1, int(evaluator.get("n_concurrent", 1))),
                evaluator_fingerprint=evaluator_fingerprint,
                task_set_hash=task_set_hash,
                runtime_fingerprint=runtime_fingerprint,
                execution_runtime_fingerprint=execution_runtime.receipt.fingerprint,
            )
            run_plan_path = run_plan.write(run_dir / "run-plan.json")
            base: dict[str, Any] = {
                "experiment_id": experiment_id(workspace),
                "generation": genid,
                "candidate_commit": candidate_commit,
                "purpose": purpose,
                "attempt": attempt,
                "evaluator_fingerprint": evaluator_fingerprint,
                "task_set_hash": task_set_hash,
                "runtime_fingerprint": runtime_fingerprint,
                "expected_trials": expected,
            }
            if contract is not None:
                contract_path = run_dir / "evaluation-contract.json"
                write_evaluation_contract(contract_path, contract)
                base["contract_id"] = contract.contract_id
                base["evaluation_contract"] = _evaluation_contract_reference(workspace, contract_path)
            try:
                try:
                    preflight = (
                        run_preflight(
                            workspace,
                            candidate_commit=candidate_commit,
                            candidate_checkout=checkout,
                            purpose=purpose,
                            task_limit=task_limit,
                            receipt_path=run_dir / "preflight.json",
                        )
                        if contract is not None
                        else None
                    )
                    if preflight is not None:
                        base["preflight_receipt"] = _receipt_reference(workspace, preflight.receipt_path)
                    if preflight is not None and preflight.status is PreflightStatus.FAILED:
                        assert contract is not None
                        candidate_lock_invalid = (
                            preflight.failure_category is PreflightFailureCategory.DEPENDENCY_LOCK_INVALID
                        )
                        setup_outcome = (
                            Outcome.CANDIDATE_INVALID if candidate_lock_invalid else Outcome.INFRASTRUCTURE_FAILED
                        )
                        record = classify_evaluation(
                            **base,
                            trials=materialize_setup_failure(
                                contract.trial_identities,
                                setup_outcome,
                                failure_category="dependency_lock_invalid",
                            ),
                            setup_outcome=setup_outcome,
                            setup_reason=preflight.failure_message or "ordinary preflight failed",
                            partial_floor=float(evaluator.get("partial_floor", 0.9)),
                            benchmark_timeout_is_zero=timeout_zero,
                            cost_usd=0.0,
                            wall_s=time.monotonic() - start,
                            artifacts=None,
                        )
                        record = freeze_diagnostics(record, contract)
                        write_attempt_summary(run_dir, record)
                        return record
                    runtime = prepare_candidate_runtime(
                        checkout,
                        run_dir,
                        workspace / "runs" / "runtime",
                        candidate_commit,
                        evaluator,
                        **({"contract_id": contract.contract_id} if contract is not None else {}),
                    )
                    base["candidate_runtime"] = _receipt_reference(workspace, runtime.receipt_path)
                    verification = None
                    if contract is not None:
                        receipt_payload = (
                            json.loads(runtime.receipt_path.read_text())
                            if runtime.receipt_path is not None and runtime.receipt_path.is_file()
                            else None
                        )
                        if receipt_payload is not None and not isinstance(receipt_payload, dict):
                            raise ValueError("candidate runtime receipt must be a JSON object")
                        verification = verify_candidate_runtime_receipt(contract, receipt_payload)
                        base["contract_certified"] = verification.certified
                    if verification is not None and not verification.certified:
                        record = classify_evaluation(
                            **base,
                            trials=contract_trials(contract, ()),
                            setup_outcome=Outcome.INFRASTRUCTURE_FAILED,
                            setup_reason=verification.reason,
                            partial_floor=float(evaluator.get("partial_floor", 0.9)),
                            benchmark_timeout_is_zero=timeout_zero,
                            cost_usd=0.0,
                            wall_s=time.monotonic() - start,
                            artifacts=None,
                        )
                    elif not runtime.ready:
                        candidate_runtime_invalid = (
                            contract is not None and runtime.outcome is Outcome.CANDIDATE_INVALID
                        )
                        record = classify_evaluation(
                            **base,
                            trials=(
                                materialize_setup_failure(
                                    contract.trial_identities,
                                    runtime.outcome,
                                    failure_category="candidate_runtime_invalid",
                                )
                                if candidate_runtime_invalid
                                else contract_trials(contract, ())
                            ),
                            setup_outcome=runtime.outcome,
                            setup_reason=runtime.reason,
                            partial_floor=float(evaluator.get("partial_floor", 0.9)),
                            benchmark_timeout_is_zero=timeout_zero,
                            cost_usd=0.0,
                            wall_s=time.monotonic() - start,
                            artifacts=None,
                        )
                    else:
                        result = _run_eval_script(
                            checkout,
                            run_dir,
                            genid,
                            task_limit,
                            purpose,
                            evaluation_split_name(evaluator, purpose),
                            runtime,
                            run_plan_path,
                            prepare_execution_environment(
                                execution_runtime,
                                clean_python_env(),
                                runtime_root=workspace / "runs" / "runtime" / "execution",
                            ),
                        )
                        setup_outcome, setup_reason = read_setup_evidence(run_dir)
                        if not _runtime_selection_matches(run_dir, task_members):
                            setup_outcome = Outcome.INFRASTRUCTURE_FAILED
                            setup_reason = "runtime task selection differs from the planned effective task set"
                        try:
                            vector = read_task_vector(run_dir)
                            trials = trial_results(vector) if vector is not None else ()
                        except (OSError, ValueError, json.JSONDecodeError) as error:
                            trials = ()
                            setup_outcome, setup_reason = Outcome.INFRASTRUCTURE_FAILED, str(error)
                        trials = contract_trials(contract, trials)
                        candidate_owned = setup_outcome is Outcome.CANDIDATE_INVALID or any(
                            trial.owner == "candidate"
                            and (
                                trial.outcome is Outcome.CANDIDATE_INVALID
                                or trial.exception_type
                                or trial.exception_message
                            )
                            for trial in trials
                        )
                        complete_trial_vector = len(trials) == int(base["expected_trials"])
                        if result.returncode not in {0, 2} and not candidate_owned and not complete_trial_vector:
                            setup_outcome = Outcome.INFRASTRUCTURE_FAILED
                            setup_reason = f"evaluator exited with code {result.returncode}"
                        record = classify_evaluation(
                            **base,
                            trials=trials,
                            setup_outcome=setup_outcome,
                            setup_reason=setup_reason,
                            partial_floor=float(evaluator.get("partial_floor", 0.9)),
                            benchmark_timeout_is_zero=timeout_zero,
                            cost_usd=read_cost(run_dir),
                            wall_s=time.monotonic() - start,
                            artifacts=_evaluation_artifact_reference(workspace, run_dir),
                        )
                finally:
                    cleanup_needed = False
                    git(workspace, "worktree", "remove", "--force", str(checkout), check=False)
            except Exception as error:
                record = EvaluationRecord(
                    **base,
                    outcome=Outcome.INFRASTRUCTURE_FAILED,
                    reason=str(error),
                    trials=contract_trials(contract, ()),
                    score=None,
                    cost_usd=0.0,
                    wall_s=time.monotonic() - start,
                )
                return freeze_diagnostics(record, contract)
            except BaseException as error:
                record = EvaluationRecord(
                    **base,
                    outcome=Outcome.CANCELLED,
                    reason=str(error) or "evaluation cancelled",
                    trials=contract_trials(contract, ()),
                    score=None,
                    cost_usd=0.0,
                    wall_s=time.monotonic() - start,
                )
                record = freeze_diagnostics(record, contract)
                raise EvaluationInterrupted(record, error) from error
            record = freeze_diagnostics(record, contract)
            write_attempt_summary(run_dir, record)
            return record
        finally:
            if cleanup_needed:
                git(workspace, "worktree", "remove", "--force", str(checkout), check=False)


def _evaluation_artifact_reference(workspace: Path, run_dir: Path) -> dict[str, str] | None:
    path = run_dir / "evaluation_artifacts.json"
    return (
        {"path": path.relative_to(workspace).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if path.exists()
        else None
    )


def _evaluation_contract_reference(workspace: Path, path: Path) -> dict[str, str]:
    return {
        "path": path.resolve().relative_to(workspace.resolve()).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _receipt_reference(workspace: Path, receipt: Path | None) -> dict[str, str] | None:
    if receipt is None or not receipt.exists():
        return None
    return {
        "path": receipt.resolve().relative_to(workspace.resolve()).as_posix(),
        "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
    }


def _runtime_selection_matches(
    run_dir: Path,
    planned_members: tuple[str, ...] | TaskSetIdentity,
) -> bool:
    if isinstance(planned_members, TaskSetIdentity):
        planned_members = planned_members.members
    if not planned_members:
        return True
    selection = run_dir / "task-split.json"
    plan = run_dir / "run-plan.json"
    # The run plan is the authoritative, already-limited task set.  A frozen
    # split receipt can legitimately contain the whole split when a smoke or
    # capability run applies ``task_limit`` afterwards.
    source = plan if plan.is_file() else selection
    if not source.is_file():
        return False
    try:
        payload = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or any(not isinstance(task, str) or not task for task in tasks):
        return False
    expected = set(planned_members)
    normalized = tuple(_normalize_runtime_task_id(task, expected) for task in tasks)
    return tuple(sorted(set(normalized))) == tuple(sorted(expected))


def _normalize_runtime_task_id(task_id: str, expected_task_ids: set[str]) -> str:
    """Map Harbor-qualified task names back to their frozen dataset identity.

    Harbor reports ``task.name`` from task.toml, while RSIHub freezes local
    dataset-directory names.  Official datasets commonly qualify the former as
    ``namespace/dataset__directory-name``.  Accept that representation only
    when its suffix resolves to exactly one frozen member; ambiguous or
    unrelated names remain mismatches.
    """
    if task_id in expected_task_ids:
        return task_id
    matches = [
        expected
        for expected in expected_task_ids
        if task_id.endswith(f"__{expected}") or task_id.endswith(f"/{expected}")
    ]
    return matches[0] if len(matches) == 1 else task_id


def _expected_trials(evaluator: dict[str, Any], task_limit: int | None, *, selected_tasks: int | None = None) -> int:
    attempts = evaluator_repetitions(evaluator)
    tasks = selected_tasks if selected_tasks is not None else int(evaluator.get("tasks_per_round", attempts))
    if task_limit is not None:
        tasks = min(tasks, task_limit) if selected_tasks is not None else task_limit
    return max(1, tasks) * attempts


def _run_eval_script(
    checkout: Path,
    run_dir: Path,
    genid: str,
    task_limit: int | None,
    purpose: str,
    evaluation_split: str,
    runtime: CandidateRuntimeResult,
    run_plan_path: Path | None = None,
    process_environment: dict[str, str] | None = None,
) -> OwnedResult:
    runs_dir = next(parent for parent in run_dir.parents if parent.name == "runs")
    source_environment = clean_python_env()
    config = load_config(checkout / "evolve.yaml")
    evaluator = config.get("evaluator")
    if not isinstance(evaluator, dict):
        raise ValueError("evaluator configuration must be a mapping")
    environment_plan = resolve_evaluator_runtime_environment(checkout, evaluator, source_environment)
    write_harbor_environment_inputs(run_dir, environment_plan)
    env: dict[str, str] = {
        **(process_environment or source_environment),
        **environment_plan.process_env(),
        # Harbor may change its process cwd while constructing trials.  Pin the
        # trusted detached checkout so candidate-local import paths such as
        # ``target.agent:HarborAgent`` remain resolvable without inheriting a
        # caller-controlled PYTHONPATH.
        "PYTHONPATH": str(checkout.resolve()),
        "EVOLVE_RUN_DIR": str(run_dir),
        "EVOLVE_GENID": genid,
        "EVOLVE_EVAL_KIND": purpose,
        "EVOLVE_ATTEMPT_ID": owned_attempt_id(runs_dir.parent, run_dir),
        "EVOLVE_WORKSPACE": str(runs_dir.parent.resolve()),
    }
    if run_plan_path is not None:
        env["EVOLVE_RUN_PLAN"] = str(run_plan_path.resolve())
    env["EVOLVE_EVAL_SPLIT"] = evaluation_split
    if runtime.variant is not None:
        env["EVOLVE_CANDIDATE_RUNTIME_ENV_JSON"] = runtime.environment_json()
        env["EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON"] = runtime.mounts_json()
    env.setdefault("EVOLVE_FRAMEWORK_PYTHON", sys.executable)
    configured_uv_cache = env.get("EVOLVE_UV_CACHE_DIR")
    uv_cache = Path(configured_uv_cache).expanduser() if configured_uv_cache else runs_dir / "runtime" / "uv-cache"
    if not uv_cache.is_absolute():
        uv_cache = runs_dir.parent / uv_cache
    uv_cache = uv_cache.resolve()
    uv_cache.mkdir(parents=True, exist_ok=True)
    env["EVOLVE_UV_CACHE_DIR"] = str(uv_cache)
    if task_limit is not None:
        env["EVOLVE_TASK_LIMIT"] = str(task_limit)
    result = run_owned([str(checkout / "evaluator" / "eval.sh")], cwd=checkout, env=env)
    (run_dir / "stdout.log").write_text(result.stdout)
    (run_dir / "stderr.log").write_text(result.stderr)
    return result
