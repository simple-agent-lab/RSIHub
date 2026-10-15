# Meta-Harness

[Meta-Harness](https://arxiv.org/abs/2603.28052) (Lee et al., 2026) searches
over the harness, the code around a fixed base model, with a coding agent as
the proposer. Its defining choice is what the proposer sees: filesystem access
to every prior candidate's code, scores, and full execution traces, not a
summary of the latest attempt. This recipe reproduces that loop under RSIHub's
frozen evaluator; the reference implementation is
[stanford-iris-lab/meta-harness](https://github.com/stanford-iris-lab/meta-harness).

The MiniSWE target is pinned to commit
`388da74aad620a384ab47669b17c52133e30e7c3`, whose checked-in `uv.lock` is part
of the candidate runtime contract. Because upstream does not track that lock,
workspace initialization generates and freezes it explicitly.

`children_per_gen: 1` creates one candidate per round, matching the reference
example's one hypothesis per iteration.
`surface.include` exposes only `target/**`; the base model and evaluator stay fixed.
`select.operator: greedy` starts each proposal from the best certified candidate.
`rollout.operator: meta_harness` replays the selected parent's certified
evaluation and builds the candidate store under `rollout/candidates/`: an
index of every prior candidate with its parent, score, status, handoff notes,
and diffs against its parent and the seed, plus a per-task frontier. Each
certified candidate's evaluation is replayed once, in the first generation
after it is certified, so its traces stay readable without copying them every
generation.
`analyze.operator: trace_browser` exposes the selected parent's traces, metrics,
and history through the normalized feedback bundle.
`mutate.operator: meta_harness` runs a Harbor-hosted Codex CLI 0.146.0 proposer
with `xhigh` reasoning. Its prompt carries the reference proposer's rules: one
falsifiable mechanism per candidate, read failed and successful traces across
candidates, no task-specific hints, and a handoff note (`HYPOTHESIS`,
`CHANGES`, `PREDICTION`, `EVIDENCE`) that later proposers read from the index.
`gate.operator: parent_eligible` keeps every certified candidate in the population.
`evaluator` matches the HyperAgents and AHE MiniSWE recipes: all 30 curated
Terminal-Bench 2.0 tasks form one frozen optimization set, with one trial per
task per candidate.

Build the workspace image once before running:

```bash
IMAGE_CONTEXT="$(python -c 'from evolve.config import resource_root; print(resource_root("containers") / "mutate-codex")')"
docker build --build-arg CODEX_VERSION=0.146.0 \
  -t evolve-mutate-codex:20260818-codex0146 "$IMAGE_CONTEXT"
```

## Differences from the reference implementation

- The reference proposer is Claude Code with Opus 4.6. This recipe uses the
  same Codex proposer as the other RSIHub recipes so that results compare
  across methods. Switch `mutate.config.agent` and `model` to use another
  Harbor-supported proposer.
- The reference proposer copies any prior candidate as its starting file. Here
  the checkout is the best certified candidate, and the proposer ports ideas
  from other candidates through their diffs.
- The reference runs an import check and a one-task smoke test before the full
  evaluation. RSIHub's guardrail and canonical evaluation already reject
  candidates that fail to install or run.
- Traces exist only for certified candidates. A candidate whose evaluation was
  not certified is listed with its code and status but no traces.

## Operator Routing

`select: {operator: greedy, config: {}}` resolves to [`library/select/greedy.py`](../../library/select/greedy.py).
`rollout: {operator: meta_harness, config: {...}}` resolves to [`library/rollout/meta_harness.py`](../../library/rollout/meta_harness.py) and replays certified evidence through [`library/rollout/_support/certified_replay.py`](../../library/rollout/_support/certified_replay.py).
`analyze: {operator: trace_browser, config: {...}}` resolves to [`library/analyze/trace_browser.py`](../../library/analyze/trace_browser.py).
`mutate: {operator: meta_harness, config: {...}}` resolves to [`library/mutate/meta_harness.py`](../../library/mutate/meta_harness.py).
`gate: {operator: parent_eligible, config: {}}` resolves to [`library/gate/parent_eligible.py`](../../library/gate/parent_eligible.py).
`record: {operator: jsonl, config: {}}` resolves to [`library/record/jsonl.py`](../../library/record/jsonl.py).
