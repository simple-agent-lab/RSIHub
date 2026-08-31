import hashlib
import json
import stat
from pathlib import Path

import pytest
from conftest import git, init_workspace, rows_by_genid

import evolve.evaluation.execution as evaluator_module
from evolve.archive import MECHANISM_EVAL_FIELD, append_event
from evolve.driver import RunOptions
from evolve.driver import run as driver_run
from evolve.evaluation import Outcome
from evolve.evaluation.evidence import TaskVectorError
from evolve.evaluation.execution import (
    _evaluation_artifact_reference,
    _read_task_vector,
    _run_eval_script,
    _runtime_receipt_reference,
    evaluate,
)
from evolve.evaluation.legacy import effective_task_set_identity
from evolve.frozen.interfaces import ArchiveView
from evolve.runtime import OwnedResult
from evolve.runtime.uv import CandidateRuntimeResult, RuntimeMount


def make_eval_script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def configure_outcome_evaluator(
    workspace: Path,
    *,
    timeout_rule: str | None = None,
    exit_code: int = 0,
) -> None:
    make_eval_script(
        workspace / "evaluator/eval.sh",
        "#!/bin/sh\n"
        "set -eu\n"
        'mkdir -p "$EVOLVE_RUN_DIR"\n'
        'task=$("$EVOLVE_FRAMEWORK_PYTHON" -c \'import json,sys; print(json.load(open(sys.argv[1]))["tasks"][0])\' "$EVOLVE_RUN_PLAN")\n'
        'outcome="${TEST_EVAL_OUTCOME:-benchmark_complete}"\n'
        'reward="1.0"; owner="benchmark"\n'
        'case "$outcome" in\n'
        '  candidate_invalid) reward="null"; owner="candidate" ;;\n'
        '  infrastructure_failed) reward="null"; owner="infrastructure" ;;\n'
        '  timeout) reward="0.0"; owner="benchmark_agent" ;;\n'
        '  cancelled) reward="null"; owner="evaluator" ;;\n'
        "esac\n"
        'printf \'{"schema_version":1,"tasks":{"%s":{"trials":[{"trial":0,"status":"%s","reward":%s,"owner":"%s"}]}}}\\n\' '
        '"$task" "$outcome" "$reward" "$owner" > "$EVOLVE_RUN_DIR/task_vector.json"\n'
        f"exit {exit_code}\n",
    )
    config = workspace / "evolve.yaml"
    text = config.read_text().replace("tasks_per_round: 16", "tasks_per_round: 1")
    if timeout_rule is not None:
        text = text.replace(
            "  tasks_per_round: 1\n", f"  tasks_per_round: 1\n  benchmark_timeout_is_zero: {timeout_rule}\n"
        )
    config.write_text(text)
    splits_path = workspace / "evaluator/splits.json"
    splits = json.loads(splits_path.read_text())
    splits["gate_tasks_per_round"] = 1
    splits_path.write_text(json.dumps(splits) + "\n")
    git(workspace, "add", "evaluator/eval.sh", "evaluator/splits.json", "evolve.yaml")
    git(workspace, "commit", "-m", "configure canonical outcome evaluator")
    git(workspace, "tag", "-f", "gen/0")


def test_complete_trial_vector_outweighs_nonzero_aggregate_evaluator_exit(tmp_path: Path) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)
    configure_outcome_evaluator(workspace, exit_code=3)

    record = evaluate(workspace, "gen/0", "0", purpose="genesis")

    assert record.outcome is Outcome.BENCHMARK_COMPLETE
    assert record.score == 1.0
    attempts = list((workspace / "runs/evaluations/genesis/gen-0").glob("*/attempt-1"))
    assert len(attempts) == 1
    assert (attempts[0] / "status").read_text() == "complete\n"
    assert (attempts[0] / "score").read_text() == "1.0\n"


def prepare_lifecycle_generation(workspace: Path) -> None:
    configure_outcome_evaluator(workspace)
    driver_run(RunOptions(workspace, max_generations=0))
    git(workspace, "tag", "gen/1", "gen/0")
    append_event(
        workspace,
        workspace.name,
        {
            "genid": "1",
            "parent": "0",
            "tag": "gen/1",
            "mutated": [],
            "surface_violations": [],
        },
    )


