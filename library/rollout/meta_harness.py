"""Meta-Harness history store: replay every certified candidate, not only the selected parent.

Meta-Harness gives its proposer filesystem access to all prior candidates' code, scores, and
execution traces. This rollout replays the selected parent exactly like `parent_evaluation`, then
extends the run with a candidate store under `rollout/candidates/`: each certified candidate's
evaluation is replayed once, in the first generation that sees it, and later generations point
back to that replay instead of copying it again. Uncertified candidates are listed with their code
and status but no traces.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evolve.frozen import sdk
from evolve.frozen.config import Config, integer, number
from evolve.frozen.interfaces import ArchiveView, OperatorContext, RolloutOperator, RolloutResult
from evolve.git import git_stdout, tag_exists
from library.rollout._support.certified_replay import _load_collect_cases, case_summary, replay_certified_cases

CONFIG = Config(
    {
        "field_limit": integer(default=2000, minimum=1),
        "pass_threshold": number(default=1.0),
    }
)

INDEX = "index.json"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _prior_entries(workspace: Path, current_run: Path) -> dict[str, dict[str, Any]]:
    """Return one earlier index entry per candidate whose replayed cases still exist."""
    entries: dict[str, dict[str, Any]] = {}
    runs = workspace / "runs"
    if not runs.is_dir():
        return entries
    for run in sorted(runs.glob("gen-*"), key=lambda path: path.name):
        if run.resolve() == current_run.resolve():
            continue
        payload = _read_json(run / "rollout" / "candidates" / INDEX)
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
            continue
        for entry in payload["candidates"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("genid"), str):
                continue
            cases = entry.get("cases")
            if isinstance(cases, str) and (workspace / cases).is_file():
                entries[entry["genid"]] = entry
    return entries


def _reusable(prior: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    """A prior replay is reusable only if it replayed the artifact the row certifies now."""
    reference = row.get("artifacts")
    digest = reference.get("sha256") if isinstance(reference, dict) else None
    return prior is not None and isinstance(digest, str) and prior.get("artifact_sha256") == digest


def _per_task(cases: list[dict[str, Any]]) -> dict[str, float]:
    rewards: dict[str, list[float]] = defaultdict(list)
    for case in cases:
        reward = case.get("reward")
        if isinstance(reward, (int, float)) and not isinstance(reward, bool):
            rewards[str(case.get("task_name"))].append(float(reward))
    return {task: round(sum(values) / len(values), 6) for task, values in sorted(rewards.items())}


def _diff(workspace: Path, base: str | None, tag: str | None) -> str | None:
    if not base or not tag:
        return None
    try:
        if not (tag_exists(workspace, base) and tag_exists(workspace, tag)):
            return None
        diff = git_stdout(workspace, "diff", base, tag)
    except Exception:
        return None
    return diff + "\n" if diff else ""


def _relative(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def _frontier(entries: list[dict[str, Any]]) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for task, reward in (entry.get("per_task") or {}).items():
            best = tasks.get(task)
            if best is None or reward > best["reward"]:
                tasks[task] = {"reward": reward, "candidates": [entry["genid"]]}
            elif reward == best["reward"]:
                best["candidates"].append(entry["genid"])
    scored = [entry for entry in entries if entry.get("certified") and isinstance(entry.get("score"), (int, float))]
    best = max(scored, key=lambda entry: float(entry["score"]), default=None)
    return {
        "best": {"genid": best["genid"], "score": best["score"]} if best else None,
        "tasks": tasks,
    }


def _render_index(entries: list[dict[str, Any]], frontier: dict[str, Any]) -> str:
    lines = [
        "# Meta-Harness candidate store",
        "",
        "Every prior candidate, its code, score, and replayed traces. Paths are relative to the experiment "
        "workspace root.",
        "",
        "| gen | parent | score | status | trials passed | traces | diff vs parent | handoff |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        summary = entry.get("summary") or {}
        passed = f"{summary.get('passed')}/{summary.get('trials_observed')}" if summary else "-"
        lines.append(
            "| {genid} | {parent} | {score} | {status} | {passed} | {cases} | {diff} | {handoff} |".format(
                genid=entry["genid"],
                parent=entry.get("parent") or "-",
                score=entry.get("score"),
                status=entry.get("status"),
                passed=passed,
                cases=f"`{entry['cases']}`" if entry.get("cases") else "not replayed",
                diff=f"`{entry['diff_vs_parent']}`" if entry.get("diff_vs_parent") else "-",
                handoff=f"`{entry['handoff']}`" if entry.get("handoff") else "-",
            )
        )
    best = frontier.get("best")
    lines += [
        "",
        f"Best candidate by score: {best['genid']} ({best['score']})" if best else "Best candidate by score: none",
        "Per-task frontier: `frontier.json` in this directory.",
        "",
    ]
    return "\n".join(lines)


class MetaHarnessRollout(RolloutOperator):
    def rollout(self, checkout: Path, ctx: OperatorContext) -> RolloutResult:
        if ctx.parent is None:
            raise SystemExit("meta_harness rollout requires a selected parent")
        archive = ArchiveView(ctx.workspace)
        rows = archive.rows()
        by_id = {str(row.get("genid")): row for row in rows}
        if ctx.parent not in by_id:
            raise SystemExit(f"selected parent {ctx.parent} is missing from archive")
        certified = {str(row.get("genid")) for row in archive.valid_parents()}
        if ctx.parent not in certified:
            raise SystemExit(f"selected parent {ctx.parent} has no currently valid certified evaluation")

        field_limit = int(ctx.config.get("field_limit", 2000))
        pass_threshold = float(ctx.config.get("pass_threshold", 1.0))
        collector = None

        def load_collector():
            nonlocal collector
            if collector is None:
                collector = _load_collect_cases(checkout)
            return collector

        rollout_dir = ctx.run_dir / "rollout"
        cases, jobs_dirs, artifact = replay_certified_cases(
            workspace=ctx.workspace,
            genid=ctx.parent,
            row=by_id[ctx.parent],
            load_collector=load_collector,
            destination=rollout_dir,
            field_limit=field_limit,
            pass_threshold=pass_threshold,
        )
        if not cases:
            raise SystemExit("evaluation replay produced no trial results")
        (rollout_dir / "cases.json").write_text(json.dumps(cases, indent=2, sort_keys=True) + "\n")

        store = rollout_dir / "candidates"
        prior = _prior_entries(ctx.workspace, ctx.run_dir)
        seed_tag = str(rows[0].get("tag") or "") if rows else ""
        entries: list[dict[str, Any]] = []
        replayed: list[str] = []
        for row in rows:
            genid = str(row.get("genid"))
            entry: dict[str, Any] = {
                "genid": genid,
                "parent": row.get("parent"),
                "score": row.get("score"),
                "status": row.get("status"),
                "verdict": row.get("verdict"),
                "reason": row.get("reason"),
                "certified": genid in certified,
            }
            handoff = ctx.workspace / "artifacts" / "generations" / genid / "handoff.md"
            if handoff.is_file():
                entry["handoff"] = _relative(ctx.workspace, handoff)
            if genid == ctx.parent:
                entry["cases"] = _relative(ctx.workspace, rollout_dir / "cases.json")
                entry["summary"] = case_summary(cases)
                entry["per_task"] = _per_task(cases)
                entry["artifact_sha256"] = artifact.sha256
            elif genid in certified and _reusable(prior.get(genid), row):
                for key in ("cases", "summary", "per_task", "artifact_sha256"):
                    if key in prior[genid]:
                        entry[key] = prior[genid][key]
            elif genid in certified:
                destination = store / f"gen-{genid}"
                try:
                    candidate_cases, _jobs, candidate_artifact = replay_certified_cases(
                        workspace=ctx.workspace,
                        genid=genid,
                        row=row,
                        load_collector=load_collector,
                        destination=destination,
                        field_limit=field_limit,
                        pass_threshold=pass_threshold,
                    )
                except SystemExit as error:
                    # Only the selected parent's replay is load-bearing; one unreadable historical
                    # candidate must not block every later generation.
                    entry["replay_error"] = str(error.code)
                else:
                    _write_json(destination / "cases.json", candidate_cases)
                    entry["cases"] = _relative(ctx.workspace, destination / "cases.json")
                    entry["summary"] = case_summary(candidate_cases) if candidate_cases else {}
                    entry["per_task"] = _per_task(candidate_cases)
                    entry["artifact_sha256"] = candidate_artifact.sha256
                    replayed.append(genid)
            tag = str(row.get("tag") or "") or None
            parent_tag = str(by_id.get(str(row.get("parent")), {}).get("tag") or "") or None
            for key, base in (("diff_vs_parent", parent_tag), ("diff_vs_seed", seed_tag or None)):
                if base == tag:
                    continue
                diff = _diff(ctx.workspace, base, tag)
                if diff is not None:
                    path = store / f"gen-{genid}" / f"{key.removeprefix('diff_')}.diff"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(diff)
                    entry[key] = _relative(ctx.workspace, path)
            entries.append(entry)

        frontier = _frontier(entries)
        _write_json(store / INDEX, {"schema_version": 1, "parent": ctx.parent, "candidates": entries})
        _write_json(store / "frontier.json", frontier)
        (store / "index.md").write_text(_render_index(entries, frontier))
        return RolloutResult(
            summary={
                "variant": "meta_harness",
                "source_parent": ctx.parent,
                **case_summary(cases),
                "jobs_dir": str(jobs_dirs[-1]),
                "jobs_dirs": [str(path) for path in jobs_dirs],
                "candidates_indexed": len(entries),
                "candidates_replayed": replayed,
            },
            artifacts=[
                "rollout/cases.json",
                "rollout/candidates/index.json",
                "rollout/candidates/index.md",
                "rollout/candidates/frontier.json",
                f"evaluation-artifacts:{artifact.path.relative_to(ctx.workspace)}",
            ],
        )


if __name__ == "__main__":
    sdk.main(MetaHarnessRollout, config_schema=CONFIG)
