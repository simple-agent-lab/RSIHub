# HyperAgents on full Terminal-Bench 2.0

This recipe ports the historical full-benchmark HyperAgents experiment to the
current operator schema. It preserves the HyperAgents archive-and-child
lifecycle while evaluating every candidate on all 89 Terminal-Bench 2.0 tasks.

- `score_child_prop` selects one parent from the complete valid archive.
- The selected parent's certified evaluation is reused as mutation evidence.
- `target/**` and `operators/**` remain editable.
- Every installable child is evaluated immediately on the same 89 tasks.
- Generation 0 plus ten children produce 979 benchmark trials before retries.

This is a full-benchmark optimization curve, not a held-out generalization
result. It has no gate or sealed cohort and no final anchor. Use a partitioned
recipe such as GEPA when train-to-held-out transfer is the claim under test.

Prepare the official dataset and mutation image, then start with one generation:

```bash
./scripts/setup_terminal_bench.sh hyperagents_tbench_full
GENERATIONS=1 ./scripts/run_recipe_demo.sh hyperagents_tbench_full
```

The setup command downloads the official dataset once. The demo initializes
the workspace from the complete exported dataset rather than the curated
30-task subset.
