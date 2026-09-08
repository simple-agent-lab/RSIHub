#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RECIPE=${1:-}
CALLER=$PWD
ASSET_ROOT=${EVOLVE_ASSET_DIR:-$ROOT/.evolve-assets/terminal-bench-2.0}
[[ $ASSET_ROOT == /* ]] || ASSET_ROOT=$CALLER/$ASSET_ROOT
RAW_DATASET=$ASSET_ROOT/raw
DATASET=$ASSET_ROOT/terminal-bench-2-30-v1
READY_DATASET=$DATASET
[[ $RECIPE == *_tbench_full ]] && READY_DATASET=$RAW_DATASET/terminal-bench
RAW_PENDING=$ASSET_ROOT/.raw.pending
OWNS_PENDING=0

cleanup() {
  if [[ $OWNS_PENDING == 1 ]]; then
    rm -rf -- "$RAW_PENDING"
  fi
}
trap cleanup EXIT

case "$RECIPE" in
  aevolve|ahe|hyperagents|ahe_codex|gepa|hill_climb|hill_climb_codex|hyperagents_codex|hyperagents_tbench_full|hyperagents_codex_tbench_full) ;;
  *)
    [[ -f "$RECIPE" || -f "$RECIPE/evolve.yaml" ]] || {
      echo "unsupported recipe; supported recipes are Terminal-Bench profiles or a recipe YAML path" >&2; exit 2;
    }
    [[ $RECIPE == /* ]] || RECIPE=$CALLER/$RECIPE
    ;;
esac

for tool in uv git docker; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 2; }
done
GIT_VERSION=$(git --version)
GIT_VERSION=${GIT_VERSION#git version }
GIT_VERSION=${GIT_VERSION%% *}
IFS=. read -r GIT_MAJOR GIT_MINOR _ <<<"$GIT_VERSION"
if [[ ! $GIT_MAJOR =~ ^[0-9]+$ || ! $GIT_MINOR =~ ^[0-9]+$ ]] ||
  ((GIT_MAJOR < 2 || (GIT_MAJOR == 2 && GIT_MINOR < 25))); then
  echo "Git 2.25 or newer is required by Harbor (found $GIT_VERSION)" >&2
  exit 2
fi
docker info >/dev/null 2>&1 || { echo "Docker daemon is unavailable" >&2; exit 2; }

cd "$ROOT"
uv sync --frozen
RUNTIME=$(uv run --frozen python scripts/recipe_runtime.py "$RECIPE")
IFS=$'\t' read -r RUNTIME_KIND IMAGE_VERSION IMAGE <<<"$RUNTIME"
case "$RUNTIME_KIND" in
  codex)
    IMAGE_CONTEXT=$ROOT/containers/mutate-codex
    BUILD_ARGS=(--build-arg "CODEX_VERSION=$IMAGE_VERSION")
    ;;
  miniswe)
    IMAGE_CONTEXT=$ROOT/containers/mutate
    BUILD_ARGS=(--build-arg "MINISWE_VERSION=$IMAGE_VERSION")
    ;;
  *) echo "unsupported recipe runtime" >&2; exit 2 ;;
esac
mkdir -p "$ASSET_ROOT"
if [[ ! -d "$RAW_DATASET/terminal-bench" ]]; then
  [[ ! -e "$RAW_DATASET" ]] || { echo "incomplete raw dataset directory exists: $RAW_DATASET" >&2; exit 2; }
  [[ ! -e "$RAW_PENDING" ]] || { echo "incomplete setup directory exists: $RAW_PENDING" >&2; exit 2; }
  OWNS_PENDING=1
  uv run --frozen harbor download terminal-bench@2.0 --export -o "$RAW_PENDING"
  mv "$RAW_PENDING" "$RAW_DATASET"
  OWNS_PENDING=0
fi
uv run --frozen python scripts/examples/terminal_bench_smoke/prepare_dataset.py "$RAW_DATASET" "$DATASET"

# Always resolve the declared Dockerfile; Docker reuses matching build layers.
# A local tag or version label does not establish the expected base/toolchain.
docker build "${BUILD_ARGS[@]}" -t "$IMAGE" "$IMAGE_CONTEXT"

echo "Terminal-Bench 2.0 setup is ready at $READY_DATASET"
if [[ -e "$RECIPE" ]]; then
  echo "Use evolve init --recipe-path with your recipe directory and an explicit --dataset path."
else
  echo "EVOLVE_ASSET_DIR=\"$ASSET_ROOT\" ./scripts/run_recipe_demo.sh $RECIPE"
fi