def test_archive_view_reads_markerless_legacy_evaluation_without_rewrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive = workspace / "archive.jsonl"
    archive.write_text(
        json.dumps(
            {
                "genid": "1",
                "parent": "0",
                "tag": "gen/1",
                "score": 0.0,
                "status": "complete",
                "task_set_hash": "legacy-task-set",
                "task_vector": None,
                "evaluator_tree": "legacy-evaluator",
                "valid_parent": True,
                "verdict": "keep",
                "reason": "mechanism evaluation stamp",
                "pending_gate_record": True,
                "note": "mechanism evaluation recorded before gate/record",
                "cost": {"usd": 0, "wall_s": 1.0},
            }
        )
        + "\n"
    )
    before = archive.read_bytes()

    row = ArchiveView(workspace).row("1")

    assert row is not None
    assert row["status"] == "complete"
    assert row["score"] == 0.0
    assert row["valid_parent"] is True
    assert archive.read_bytes() == before


def test_eval_script_receives_persistent_workspace_uv_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    checkout = tmp_path / "checkout"
    (checkout / "evaluator").mkdir(parents=True)
    make_eval_script(
        checkout / "evaluator" / "eval.sh",
        "#!/bin/sh\n"
        "set -eu\n"
        'mkdir -p "$EVOLVE_RUN_DIR"\n'
        'printf "%s\\n" "$EVOLVE_UV_CACHE_DIR" > cache-path\n'
        'printf "complete\\n" > "$EVOLVE_RUN_DIR/status"\n'
        'printf "1.0\\n" > "$EVOLVE_RUN_DIR/score"\n',
    )
    run_dir = workspace / "runs" / "gen-1" / "eval"
    run_dir.mkdir(parents=True)

    result = _run_eval_script(
        checkout,
        run_dir,
        "1",
        None,
        "research",
        "gate",
        CandidateRuntimeResult(None, None),
    )

    assert result.returncode == 0
    expected = workspace / "runs" / "runtime" / "uv-cache"
    assert (checkout / "cache-path").read_text() == f"{expected}\n"
    assert expected.is_dir()


