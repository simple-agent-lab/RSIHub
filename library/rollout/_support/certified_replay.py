"""Digest-verified replay of certified evaluation artifacts for rollout operators."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, cast

_SHA256 = re.compile(r"[0-9a-f]{64}")
_CERTIFIED_REPLAY_ARTIFACTS = (
    "agent/mini-swe-agent.trajectory.json",
    "agent/mini-swe-agent.txt",
    "agent/trajectory.json",
    "verifier/diagnostics.json",
    "evolve-replay.json",
)


class _ArtifactSnapshot:
    """One digest-verified read of an evaluation artifact and its parsed JSON."""

    __slots__ = ("document", "json_valid", "path", "payload", "sha256")

    def __init__(
        self,
        *,
        path: Path,
        payload: bytes,
        sha256: str,
        document: object,
        json_valid: bool,
    ) -> None:
        self.path = path
        self.payload = payload
        self.sha256 = sha256
        self.document = document
        self.json_valid = json_valid


def _load_collect_cases(checkout: Path) -> Callable[..., list[dict[str, Any]]]:
    path = checkout / "library" / "_shared" / "harbor" / "evidence.py"
    if not path.is_file():
        raise SystemExit(f"vendored Harbor runtime is missing: {path}")
    spec = importlib.util.spec_from_file_location("evolve_parent_evaluation_harbor", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load vendored Harbor runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    collector = getattr(module, "collect_cases", None)
    if not callable(collector):
        raise SystemExit("vendored Harbor runtime has no collect_cases helper")
    return collector


def _verified_artifact_reference(workspace: Path, reference: object, *, description: str) -> _ArtifactSnapshot:
    if not isinstance(reference, dict):
        raise SystemExit(f"{description} has no certified evaluation artifacts")
    relative = reference.get("path")
    expected = reference.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise SystemExit(f"{description} has malformed evaluation artifacts")
    path = (workspace / relative).resolve()
    try:
        path.relative_to(workspace.resolve())
    except ValueError as error:
        raise SystemExit("evaluation artifact path escapes workspace") from error
    if not path.is_file():
        raise SystemExit(f"evaluation artifact path is missing: {relative}")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise SystemExit(f"evaluation artifact path is unreadable: {relative}") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected:
        raise SystemExit("evaluation artifact digest mismatch")
    try:
        document = json.loads(payload)
        json_valid = True
    except (UnicodeDecodeError, json.JSONDecodeError):
        document = None
        json_valid = False
    return _ArtifactSnapshot(
        path=path,
        payload=payload,
        sha256=digest,
        document=document,
        json_valid=json_valid,
    )


def _certified_artifact(workspace: Path, parent: str, row: dict[str, Any]) -> _ArtifactSnapshot:
    return _verified_artifact_reference(
        workspace,
        row.get("artifacts"),
        description=f"selected parent {parent}",
    )


def _artifact_sources(
    workspace: Path,
    parent: str,
    artifact: _ArtifactSnapshot,
) -> list[tuple[_ArtifactSnapshot, set[str] | None, bool]]:
    """Return artifact, repaired-task filter, and whether the filter is inclusive."""
    manifest = artifact.document if artifact.json_valid else None
    if not isinstance(manifest, dict) or manifest.get("kind") != "failed_task_repair":
        return [(artifact, None, True)]

    replaced = manifest.get("replaced_slots")
    if not isinstance(replaced, list):
        raise SystemExit(f"selected parent {parent} has malformed repair artifact slots")
    repaired_tasks = {
        task_id for slot in replaced if isinstance(slot, dict) and isinstance(task_id := slot.get("task_id"), str)
    }
    if not repaired_tasks:
        raise SystemExit(f"selected parent {parent} repair artifact has no replaced tasks")
    base = _verified_artifact_reference(
        workspace,
        manifest.get("base_artifacts"),
        description=f"selected parent {parent} base attempt",
    )
    repair = _verified_artifact_reference(
        workspace,
        manifest.get("repair_artifacts"),
        description=f"selected parent {parent} repair attempt",
    )
    return [
        (base, repaired_tasks, False),
        (repair, repaired_tasks, True),
    ]


def _artifact_index(artifact: _ArtifactSnapshot) -> dict[str, Any]:
    if not artifact.json_valid:
        raise SystemExit("evaluation artifact index is unreadable")
    payload = artifact.document
    if not isinstance(payload, dict) or not isinstance(payload.get("trials"), list):
        raise SystemExit("evaluation artifact index is malformed")
    return cast(dict[str, Any], payload)


def _indexed_path(jobs_dir: Path, relative: object) -> tuple[str, Path]:
    if not isinstance(relative, str) or not relative:
        raise SystemExit("evaluation artifact file has malformed path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SystemExit(f"evaluation artifact file path escapes jobs: {relative}")
    target = jobs_dir.joinpath(*pure.parts)
    try:
        target.resolve().relative_to(jobs_dir.resolve())
    except ValueError as error:
        raise SystemExit(f"evaluation artifact file path escapes jobs: {relative}") from error
    return pure.as_posix(), target


def _verified_indexed_files(artifact: _ArtifactSnapshot) -> tuple[Path, dict[str, bytes]]:
    """Verify the file index and return one immutable read of every certified file."""
    index = _artifact_index(artifact)
    jobs_dir = artifact.path.parent / "jobs"
    if not jobs_dir.is_dir():
        raise SystemExit(f"evaluation jobs directory is missing: {jobs_dir}")

    certified: dict[str, bytes] = {}
    for trial in index["trials"]:
        if not isinstance(trial, dict) or not isinstance(trial.get("files"), list):
            raise SystemExit("evaluation artifact trial is malformed")
        result_paths: list[str] = []
        for entry in trial["files"]:
            if not isinstance(entry, dict):
                raise SystemExit("evaluation artifact file entry is malformed")
            relative, path = _indexed_path(jobs_dir, entry.get("path"))
            expected_bytes = entry.get("bytes")
            expected_sha256 = entry.get("sha256")
            if (
                isinstance(expected_bytes, bool)
                or not isinstance(expected_bytes, int)
                or expected_bytes < 0
                or not isinstance(expected_sha256, str)
                or _SHA256.fullmatch(expected_sha256) is None
            ):
                raise SystemExit(f"evaluation artifact file entry is malformed: {relative}")
            if relative in certified:
                raise SystemExit(f"evaluation artifact file path is duplicated: {relative}")
            if not path.is_file():
                raise SystemExit(f"evaluation artifact file is missing: {relative}")
            payload = path.read_bytes()
            if len(payload) != expected_bytes:
                raise SystemExit(f"evaluation artifact file size mismatch: {relative}")
            if hashlib.sha256(payload).hexdigest() != expected_sha256:
                raise SystemExit(f"evaluation artifact file digest mismatch: {relative}")
            certified[relative] = payload
            if PurePosixPath(relative).name == "evolve-replay.json":
                result_paths.append(relative)
        if len(result_paths) != 1:
            raise SystemExit("evaluation artifact trial must certify exactly one evolve-replay.json")

    indexed_results = {relative for relative in certified if PurePosixPath(relative).name == "evolve-replay.json"}
    actual_results = {
        path.relative_to(jobs_dir).as_posix()
        for path in jobs_dir.rglob("evolve-replay.json")
        if path.is_file() and _is_replay_result(path)
    }
    extra_results = sorted(actual_results - indexed_results)
    if extra_results:
        raise SystemExit(f"evaluation artifact contains unindexed replay result: {extra_results[0]}")
    extra_artifacts: list[str] = []
    for result in indexed_results:
        trial_root = PurePosixPath(result).parent
        for suffix in _CERTIFIED_REPLAY_ARTIFACTS:
            relative = (trial_root / suffix).as_posix()
            if relative in certified:
                continue
            if jobs_dir.joinpath(*PurePosixPath(relative).parts).is_file():
                extra_artifacts.append(relative)
    if extra_artifacts:
        raise SystemExit(f"evaluation artifact contains unindexed replay file: {sorted(extra_artifacts)[0]}")
    return jobs_dir, certified


def _is_replay_result(path: Path) -> bool:
    """Match the collector's rule for result files that can create a case."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("task_name")) and bool(payload.get("trial_name"))


