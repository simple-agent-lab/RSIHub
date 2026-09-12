"""Pre-init checks: catch every `evolve init` refusal before anything is written.

Mirrors the validation `init_workspace` performs (recipe, seed, dataset,
runtime digest, workspace emptiness) plus the tools the console needs, so a
cold start fails here with a checklist instead of one refusal at a time.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..composition import RecipeResolutionError, render_recipe_problems, resolve_builtin_recipe, resolve_recipe
from ..config import DEFAULT_RECIPE
from ..splits import build_manifest

_TEST_ONLY_SEEDS = frozenset({"builtin-dummy"})
_BUILTIN_SEEDS = frozenset({"builtin-codex", "builtin-dsh", "builtin-local-smoke"})


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str


def _binary(name: str, reason: str) -> Check:
    path = shutil.which(name)
    if path:
        return Check(name, "ok", path)
    return Check(name, "fail", f"{name} is not on PATH; {reason}")


def _load_config(recipe: str | None, recipe_path: Path | None) -> tuple[dict[str, Any] | None, Check]:
    if recipe is not None and recipe_path is not None:
        return None, Check("recipe", "fail", "cannot combine --recipe with --recipe-path")
    try:
        if recipe_path is not None:
            resolved = resolve_recipe(recipe_path)
        else:
            name = recipe or DEFAULT_RECIPE
            resolved = resolve_builtin_recipe(name)
    except RecipeResolutionError as error:
        return None, Check("recipe", "fail", render_recipe_problems(error.problems))
    return resolved.config, Check("recipe", "ok", resolved.name)


def _check_digest(engine: str) -> Check:
    digest = os.environ.get("EVOLVE_RUNTIME_DIGEST", "").strip()
    if digest:
        return Check("runtime digest", "ok", digest)
    if engine == "harbor":
        return Check(
            "runtime digest",
            "fail",
            "EVOLVE_RUNTIME_DIGEST is not set; export the immutable evaluator runtime digest before `evolve init`",
        )
    return Check("runtime digest", "warn", "EVOLVE_RUNTIME_DIGEST is not set (not required by this evaluator engine)")


def _check_seed(seed: str) -> Check:
    if not seed:
        return Check("seed", "fail", "target.seed is required; pass --seed")
    if seed in _TEST_ONLY_SEEDS:
        return Check("seed", "fail", f"{seed} is test-only; pass a local seed directory instead")
    if seed in _BUILTIN_SEEDS:
        return Check("seed", "ok", f"{seed} (vendored at init)")
    if "://" in seed or seed.startswith("git@"):
        return Check("seed", "ok", f"{seed} (git URL, cloned at init; not verified offline)")
    candidate = Path(seed).expanduser()
    if candidate.is_dir():
        return Check("seed", "ok", str(candidate.resolve()))
    return Check("seed", "fail", f"seed directory does not exist: {seed}")


def _valid_harbor_task_dirs(dataset: Path) -> tuple[list[str], list[str]]:
    """Split task directories by Harbor's real discovery rule."""
    from harbor.models.task.task import Task

    if Task.is_valid_dir(dataset):
        return [dataset.name], []
    valid: list[str] = []
    invalid: list[str] = []
    for entry in sorted(dataset.iterdir()):
        if not entry.is_dir():
            continue
        (valid if Task.is_valid_dir(entry) else invalid).append(entry.name)
    return valid, invalid


def _check_dataset(evaluator: dict[str, Any], *, rollout_needs_dataset: bool) -> Check:
    dataset = str(evaluator.get("dataset") or "")
    if not dataset:
        return Check("dataset", "fail", "evaluator.dataset is empty; pass --dataset")
    candidate = Path(dataset).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if not candidate.is_dir():
        if rollout_needs_dataset:
            return Check(
                "dataset",
                "fail",
                f"{dataset!r} does not resolve to a local task directory, but the "
                "configured harbor rollout needs frozen split membership; pass --dataset",
            )
        return Check(
            "dataset",
            "warn",
            f"{dataset!r} does not resolve to a local task directory; splits stay symbolic",
        )
    resolved = candidate.resolve()
    split = evaluator.get("split")
    if not isinstance(split, dict):
        split = {"train": 1.0, "gate": 0.0, "sealed": 0.0, "seed": 0}
    valid, invalid = _valid_harbor_task_dirs(resolved)
    if valid and valid != [resolved.name]:
        try:
            build_manifest(
                dataset,
                split,
                base_dir=Path.cwd(),
                sampling=str(evaluator.get("sampling", "static")),
                gate_limit=int(evaluator.get("tasks_per_round", 1)),
            )
        except ValueError as error:
            return Check("dataset", "fail", str(error))
    if not valid:
        return Check(
            "dataset",
            "fail",
            f"{resolved} contains no directory Harbor discovers as a task "
            "(each needs task.toml, instruction.md, environment/, and tests/test.sh)",
        )
    if invalid:
        return Check(
            "dataset",
            "warn",
            f"{resolved}: {len(valid)} valid tasks; Harbor will silently skip {', '.join(invalid[:5])}",
        )
    return Check("dataset", "ok", f"{resolved} ({len(valid)} tasks)")


def _check_workspace(workspace: Path) -> Check:
    if workspace.exists() and any(workspace.iterdir()):
        if (workspace / "evolve.yaml").is_file():
            return Check("workspace", "fail", f"{workspace} is already initialized; use ./evolve status or doctor")
        return Check("workspace", "fail", f"workspace is not empty: {workspace}")
    return Check("workspace", "ok", f"{workspace} is empty or will be created")


def run_preflight(
    *,
    workspace: Path,
    recipe: str | None,
    recipe_path: Path | None,
    seed: str | None,
    dataset: str | None,
    tasks_per_round: int | None = None,
) -> list[Check]:
    checks = [
        _binary("uv", "the workspace console runs through it"),
        _binary("git", "lineage lives in git"),
    ]
    config, recipe_check = _load_config(recipe, recipe_path)
    checks.append(recipe_check)
    if config is not None:
        evaluator = dict(config.get("evaluator") or {})
        if dataset:
            evaluator["dataset"] = dataset
        if tasks_per_round is not None:
            evaluator["tasks_per_round"] = tasks_per_round
        target = dict(config.get("target") or {})
        engine = str(evaluator.get("engine") or "")
        checks.append(_check_digest(engine))
        if engine == "harbor" and not str(evaluator.get("agent") or ""):
            checks.append(Check("evaluator agent", "fail", "evaluator.agent is required for harbor recipes"))
        checks.append(_check_seed(seed or str(target.get("seed") or "")))
        operators = dict(config.get("operators") or {})
        rollout = operators.get("rollout")
        rollout_needs_dataset = isinstance(rollout, dict) and rollout.get("operator") == "harbor"
        checks.append(_check_dataset(evaluator, rollout_needs_dataset=rollout_needs_dataset))
    checks.append(_check_workspace(workspace))
    return checks


def render(checks: list[Check]) -> tuple[str, bool]:
    width = max(len(check.name) for check in checks)
    lines = [f"{check.status:<5} {check.name:<{width}}  {check.detail}" for check in checks]
    failures = sum(1 for check in checks if check.status == "fail")
    if failures:
        noun = "problem" if failures == 1 else "problems"
        lines.append(f"preflight: {failures} blocking {noun}; fix before `evolve init`")
    else:
        lines.append("preflight: ready for `evolve init`")
    return "\n".join(lines), failures == 0
