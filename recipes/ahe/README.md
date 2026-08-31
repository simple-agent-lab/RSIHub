# AHE on Terminal-Bench 2.0

This recipe keeps the AHE strategy independent from the target agent. It uses
the shared `terminal-bench-2-30-v1` dataset. Workspace initialization freezes
all 30 curated instances as one optimization set without synthesizing train,
gate, and sealed partitions. Each candidate is evaluated on those same 30 tasks
with one trial per task. That certified evaluation is replayed as the next AHE
debugger input, so its score
and debugger evidence come from the same retained Harbor trajectories rather
than a separate rollout run. Each task receives one required LLM debugger
analysis using the same model and runner as the mutate operator. The debugger uses
`high` reasoning and an explicit 64k output budget. The change-producing
mutation agent is Codex CLI 0.146.0 with `xhigh` reasoning, matching the
benchmark configuration used for the reported AHE runs. Failures
stop the generation after three attempts; there is no silent deterministic
fallback. This is configured as `debugger_max_retries: 2`: one initial
debugger call plus at most two retries.

The MiniSWE target is pinned to commit
`388da74aad620a384ab47669b17c52133e30e7c3`, whose checked-in `uv.lock` is part
of the candidate runtime contract. Because upstream does not track that lock,
workspace initialization generates and freezes it explicitly.

Canonical evaluation is deliberately different: the frozen
`CandidateMiniSweAgent` adapter installs the returned candidate source and invokes
its Python API with evaluator-owned model and resource limits. The prompt asks
for a change manifest linking target edits to debugger evidence and predicted
effects, but that manifest is best-effort metadata: a missing or malformed block
does not discard an otherwise surface-valid patch. The raw response, changed
paths, and patch are preserved and passed to the next mutate run; predicted-fix
and risk attribution is used only when available. The newest valid generation
remains the next parent even after a score regression, allowing the following
generation to attribute it and choose KEEP, REVISE, or ROLLBACK + PIVOT.

Generation 0 and generations 1 through 10 use the same frozen 30-task
optimization set. The gate operator decides parent eligibility from that
evaluation; it does not invoke a separate task partition. The evaluator is
frozen with capacity for 10 workers; set
`EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE=5` for a five-worker generation-1 smoke,
then omit the override for the full run.
Candidate execution uses Harbor's native task timeouts
(`agent_timeout_multiplier: 1`).

```bash
cd /path/to/RSIHub
evolve init /path/to/ahe-run --recipe ahe --tasks 3
cd /path/to/ahe-run
./evolve preflight .
./evolve preflight . --smoke
./evolve run . --max-generations 1
```

To let an outer coding agent perform the harness edit, keep the same workspace
and call the AHE capabilities individually:

```bash
./evolve operator run . select --genid 1
# Read runs/gen-1/parents.json, then set PARENT and fork its child worktree.
./evolve fork . "$PARENT" runs/worktrees/gen-1
./evolve operator run . rollout --genid 1 --parent "$PARENT" \
  --checkout runs/worktrees/gen-1
./evolve operator run . analyze --genid 1 --parent "$PARENT" \
  --checkout runs/worktrees/gen-1 \
  --config '{"max_tasks":5,"max_concurrent":3}'
```

The outer agent reads `runs/gen-1/analyze/`, edits the harness, and
finishes through `surface-check`, `commit`, `eval`, and `finalize`. Omit the
temporary limits for the canonical full analysis. The `mutate` stage is
optional on this path and remains the mutation stage for `evolve run`.

Live runs need Docker, Harbor, model credentials, and an immutable evaluator
runtime. Build the pinned Codex mutation image once before running:

```bash
IMAGE_CONTEXT="$(python -c 'from evolve.config import resource_root; print(resource_root("containers") / "mutate-codex")')"
docker build --build-arg CODEX_VERSION=0.146.0 \
  -t evolve-mutate-codex:20260818-codex0146 "$IMAGE_CONTEXT"
```

Codex is preinstalled in the image; the recipe does not require a host Codex
installation.
