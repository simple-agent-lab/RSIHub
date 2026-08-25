from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import cast
from urllib.parse import quote

from harbor.viewer.scanner import JobScanner
from harbor.viewer.trial_utils import agent_name_from_result, trial_summary_from_config

from .models import HarborTrialLink, JobRootReference

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class HarborFederation:
    root: Path
    job_names: dict[tuple[Path, str], str]
    trial_links: dict[tuple[str, str, str, int], HarborTrialLink]


class HarborBridge:
    """Expose referenced Harbor jobs through a disposable symlink directory."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None

    def __enter__(self) -> HarborBridge:
        if self._tempdir is None:
            try:
                self._tempdir = tempfile.TemporaryDirectory(prefix=".evolve-view-harbor-", dir=self.workspace.parent)
            except OSError:
                self._tempdir = tempfile.TemporaryDirectory(prefix="evolve-view-harbor-")
            self.root = Path(self._tempdir.name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
        self._tempdir = None
        self.root = None

    def refresh(
        self,
        job_roots: Iterable[JobRootReference],
        *,
        canonical_tasks: Mapping[tuple[str, str], Iterable[str]] | None = None,
    ) -> HarborFederation:
        root = self._require_root()
        references = tuple(job_roots)
        desired = _federated_jobs(references)
        for name, target in desired.values():
            destination = root / name
            if not destination.exists():
                temporary = root / f".{name}.next"
                _remove_path(temporary)
                shutil.copytree(target, temporary, copy_function=_link_or_copy)
                _normalize_copied_job(temporary)
                os.replace(temporary, destination)
        expected = {name for name, _target in desired.values()}
        for entry in root.iterdir():
            if entry.name not in expected:
                _remove_path(entry)

        job_names = {key: value[0] for key, value in desired.items()}
        references_by_job: dict[str, list[JobRootReference]] = {}
        for reference in references:
            for child in _job_children(reference.path):
                name = job_names.get((reference.path.resolve(), child.name))
                if name is not None and reference not in references_by_job.setdefault(name, []):
                    references_by_job[name].append(reference)
        return HarborFederation(
            root=root,
            job_names=job_names,
            trial_links=_trial_links(root, references_by_job, canonical_tasks or {}),
        )

    def _require_root(self) -> Path:
        if self.root is None:
            raise RuntimeError("HarborBridge must be entered before refresh")
        return self.root


def _federated_jobs(
    references: Iterable[JobRootReference],
) -> dict[tuple[Path, str], tuple[str, Path]]:
    jobs: dict[tuple[Path, str], tuple[str, Path]] = {}
    for reference in references:
        source_root = reference.path.resolve()
        for child in _job_children(source_root):
            digest = hashlib.sha256(str(child.resolve()).encode()).hexdigest()[:10]
            stem = _SAFE_NAME.sub("-", child.name).strip("-.") or "job"
            jobs[(source_root, child.name)] = (f"{stem}-{digest}", child.resolve())
    return jobs


def _job_children(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            (
                child
                for child in root.iterdir()
                if child.is_dir() and ((child / "config.json").is_file() or (child / "result.json").is_file())
            ),
            key=lambda child: child.name,
        )
    )


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _normalize_copied_job(job: Path) -> None:
    """Make retained writable-mount evidence readable by current Harbor models.

    Historical RSIHub trials can truthfully record writable cache mounts, while
    Harbor 0.18 accepts only read-only mounts when loading results in its viewer.
    The bridge changes only its disposable copy: replacing a normalized JSON
    file also breaks the hard link, leaving the experiment evidence untouched.
    """
    for trial in job.iterdir():
        if not trial.is_dir():
            continue
        for filename, nested in (("config.json", False), ("result.json", True)):
            path = trial / filename
            document = _json_object(path)
            config = document.get("config") if nested else document
            if not _mark_mounts_read_only(config):
                continue
            temporary = path.with_name(f".{filename}.viewer-normalized")
            temporary.write_text(json.dumps(document, separators=(",", ":")))
            os.replace(temporary, path)


def _mark_mounts_read_only(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    environment = value.get("environment")
    mounts = environment.get("mounts") if isinstance(environment, dict) else None
    changed = False
    if isinstance(mounts, list):
        for mount in mounts:
            if isinstance(mount, dict) and mount.get("read_only") is False:
                cast(dict[str, object], mount)["read_only"] = True
                changed = True
    return changed


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _trial_links(
    root: Path,
    references_by_job: Mapping[str, Iterable[JobRootReference]],
    canonical_tasks: Mapping[tuple[str, str], Iterable[str]],
) -> dict[tuple[str, str, str, int], HarborTrialLink]:
    scanner = JobScanner(root)
    links: dict[tuple[str, str, str, int], HarborTrialLink] = {}
    for job_name, references in sorted(references_by_job.items()):
        trials = [
            (trial_name, _trial_evidence(scanner, root, job_name, trial_name))
            for trial_name in scanner.list_trials(job_name)
        ]
        for reference in references:
            key = (reference.generation, reference.purpose)
            candidates = tuple(str(task) for task in canonical_tasks.get(key, ()))
            repetitions: Counter[str] = Counter()
            for trial_name, trial in trials:
                if trial is None:
                    continue
                task_name, source, agent, provider, model, reward, duration_ms = trial
                canonical = _canonical_task(task_name, candidates)
                if canonical is None:
                    continue
                repetition = repetitions[canonical]
                repetitions[canonical] += 1
                parts = [
                    quote(part or "unknown", safe="")
                    for part in (job_name, source, agent, provider, model, task_name, trial_name)
                ]
                url = f"/jobs/{parts[0]}/tasks/{'/'.join(parts[1:6])}/trials/{parts[6]}"
                links[(reference.generation, reference.purpose, canonical, repetition)] = HarborTrialLink(
                    url=url,
                    reward=reward,
                    duration_ms=duration_ms,
                )
    return links


def _trial_evidence(
    scanner: JobScanner, root: Path, job_name: str, trial_name: str
) -> tuple[str, str | None, str | None, str | None, str | None, float | None, float | None] | None:
    trial_dir = root / job_name / trial_name
    raw_result = _json_object(trial_dir / "result.json")
    raw_config = _json_object(trial_dir / "config.json")
    if _has_legacy_writable_mount(raw_result.get("config")):
        return _raw_result_evidence(raw_result)
    if not raw_result and _has_legacy_writable_mount(raw_config):
        return _raw_config_evidence(raw_config)
    result = scanner.get_trial_result(job_name, trial_name)
    if result is not None:
        model = result.agent_info.model_info
        reward = (
            result.verifier_result.rewards.get("reward")
            if result.verifier_result and result.verifier_result.rewards
            else None
        )
        duration = (
            (result.finished_at - result.started_at).total_seconds() * 1000
            if result.finished_at is not None and result.started_at is not None
            else None
        )
        return (
            result.task_name,
            result.source,
            agent_name_from_result(result),
            model.provider if model else None,
            model.name if model else None,
            float(reward) if isinstance(reward, (int, float)) else None,
            duration,
        )
    config = scanner.get_trial_config(job_name, trial_name)
    if config is None:
        return _raw_result_evidence(raw_result) or _raw_config_evidence(raw_config)
    summary = trial_summary_from_config(trial_name, config)
    return (
        summary.task_name,
        summary.source,
        summary.agent_name,
        summary.model_provider,
        summary.model_name,
        summary.reward,
        None,
    )


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _has_legacy_writable_mount(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    environment = value.get("environment")
    mounts = environment.get("mounts") if isinstance(environment, dict) else None
    return isinstance(mounts, list) and any(
        isinstance(mount, dict) and mount.get("read_only") is False for mount in mounts
    )


def _raw_result_evidence(
    value: dict[str, object],
) -> tuple[str, str | None, str | None, str | None, str | None, float | None, float | None] | None:
    task = value.get("task_name")
    if not isinstance(task, str):
        return None
    agent_info = value.get("agent_info")
    agent = agent_info.get("name") if isinstance(agent_info, dict) else None
    model_info = agent_info.get("model_info") if isinstance(agent_info, dict) else None
    provider = model_info.get("provider") if isinstance(model_info, dict) else None
    model = model_info.get("name") if isinstance(model_info, dict) else None
    verifier = value.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    source = value.get("source")
    started = _datetime(value.get("started_at"))
    finished = _datetime(value.get("finished_at"))
    duration = (finished - started).total_seconds() * 1000 if started and finished else None
    return (
        task,
        source if isinstance(source, str) else None,
        agent if isinstance(agent, str) else None,
        provider if isinstance(provider, str) else None,
        model if isinstance(model, str) else None,
        float(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool) else None,
        duration,
    )


def _raw_config_evidence(
    value: dict[str, object],
) -> tuple[str, str | None, str | None, str | None, str | None, float | None, float | None] | None:
    task_config = value.get("task")
    if not isinstance(task_config, dict):
        return None
    task = task_config.get("name") or task_config.get("path")
    if not isinstance(task, str):
        return None
    agent_config = value.get("agent")
    source = task_config.get("source")
    agent = agent_config.get("name") or agent_config.get("import_path") if isinstance(agent_config, dict) else None
    model_name = agent_config.get("model_name") if isinstance(agent_config, dict) else None
    provider, model = (
        model_name.split("/", 1) if isinstance(model_name, str) and "/" in model_name else (None, model_name)
    )
    return (
        task,
        source if isinstance(source, str) else None,
        agent if isinstance(agent, str) else None,
        provider,
        model if isinstance(model, str) else None,
        None,
        None,
    )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _canonical_task(harbor_task: str, candidates: tuple[str, ...]) -> str | None:
    if not candidates:
        return harbor_task
    if harbor_task in candidates:
        return harbor_task
    matches = [
        candidate
        for candidate in candidates
        if candidate.endswith(f"__{harbor_task}")
        or harbor_task.endswith(f"__{candidate}")
        or harbor_task.endswith(f"/{candidate}")
    ]
    return matches[0] if len(matches) == 1 else None
