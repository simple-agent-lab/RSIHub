# HyperAgents

HyperAgents-style search evolves more than the target agent. The mutable
surface includes the agent and all operators, so improvements
can come from behavior, process, or memory. The population stays small but
branchy, and parent choice is randomized to explore different process variants
instead of always following the current best score.

The MiniSWE target is pinned to commit
`388da74aad620a384ab47669b17c52133e30e7c3`, whose checked-in `uv.lock` is part
of the candidate runtime contract. Because upstream does not track that lock,
workspace initialization generates and freezes it explicitly.

`children_per_gen: 1` creates one candidate per round.
`surface.include` exposes `target/**` plus `operators/**`.
`select.operator: score_child_prop` balances score with child-proposal behavior.
`rollout.operator: parent_evaluation` exposes the selected parent's sanitized, certified gate evaluation without launching another task run.
`analyze.operator: trace_browser` exposes current traces, metrics, and history through the normalized feedback bundle.
`mutate.operator: hyperagents` consumes that bundle through a Harbor-hosted
Codex CLI 0.146.0 agent while retaining self-referential editing. The mutation
agent uses `xhigh` reasoning, matching the benchmark configuration used for
the reported HyperAgents runs.
`gate.operator: parent_eligible` admits evaluated process candidates.
`evaluator.engine: harbor` runs the canonical black-box benchmark.
`task_scope: full` freezes all 30 curated task identities as one shared
optimization set when the workspace is initialized from the project root.
Each candidate receives one trial on every task.

The selected parent's retained optimization evaluation is available before
the child is produced, and every installable child is then immediately
evaluated on the same frozen optimization set. Generation 0 and generations 1
through 10 therefore form a 30-task optimization curve. The gate operator
decides parent eligibility from that evaluation rather than invoking a separate
task partition. The evaluator is frozen with capacity for 10 workers; set
`EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE=5` for a five-worker generation-1 smoke,
then omit the override for the full run.
Candidate execution uses Harbor's native task timeouts
(`agent_timeout_multiplier: 1`).

Build the workspace image once before running:

```bash
IMAGE_CONTEXT="$(python -c 'from evolve.config import resource_root; print(resource_root("containers") / "mutate-codex")')"
docker build --build-arg CODEX_VERSION=0.146.0 \
  -t evolve-mutate-codex:20260818-codex0146 "$IMAGE_CONTEXT"
```

## Operator Routing

`select: {operator: score_child_prop, config: {}}` resolves to [`library/select/score_child_prop.py`](../../library/select/score_child_prop.py).
`rollout: {operator: parent_evaluation, config: {}}` resolves to [`library/rollout/parent_evaluation.py`](../../library/rollout/parent_evaluation.py) and uses the normalized collector from [`library/_shared/harbor/`](../../library/_shared/harbor/).
`analyze: {operator: trace_browser, config: {}}` resolves to [`library/analyze/trace_browser.py`](../../library/analyze/trace_browser.py).
`mutate: {operator: hyperagents, config: {...}}` resolves to [`library/mutate/hyperagents.py`](../../library/mutate/hyperagents.py).
`validate: {operator: hyperagents, config: {}}` resolves to [`library/validate/hyperagents.py`](../../library/validate/hyperagents.py).
`gate: {operator: parent_eligible, config: {}}` resolves to [`library/gate/parent_eligible.py`](../../library/gate/parent_eligible.py).
`record: {operator: hyperagents, config: {}}` resolves to [`library/record/hyperagents.py`](../../library/record/hyperagents.py).

Operator changes use natural stage semantics: they become active the next time
the changed operator is invoked. The prompt requires every proposal to include
a substantive target change; canonical evaluation remains frozen.
