#!/bin/sh
# Host runtime hook. Engine contract: `sh prepare-runtime.sh <run_dir> <env_out>`
# under POSIX sh; KEY=VALUE lines written to $2 are injected into every trial
# as --ae (agent) and --ve (verifier). Diagnostics go to stderr.
#
# Runtime carrier:
#   DSH_RUNTIME_MODE=exe  — bundled platform exe (no system Node required here;
#                           drivers still preflight the exe via runtime_mode.py)
#   DSH_RUNTIME_MODE=node — or unset: require Node >= 22.19 for the node carrier
# After an editable `uv add` of sdk-runtime, build the carrier with:
#   pnpm exec tsx scripts/build-exe-for-python-sdk.ts
set -eu

run_dir=${1:?run directory is required}
env_file=${2:?runtime environment output is required}

fail() { echo "prepare-runtime: $*" >&2; exit 1; }

MODE="${DSH_RUNTIME_MODE:-}"
NODE_BIN=""

require_node() {
  NODE_BIN="${DSH_NODE_BIN:-$(command -v node || true)}"
  [ -x "$NODE_BIN" ] || fail "node not found (set DSH_NODE_BIN); for exe mode set DSH_RUNTIME_MODE=exe after building the carrier"
  NODE_VER="$("$NODE_BIN" --version | sed 's/^v//')"
  NODE_MAJOR="${NODE_VER%%.*}"
  NODE_REST="${NODE_VER#*.}"
  NODE_MINOR="${NODE_REST%%.*}"
  case "$NODE_MAJOR" in
    ''|*[!0-9]*) fail "unparseable node version: $NODE_VER" ;;
  esac
  case "$NODE_MINOR" in
    ''|*[!0-9]*) NODE_MINOR=0 ;;
  esac
  if [ "$NODE_MAJOR" -lt 22 ] || { [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 19 ]; }; then
    fail "node $NODE_VER too old (need >= 22.19)"
  fi
}

case "$MODE" in
  exe)
    # Bundled exe path: Node is optional at prepare time. Drivers call
    # ensure_runtime_mode() and fail clearly if the exe carrier is missing.
    MODE=exe
    ;;
  node|"")
    require_node
    MODE=node
    ;;
  *)
    fail "unsupported DSH_RUNTIME_MODE='$MODE' (expected 'exe' or 'node')"
    ;;
esac

# Optional restricted-network assets (uv/uvx binaries + portable python)
if [ -n "${DSH_ASSETS_DIR:-}" ]; then
  for f in uv uvx py313.tar.gz; do
    [ -f "$DSH_ASSETS_DIR/$f" ] || fail "asset missing: $DSH_ASSETS_DIR/$f"
  done
fi

# Runtime facts injected into every trial
{
  echo "DSH_RUNTIME_MODE=$MODE"
  [ -n "$NODE_BIN" ] && echo "DSH_NODE_BIN=$NODE_BIN"
  [ -n "${DSH_ASSETS_DIR:-}" ] && echo "DSH_ASSETS_DIR=$DSH_ASSETS_DIR"
  [ -n "${DSH_CONTAINER_APT_MIRROR:-}" ] && echo "DSH_CONTAINER_APT_MIRROR=$DSH_CONTAINER_APT_MIRROR"
  [ -n "${DSH_CONTAINER_PIP_INDEX:-}" ] && echo "DSH_CONTAINER_PIP_INDEX=$DSH_CONTAINER_PIP_INDEX"
  [ -n "${DSH_CONTAINER_PROXY:-}" ] && echo "DSH_CONTAINER_PROXY=$DSH_CONTAINER_PROXY"
  [ -n "${DSH_CONTAINER_NO_PROXY:-}" ] && echo "DSH_CONTAINER_NO_PROXY=$DSH_CONTAINER_NO_PROXY"
  true
} > "$env_file"
