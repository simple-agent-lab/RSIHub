# HyperAgents for Codex on full Terminal-Bench 2.0

This recipe applies a held-out HyperAgents optimization lifecycle to the
built-in Codex target over all 89 Terminal-Bench 2.0 tasks.

The mutable surface is `target/**` plus `operators/**`. HyperAgents may change
the main prompt, task skills, the complete Codex plugin under
`target/plugins/**` (manifest, hooks, context, and plugin-owned skills), the
plugin marketplace declaration under `target/.agents/**`, and the active
operator implementations. The evaluator and benchmark remain frozen.

- The frozen split contains 50 train, 19 gate, and 20 sealed tasks.
- `score_child_prop` selects parents using their canonical 19-task gate scores.
- The selected parent runs all 50 train tasks to produce mutation evidence.
- Gate and sealed task identities and results are unavailable to the mutator.
- Every installable child is evaluated immediately on the 19-task gate.
- The 20-task sealed cohort is used only for baseline and final anchors.

The archive remains open-ended: a lower-scoring child may remain eligible as a
future stepping stone, while its lower gate score reduces its selection weight.
The sealed result supports a held-out claim only when the final anchor is
complete and was not used to steer the run.

Prepare the official dataset and pinned Codex mutation image, then start with
one generation:

```bash
./scripts/setup_terminal_bench.sh hyperagents_codex_tbench_full
GENERATIONS=1 ./scripts/run_recipe_demo.sh hyperagents_codex_tbench_full
```
