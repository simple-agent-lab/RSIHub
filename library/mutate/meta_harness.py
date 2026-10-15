"""Meta-Harness proposer: one falsifiable harness change from the full candidate history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evolve.agent import AgentCommandError
from evolve.frozen import sdk
from evolve.frozen.config import string
from evolve.frozen.interfaces import MutateOperator, MutateResult
from evolve.patching import create_candidate_patch, load_surface_policy, patch_parent_ref
from library._shared.artifacts import render_artifact_guidance
from library._shared.runners import run_agent, runner_name
from library.mutate._config import WORKSPACE_CONFIG
from library.mutate._support.workspace import workspace_contract

CONFIG = WORKSPACE_CONFIG.extend(
    {
        "prompt_path": string(),
        "skills_dir": string(),
        "memory_dir": string(),
    }
)


PROMPT = """# Meta-Harness Proposer

You are the proposer in a Meta-Harness search over the agent harness: the code around a fixed
base model that decides what to store, retrieve, and show while the model works. You do not run
benchmarks. The framework evaluates your candidate after you finish.

## Constraints

- Produce exactly one new candidate in this checkout every iteration. Do not conclude that the
  current harness is optimal or stop early.
- One mechanism per candidate. Each candidate tests one falsifiable hypothesis; a second idea is a
  second candidate.
- Mechanism first. Start from a failure mode or success pattern you found in the traces, never
  from a speculative change.
- No task-specific hints. Never mention task names in code, prompts, or comments, and do not
  hardcode knowledge about individual tasks. General guidance is fine when it would help a
  developer on many unfamiliar tasks; if in doubt, make it more general.
- The checkout is the selected parent. You may port code from any prior candidate by reading its
  diff in the candidate store; do not import from other candidates at runtime.
- Do not modify the fixed evaluator, mechanism, workspace configuration, archive, task partitions,
  credentials, endpoints, or resource limits.
"""


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _safe_usage(usage: object) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {"usd": 0}
    normalized = dict(usage)
    usd = normalized.get("usd", 0)
    normalized["usd"] = usd if isinstance(usd, (int, float)) and not isinstance(usd, bool) else 0
    return normalized


def build_prompt(checkout: Path, observation: str, ctx) -> str:
    del observation
    if runner_name(ctx) == "harbor":
        repository = Path("/app/task/workspace")
        current_run = repository / "runs" / f"gen-{ctx.genid}"
        experiment = repository
    else:
        repository = checkout
        current_run = ctx.run_dir
        experiment = ctx.workspace
    store = current_run / "rollout" / "candidates"
    handoff = experiment / "artifacts" / "generations" / str(ctx.genid) / "handoff.md"
    return (
        f"{PROMPT.rstrip()}\n\n"
        f"{workspace_contract(checkout, ctx.config)}\n\n"
        "# Candidate store\n\n"
        f"- Index: `{store / 'index.md'}` and `{store / 'index.json'}` list every prior candidate with its "
        "parent, score, status, replayed traces, diffs, and handoff notes. Their paths are relative to "
        f"`{experiment}`.\n"
        f"- Per-task frontier: `{store / 'frontier.json'}` names the best candidate on each task.\n"
        f"- Selected parent traces: `{current_run / 'rollout' / 'cases.json'}` and "
        f"`{current_run / 'analyze' / 'evidence'}`.\n\n"
        "# Workflow\n\n"
        "1. Read the index and frontier: current scores, what has been tried, and which hypotheses held.\n"
        "2. Deep-read failed AND successful traces across several candidates, not only the parent. "
        "Compare candidates that differ on the same task to see which code change caused the difference.\n"
        "3. Read the parent harness code and the diffs of candidates whose ideas you may reuse.\n"
        "4. State one falsifiable hypothesis, then implement only the change it needs in this checkout.\n"
        f"5. Write `{handoff}` with these sections: `HYPOTHESIS` (the falsifiable claim), `CHANGES` "
        "(what you changed), `PREDICTION` (expected pass-rate change and token/turn impact), and "
        "`EVIDENCE` (the traces that motivated it). Later proposers read it from the index.\n\n"
        f"Repository: {repository}\n"
        f"Prior generation artifacts: {experiment / 'runs'}\n"
        f"Current generation artifacts: {current_run}\n"
        f"\n{render_artifact_guidance(ctx, experiment)}\n\n"
        "Edit the checkout directly. Do not print a patch instead of editing files.\n"
    )


class MetaHarnessMutate(MutateOperator):
    def mutate(self, checkout: Path, observation: str, ctx) -> MutateResult:
        parent_ref = patch_parent_ref(checkout, ctx)
        prompt = build_prompt(checkout, observation, ctx)
        out = ctx.run_dir / "mutate"
        out.mkdir(parents=True, exist_ok=True)
        (out / "prompt.md").write_text(prompt)
        try:
            agent_run = run_agent(checkout, prompt, ctx)
        except AgentCommandError as exc:
            (out / "output.txt").write_text(exc.output)
            _write_json(out / "usage.json", _safe_usage(exc.usage))
            raise SystemExit(exc.returncode)

        patch = create_candidate_patch(
            checkout=checkout,
            parent_ref=parent_ref,
            surface=load_surface_policy(checkout),
            repair=False,
        )
        usage = _safe_usage(agent_run.usage)
        notes = [
            "operator: meta_harness",
            f"runner: {runner_name(ctx)}",
            *patch.notes,
        ]

        (out / "model_patch.diff").write_text(patch.diff)
        (out / "patch.diff").write_text(patch.diff)
        (out / "output.txt").write_text(agent_run.output)
        (out / "rationale.md").write_text("\n".join(notes) + "\n")
        _write_json(out / "changed.json", patch.changed_paths)
        _write_json(out / "surface-check.json", patch.surface_report)
        _write_json(out / "usage.json", usage)
        return MutateResult(
            changed=patch.changed_paths,
            notes=notes,
            usage=usage,
        )


if __name__ == "__main__":
    sdk.main(MetaHarnessMutate, config_schema=CONFIG)
