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
  hyperagents_tbench_full)
    IMAGE=evolve-mutate-app:20260724-tools-mswe245
    IMAGE_CONTEXT=$ROOT/containers/mutate
    IMAGE_LABEL=io.evolve.miniswe.version
    IMAGE_VERSION=2.4.5
    BUILD_ARGS=(--build-arg MINISWE_VERSION=2.4.5)
    ;;
  aevolve|ahe|hyperagents|ahe_codex|gepa|hill_climb|hill_climb_codex|hyperagents_codex|hyperagents_codex_tbench_full)
    IMAGE=evolve-mutate-codex:20260904-codex0149
    IMAGE_CONTEXT=$ROOT/containers/mutate-codex
    IMAGE_LABEL=io.evolve.codex.version
    IMAGE_VERSION=0.149.0
    BUILD_ARGS=(--build-arg CODEX_VERSION=0.149.0)
    REUSABLE_CODEX_BASE=evolve-mutate-codex:20260818-codex0146
    ;;
  *)
    echo "unsupported recipe '$RECIPE'; supported recipes: aevolve, ahe, ahe_codex, gepa, hill_climb, hill_climb_codex, hyperagents, hyperagents_codex, hyperagents_tbench_full, hyperagents_codex_tbench_full" >&2
    exit 2
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

if [[ ${REUSABLE_CODEX_BASE:-} ]]; then
  REUSABLE_CODEX_VERSION=$(
    docker image inspect --format "{{ index .Config.Labels \"$IMAGE_LABEL\" }}" "$REUSABLE_CODEX_BASE" 2>/dev/null || true
  )
  if [[ $REUSABLE_CODEX_VERSION == 0.146.0 ]]; then
    BUILD_ARGS+=(--build-arg "RUNTIME_BASE=$REUSABLE_CODEX_BASE" --build-arg INSTALL_TOOLS=0)
  fi
fi

cd "$ROOT"
uv sync --frozen
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

INSTALLED_VERSION=$(docker image inspect --format "{{ index .Config.Labels \"$IMAGE_LABEL\" }}" "$IMAGE" 2>/dev/null || true)
if [[ $INSTALLED_VERSION != "$IMAGE_VERSION" ]]; then
  docker build "${BUILD_ARGS[@]}" -t "$IMAGE" "$IMAGE_CONTEXT"
fi

echo "Terminal-Bench 2.0 setup is ready at $READY_DATASET"
echo "EVOLVE_ASSET_DIR=\"$ASSET_ROOT\" ./scripts/run_recipe_demo.sh $RECIPE"