def _materialize_certified_jobs(
    certified_root: Path,
    source_index: int,
    artifact: _ArtifactSnapshot,
    certified: dict[str, bytes],
) -> Path:
    """Persist a read-only-by-convention view containing only certified bytes."""
    replay_jobs = certified_root / f"source-{source_index}-{artifact.sha256[:16]}"
    replay_jobs.mkdir()
    for relative, payload in certified.items():
        destination = replay_jobs.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        destination.chmod(0o444)
    directories = sorted(
        (path for path in replay_jobs.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        directory.chmod(0o555)
    replay_jobs.chmod(0o555)
    return replay_jobs


def _certify_result_paths(
    cases: list[dict[str, Any]],
    replay_jobs: Path,
    certified: dict[str, bytes],
    workspace: Path | None = None,
) -> None:
    """Reject collector paths that do not name certified files in the view."""
    replay_root = replay_jobs.resolve()
    for case in cases:
        value = case.get("result_path")
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise SystemExit("evaluation replay collector returned a malformed result path")
        try:
            relative = Path(value).resolve().relative_to(replay_root)
        except ValueError as error:
            raise SystemExit("evaluation replay collector returned a path outside the certified view") from error
        relative_string = relative.as_posix()
        if relative_string not in certified or PurePosixPath(relative_string).name != "evolve-replay.json":
            raise SystemExit("evaluation replay collector returned an uncertified result path")
        case["result_path"] = str(replay_jobs.joinpath(*PurePosixPath(relative_string).parts))

        execution = case.get("execution")
        trajectory = execution.get("trajectory") if isinstance(execution, dict) else None
        if not isinstance(trajectory, dict) or trajectory.get("status") != "available":
            continue
        trajectory_path = trajectory.get("path")
        expected_digest = trajectory.get("sha256")
        if not isinstance(trajectory_path, str) or not isinstance(expected_digest, str):
            raise SystemExit("evaluation replay collector returned a malformed trajectory reference")
        candidate = Path(trajectory_path)
        if not candidate.is_absolute():
            if workspace is None:
                raise SystemExit("evaluation replay cannot resolve a workspace-relative trajectory reference")
            candidate = workspace / candidate
        try:
            trajectory_relative = candidate.resolve().relative_to(replay_root)
        except ValueError as error:
            raise SystemExit("evaluation replay collector returned a trajectory outside the certified view") from error
        trajectory_relative_string = trajectory_relative.as_posix()
        trajectory_parts = PurePosixPath(trajectory_relative_string).parts
        if (
            trajectory_relative_string not in certified
            or len(trajectory_parts) < 2
            or trajectory_parts[-2:] != ("agent", "trajectory.json")
        ):
            raise SystemExit("evaluation replay collector returned an uncertified trajectory path")
        if hashlib.sha256(certified[trajectory_relative_string]).hexdigest() != expected_digest:
            raise SystemExit("evaluation replay collector returned a trajectory digest mismatch")


def replay_certified_cases(
    *,
    workspace: Path,
    genid: str,
    row: dict[str, Any],
    load_collector: Callable[[], Callable[..., list[dict[str, Any]]]],
    destination: Path,
    field_limit: int,
    pass_threshold: float,
) -> tuple[list[dict[str, Any]], list[Path], _ArtifactSnapshot]:
    """Replay one candidate's certified evaluation into a read-only jobs view under ``destination``.

    Every artifact is digest-verified before ``load_collector`` runs, so missing or
    tampered evidence fails before the vendored Harbor collector is imported.
    """
    artifact = _certified_artifact(workspace, genid, row)
    sources = [
        (source, *_verified_indexed_files(source), task_filter, inclusive)
        for source, task_filter, inclusive in _artifact_sources(workspace, genid, artifact)
    ]
    collector = load_collector()
    cases: list[dict[str, Any]] = []
    jobs_dirs: list[Path] = []
    destination.mkdir(parents=True, exist_ok=True)
    certified_parent = destination / "certified-jobs"
    certified_parent.mkdir(exist_ok=True)
    certified_root = Path(tempfile.mkdtemp(prefix="snapshot-", dir=certified_parent))
    collector_parameters = inspect.signature(collector).parameters
    for source_index, (source, _original_jobs, certified, task_filter, inclusive) in enumerate(sources):
        replay_jobs = _materialize_certified_jobs(certified_root, source_index, source, certified)
        jobs_dirs.append(replay_jobs)
        collector_kwargs: dict[str, Any] = {"field_limit": field_limit, "pass_threshold": pass_threshold}
        if "workspace" in collector_parameters:
            collector_kwargs["workspace"] = workspace
        source_cases = collector(replay_jobs, **collector_kwargs)
        _certify_result_paths(source_cases, replay_jobs, certified, workspace=workspace)
        if task_filter is not None:
            source_cases = [case for case in source_cases if (str(case.get("task_name")) in task_filter) is inclusive]
        cases.extend(source_cases)
    certified_root.chmod(0o555)
    return cases, jobs_dirs, artifact


def case_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Count replayed outcomes the way rollout summaries report them."""
    rewards = [
        float(case["reward"])
        for case in cases
        if isinstance(case.get("reward"), (int, float)) and not isinstance(case.get("reward"), bool)
    ]
    tasks = {str(case["task_name"]) for case in cases}
    counts = {
        name: sum(case.get("outcome") == name for case in cases)
        for name in ("passed", "failed", "agent_error", "infra_error", "incomplete")
    }
    return {
        "tasks_requested": len(tasks),
        "tasks_observed": len(tasks),
        "trials_observed": len(cases),
        "passed": counts["passed"],
        "failed": counts["failed"],
        "agent_errors": counts["agent_error"],
        "infra_errors": counts["infra_error"] + counts["incomplete"],
        "mean_observed_reward": round(sum(rewards) / len(rewards), 6) if rewards else None,
    }
