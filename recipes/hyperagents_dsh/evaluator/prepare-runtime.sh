#!/bin/sh
# Host runtime hook. Engine contract: `sh prepare-runtime.sh <run_dir> <env_out>`
# under POSIX sh; KEY=VALUE lines written to $2 are injected into every trial
# as --ae (agent) and --ve (verifier). Diagnostics go to stderr.
set -eu

run_dir=${1:?run directory is required}
env_file=${2:?runtime environment output is required}

fail() { echo "prepare-runtime: $*" >&2; exit 1; }

# Node >= 22.19 (a dsh hard requirement; the dsh SDK spawns its Node runtime)
NODE_BIN="${DSH_NODE_BIN:-$(command -v node || true)}"
[ -x "$NODE_BIN" ] || fail "node not found (set DSH_NODE_BIN)"
NODE_VER="$("$NODE_BIN" --version | sed 's/^v//')"
case "$NODE_VER" in
  2[2-9].*|[3-9][0-9].*) : ;;
  *) fail "node $NODE_VER too old (need >= 22.19)" ;;
esac

# Optional restricted-network assets (uv/uvx binaries + portable python)
if [ -n "${DSH_ASSETS_DIR:-}" ]; then
  for f in uv uvx py313.tar.gz; do
    [ -f "$DSH_ASSETS_DIR/$f" ] || fail "asset missing: $DSH_ASSETS_DIR/$f"
  done
fi

# Runtime facts injected into every trial
{
  echo "DSH_NODE_BIN=$NODE_BIN"
  [ -n "${DSH_ASSETS_DIR:-}" ] && echo "DSH_ASSETS_DIR=$DSH_ASSETS_DIR"
  [ -n "${DSH_CONTAINER_APT_MIRROR:-}" ] && echo "DSH_CONTAINER_APT_MIRROR=$DSH_CONTAINER_APT_MIRROR"
  [ -n "${DSH_CONTAINER_PIP_INDEX:-}" ] && echo "DSH_CONTAINER_PIP_INDEX=$DSH_CONTAINER_PIP_INDEX"
  [ -n "${DSH_CONTAINER_PROXY:-}" ] && echo "DSH_CONTAINER_PROXY=$DSH_CONTAINER_PROXY"
  [ -n "${DSH_CONTAINER_NO_PROXY:-}" ] && echo "DSH_CONTAINER_NO_PROXY=$DSH_CONTAINER_NO_PROXY"
  true
} > "$env_file"
