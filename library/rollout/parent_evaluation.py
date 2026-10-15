"""Expose a selected parent's sanitized, certified evaluation evidence."""

from __future__ import annotations

import json
from pathlib import Path

from evolve.frozen import sdk
from evolve.frozen.config import Config, integer, number
from evolve.frozen.interfaces import ArchiveView, OperatorContext, RolloutOperator, RolloutResult
from library.rollout._support.certified_replay import (  # noqa: F401 - re-exported for vendored callers
    _certify_result_paths,
    _load_collect_cases,
    case_summary,
    replay_certified_cases,
)

CONFIG = Config(
    {
        "field_limit": integer(default=2000, minimum=1),
        "pass_threshold": number(default=1.0),
    }
)


class ParentEvaluationRollout(RolloutOperator):
    def rollout(self, checkout: Path, ctx: OperatorContext) -> RolloutResult:
        if ctx.parent is None:
            raise SystemExit("evaluation replay requires a selected parent")
        archive = ArchiveView(ctx.workspace)
        row = archive.row(ctx.parent)
        if row is None:
            raise SystemExit(f"selected parent {ctx.parent} is missing from archive")
        valid_parents = {str(candidate.get("genid")) for candidate in archive.valid_parents()}
        if ctx.parent not in valid_parents:
            raise SystemExit(f"selected parent {ctx.parent} has no currently valid certified evaluation")
        rollout_dir = ctx.run_dir / "rollout"
        cases, jobs_dirs, artifact = replay_certified_cases(
            workspace=ctx.workspace,
            genid=ctx.parent,
            row=row,
            load_collector=lambda: _load_collect_cases(checkout),
            destination=rollout_dir,
            field_limit=int(ctx.config.get("field_limit", 2000)),
            pass_threshold=float(ctx.config.get("pass_threshold", 1.0)),
        )
        if not cases:
            raise SystemExit("evaluation replay produced no trial results")

        (rollout_dir / "cases.json").write_text(json.dumps(cases, indent=2, sort_keys=True) + "\n")
        return RolloutResult(
            summary={
                "variant": "parent_evaluation",
                "source_parent": ctx.parent,
                **case_summary(cases),
                "jobs_dir": str(jobs_dirs[-1]),
                "jobs_dirs": [str(path) for path in jobs_dirs],
            },
            artifacts=[
                "rollout/cases.json",
                f"evaluation-artifacts:{artifact.path.relative_to(ctx.workspace)}",
            ],
        )


# Compatibility for externally vendored operator code; built-in recipes use the clear name.
EvaluationReplayRollout = ParentEvaluationRollout


if __name__ == "__main__":
    sdk.main(ParentEvaluationRollout, config_schema=CONFIG)