def test_eval_script_preserves_explicit_shared_uv_cache(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    checkout = tmp_path / "checkout"
    (checkout / "evaluator").mkdir(parents=True)
    make_eval_script(
        checkout / "evaluator" / "eval.sh",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$EVOLVE_UV_CACHE_DIR" > cache-path\n',
    )
    run_dir = workspace / "runs" / "gen-1" / "eval"
    run_dir.mkdir(parents=True)
    shared_cache = tmp_path / "shared-cache"
    monkeypatch.setenv("EVOLVE_UV_CACHE_DIR", str(shared_cache))

    result = _run_eval_script(
        checkout,
        run_dir,
        "1",
        None,
        "research",
        "gate",
        CandidateRuntimeResult(None, None),
    )

    assert result.returncode == 0
    assert (checkout / "cache-path").read_text() == f"{shared_cache}\n"
    assert shared_cache.is_dir()


def test_eval_script_pins_checkout_pythonpath_instead_of_inheriting_host_value(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    checkout = tmp_path / "checkout"
    (checkout / "evaluator").mkdir(parents=True)
    make_eval_script(
        checkout / "evaluator" / "eval.sh",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$PYTHONPATH" > python-path\n',
    )
    run_dir = workspace / "runs" / "gen-1" / "eval"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "/hostile")

    result = _run_eval_script(
        checkout,
        run_dir,
        "1",
        None,
        "research",
        "gate",
        CandidateRuntimeResult(None, None),
    )

    assert result.returncode == 0
    assert (checkout / "python-path").read_text() == f"{checkout.resolve()}\n"


def test_eval_script_receives_candidate_runtime_json(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    checkout = tmp_path / "checkout"
    (checkout / "evaluator").mkdir(parents=True)
    make_eval_script(
        checkout / "evaluator" / "eval.sh",
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "%s\\n" "$EVOLVE_CANDIDATE_RUNTIME_ENV_JSON" > runtime-env\n'
        'printf "%s\\n" "$EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON" > runtime-mounts\n',
    )
    run_dir = workspace / "runs" / "gen-1" / "eval"
    run_dir.mkdir(parents=True)
    runtime = CandidateRuntimeResult(
        "uv",
        "target",
        environment=(("UV_OFFLINE", "1"),),
        mounts=(RuntimeMount(tmp_path / "cache", "/opt/evolve/uv/cache"),),
    )

    result = _run_eval_script(checkout, run_dir, "1", None, "research", "gate", runtime)

    assert result.returncode == 0
    assert json.loads((checkout / "runtime-env").read_text()) == {"UV_OFFLINE": "1"}
    assert json.loads((checkout / "runtime-mounts").read_text())[0]["target"] == "/opt/evolve/uv/cache"


def test_eval_script_omits_runtime_contract_when_preparation_is_disabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    checkout = tmp_path / "checkout"
    (checkout / "evaluator").mkdir(parents=True)
    make_eval_script(
        checkout / "evaluator" / "eval.sh",
        "#!/bin/sh\n"
        "set -eu\n"
        'test -z "${EVOLVE_CANDIDATE_RUNTIME_ENV_JSON+x}"\n'
        'test -z "${EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON+x}"\n',
    )
    run_dir = workspace / "runs" / "gen-1" / "eval"
    run_dir.mkdir(parents=True)

    result = _run_eval_script(
        checkout,
        run_dir,
        "1",
        None,
        "research",
        "gate",
        CandidateRuntimeResult(None, None),
    )

    assert result.returncode == 0


@pytest.mark.parametrize("outcome", [Outcome.CANDIDATE_INVALID, Outcome.INFRASTRUCTURE_FAILED])
def test_runtime_preparation_failure_short_circuits_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Outcome,
) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)
    configure_outcome_evaluator(workspace)
    called = False

    def fake_prepare(checkout, run_dir, runtime_root, candidate_commit, evaluator, **kwargs):
        return CandidateRuntimeResult(
            "uv",
            "target",
            outcome=outcome,
            reason="runtime preparation failed",
        )

    def fake_eval(*args, **kwargs):
        nonlocal called
        called = True
        return OwnedResult(0, "", "", 0.0, False)

    monkeypatch.setattr(evaluator_module, "prepare_candidate_runtime", fake_prepare)
    monkeypatch.setattr(evaluator_module, "_run_eval_script", fake_eval)

    record = evaluate(workspace, "gen/0", "0", purpose="genesis")

    assert record.outcome is outcome
    assert record.score is None
    assert not called
    assert record.candidate_runtime is None


def test_runtime_receipt_reference_is_compact_and_hashed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    receipt = workspace / "runs" / "candidate-runtime.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"outcome":"ready"}\n')

    assert _runtime_receipt_reference(workspace, receipt) == {
        "path": "runs/candidate-runtime.json",
        "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
    }


def test_eval_script_receives_configured_candidate_cohort(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    checkout = tmp_path / "checkout"
    (checkout / "evaluator").mkdir(parents=True)
    make_eval_script(
        checkout / "evaluator" / "eval.sh",
        '#!/bin/sh\nset -eu\nprintf "%s\\n" "$EVOLVE_EVAL_SPLIT" > selected-split\n',
    )
    run_dir = workspace / "runs" / "evaluations" / "candidate" / "gen-1" / "attempt-1"
    run_dir.mkdir(parents=True)

    result = _run_eval_script(
        checkout,
        run_dir,
        "1",
        None,
        "candidate",
        "train",
        CandidateRuntimeResult(None, None),
    )

    assert result.returncode == 0
    assert (checkout / "selected-split").read_text() == "train\n"


def test_timeout_zero_rejects_non_boolean_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)
    configure_outcome_evaluator(workspace, timeout_rule='"false"')
    monkeypatch.setenv("TEST_EVAL_OUTCOME", "timeout")

    with pytest.raises(ValueError, match="benchmark_timeout_is_zero must be a boolean"):
        evaluate(workspace, "gen/0", "0", purpose="genesis")


