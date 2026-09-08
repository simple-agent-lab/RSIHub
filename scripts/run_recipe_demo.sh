#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CALLER=$PWD
RECIPE=${1:-${RECIPE:-ahe}}
WORKSPACE=${WORKSPACE:-$ROOT/runs/${RECIPE}-demo}
TASKS=${TASKS:-}
GENERATIONS=${GENERATIONS:-1}
ENV_FILE=${ENV_FILE:-$ROOT/.env}
ASSET_ROOT=${EVOLVE_ASSET_DIR:-$ROOT/.evolve-assets/terminal-bench-2.0}
[[ $WORKSPACE == /* ]] || WORKSPACE=$CALLER/$WORKSPACE
[[ $ENV_FILE == /* ]] || ENV_FILE=$CALLER/$ENV_FILE
[[ $ASSET_ROOT == /* ]] || ASSET_ROOT=$CALLER/$ASSET_ROOT
DATASET=$ASSET_ROOT/terminal-bench-2-30-v1
[[ $RECIPE == hyperagents_tbench_full || $RECIPE == hyperagents_codex_tbench_full ]] && DATASET=$ASSET_ROOT/raw/terminal-bench
SUPPORTED=" aevolve ahe ahe_codex gepa hill_climb hill_climb_codex hyperagents hyperagents_codex hyperagents_tbench_full hyperagents_codex_tbench_full "
[[ $SUPPORTED == *" $RECIPE "* ]] || { echo "unsupported recipe '$RECIPE'; supported recipes:$SUPPORTED" >&2; exit 2; }
[[ -d "$ASSET_ROOT/raw/terminal-bench" && ( $RECIPE == *_tbench_full || -f "$DATASET/dataset-source.json" ) ]] || {
  echo "Terminal-Bench assets are missing; run ./scripts/setup_terminal_bench.sh $RECIPE" >&2; exit 2;
}

UV_RUN=(uv run --frozen)
[[ ! -f "$ENV_FILE" ]] || UV_RUN+=(--env-file "$ENV_FILE")
INIT_ARGS=(--recipe "$RECIPE" --dataset "$DATASET")
[[ -z "$TASKS" ]] || INIT_ARGS+=(--tasks "$TASKS")

cd "$ROOT"
uv sync --frozen
[[ $RECIPE == *_tbench_full ]] || "${UV_RUN[@]}" python scripts/examples/terminal_bench_smoke/prepare_dataset.py "$ASSET_ROOT/raw" "$DATASET"
"${UV_RUN[@]}" evolve init "$WORKSPACE" "${INIT_ARGS[@]}"
"${UV_RUN[@]}" "$WORKSPACE/evolve" preflight "$WORKSPACE"
"${UV_RUN[@]}" "$WORKSPACE/evolve" run "$WORKSPACE" --max-generations "$GENERATIONS" --children-per-gen 1
"${UV_RUN[@]}" "$WORKSPACE/evolve" status "$WORKSPACE"
"${UV_RUN[@]}" "$WORKSPACE/evolve" verify "$WORKSPACE"