def test_evaluator_validates_task_vectors_and_compacts_artifact_references(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    run_dir = workspace / "runs" / "gen-1" / "eval"
    run_dir.mkdir(parents=True)
    vector_path = run_dir / "task_vector.json"
    vector_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": {"case-a": {"trials": [{"trial": 0, "status": "benchmark_complete", "reward": 1.0}]}},
            }
        )
    )
    artifacts_path = run_dir / "evaluation_artifacts.json"
    artifacts_path.write_text('{"jobs_dir":"/retained/jobs","trials":[]}\n')

    assert _read_task_vector(run_dir) == json.loads(vector_path.read_text())
    assert _evaluation_artifact_reference(workspace, run_dir) == {
        "path": "runs/gen-1/eval/evaluation_artifacts.json",
        "sha256": hashlib.sha256(artifacts_path.read_bytes()).hexdigest(),
    }

    vector_path.write_text('{"schema_version": 99, "tasks": {}}\n')
    with pytest.raises(TaskVectorError, match="unsupported task vector schema"):
        _read_task_vector(run_dir)


def test_evaluator_tree_mismatch_does_not_consume_attempt_identity(tmp_path: Path) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)
    make_eval_script(workspace / "evaluator/eval.sh", "#!/bin/sh\nexit 0\n")
    git(workspace, "add", "evaluator/eval.sh")
    git(workspace, "commit", "-m", "mutate evaluator")
    git(workspace, "tag", "gen/1")

    with pytest.raises(RuntimeError, match="differs from gen/0"):
        evaluate(workspace, "gen/1", "1")

    assert not (workspace / "runs/evaluations/candidate/gen-1").exists()


def test_effective_task_set_identity_uses_configured_names_dataset_and_attempts(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    tasks = checkout / "evaluator" / "tasks"
    tasks.mkdir(parents=True)
    (checkout / "evaluator" / "splits.json").write_text('{"unchanged": true}\n')
    (tasks / "smoke.txt").write_text("task-a\ntask-b\n")
    (tasks / "train.txt").write_text("task-a\ntask-b\ntask-c\n")

    smoke = effective_task_set_identity(
        checkout,
        {"dataset": "suite@1", "k": 2, "task_file": "evaluator/tasks/smoke.txt"},
    )
    train = effective_task_set_identity(
        checkout,
        {"dataset": "suite@1", "k": 2, "task_file": "evaluator/tasks/train.txt"},
    )
    different_k = effective_task_set_identity(
        checkout,
        {"dataset": "suite@1", "k": 1, "task_file": "evaluator/tasks/smoke.txt"},
    )

    assert smoke.members == ("task-a", "task-b")
    assert train.members == ("task-a", "task-b", "task-c")
    assert len({smoke.digest, train.digest, different_k.digest}) == 3


def test_effective_task_set_identity_accepts_explicit_configured_task_names(tmp_path: Path) -> None:
    identity = effective_task_set_identity(
        tmp_path,
        {"dataset": "stub", "k": 2, "task_names": ["task-b", "task-a"]},
    )

    assert identity.members == ("task-a", "task-b")
    expected_payload = b'{"attempts":2,"dataset":"stub","tasks":["task-a","task-b"]}'
    assert identity.digest == hashlib.sha256(expected_payload).hexdigest()


def test_full_scope_candidate_identity_contains_all_train_tasks(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    evaluator_dir = checkout / "evaluator"
    evaluator_dir.mkdir(parents=True)
    (evaluator_dir / "splits.json").write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": {"train": ["a", "b", "c"], "gate": [], "sealed": []},
            }
        )
    )
    evaluator = {
        "dataset": "terminal-bench@2.0",
        "evaluation_split": "train",
        "k": 2,
    }

    identity = effective_task_set_identity(checkout, evaluator)

    assert identity.members == ("a", "b", "c")


def test_hand_edited_artifact_hash_cannot_replace_mechanism_stamp(tmp_path: Path) -> None:
    workspace, _evolve_home = init_workspace(tmp_path)
    initial = rows_by_genid(workspace)["0"]
    expected = {"path": "runs/gen-0/eval/evaluation_artifacts.json", "sha256": "authentic"}
    append_event(
        workspace,
        workspace.name,
        {
            **initial,
            "artifacts": expected,
            "reason": "mechanism evaluation stamp",
            "note": "real baseline eval",
            "cost": {"usd": 0, "wall_s": 1.0},
            MECHANISM_EVAL_FIELD: True,
        },
    )
    append_event(
        workspace,
        workspace.name,
        {
            **initial,
            "artifacts": {**expected, "sha256": "forged"},
            "reason": "hand-edited artifact hash",
            "note": "manual attempt",
        },
    )

    assert rows_by_genid(workspace)["0"]["artifacts"] == expected
