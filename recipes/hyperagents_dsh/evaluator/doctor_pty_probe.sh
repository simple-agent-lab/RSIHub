#!/bin/sh
# Model-free doctor smoke: prove the same non-TTY docker argv as
# seeds/dsh/runners/compositions/rollout.base.cordis.yml (terminal-bash).
#
# Cordis uses: docker exec -i … /bin/bash --noprofile --norc
# Never `-t` (container TTY) and never interactive bash `-i` — both break
# headless Harbor/node-pty ("PTY shell exited during startup").
#
# A pipe-only `/bin/sh -c` check is a false green relative to the agent, which
# attaches a host PTY (node-pty) to that docker argv. This probe matches the
# argv, then also spawns it under a host PTY via `script`. If `script` is
# unavailable, fail closed — do not treat pipe-exec alone as success.
# Cheap and read-only aside from a short-lived throwaway container — no Harbor trial.
set -eu

# Same resolution as DshAgent / rollout_driver: honor DSH_DOCKER_BIN when set,
# else PATH. Never fall back to a hard-coded /usr/bin/docker (Homebrew Mac).
if [ -n "${DSH_DOCKER_BIN:-}" ]; then
  DOCKER_BIN=$DSH_DOCKER_BIN
else
  DOCKER_BIN=$(command -v docker || true)
fi
[ -n "$DOCKER_BIN" ] && [ -x "$DOCKER_BIN" ] || {
  echo "doctor_pty_probe: docker not found or not executable (set DSH_DOCKER_BIN or install docker on PATH)" >&2
  echo "doctor_pty_probe: do not assume /usr/bin/docker — Homebrew Mac typically uses /opt/homebrew/bin/docker" >&2
  exit 1
}
printf 'doctor_pty_probe: using docker at %s\n' "$DOCKER_BIN"

cid=
cleanup() {
  if [ -n "${cid:-}" ]; then
    "$DOCKER_BIN" rm -f "$cid" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

# Prefer a cached image that ships /bin/bash (cordis argv). Avoid alpine/busybox:
# they lack bash and would false-fail or force a weaker /bin/sh -c path.
image=
for candidate in bash:5.2 bash:5 ubuntu:22.04 debian:bookworm-slim; do
  if "$DOCKER_BIN" image inspect "$candidate" >/dev/null 2>&1; then
    image=$candidate
    break
  fi
done
if [ -z "$image" ]; then
  image=bash:5.2
fi

cid=$("$DOCKER_BIN" run -d --rm "$image" sleep 60)

# Cordis-aligned exec: stdin open (`-i`), no container TTY (`-t`), non-interactive bash.
cordis_exec() {
  "$DOCKER_BIN" exec -i \
    -e PS1 \
    -e PROMPT_COMMAND \
    -e TERM \
    -e PAGER \
    -e GIT_PAGER \
    -w / \
    "$cid" \
    /bin/bash --noprofile --norc
}

expect_ok() {
  label=$1
  # script(1) may emit CR; match ok anywhere (do not require line-1 — host PTY
  # wrappers can prefix noise). Avoid piping script -e into head (SIGPIPE).
  out=$(printf '%s\n' "$2" | tr -d '\r')
  case "$out" in
    *ok*)
      printf 'doctor_pty_probe: %s ok\n' "$label"
      ;;
    *)
      printf 'doctor_pty_probe: %s unexpected output: %s\n' "$label" "$out" >&2
      exit 1
      ;;
  esac
}

# 1) Pipe stdin with the real bash argv (not /bin/sh -c).
pipe_out=$(printf 'echo ok\n' | cordis_exec)
expect_ok "non-TTY docker exec" "$pipe_out"

# 2) Agent-like host PTY: same argv under script(1). Fail closed if unavailable.
if ! command -v script >/dev/null 2>&1; then
  echo "doctor_pty_probe: 'script' not found; cannot verify host-PTY spawn (failing closed)" >&2
  echo "doctor_pty_probe: install util-linux (Linux) or use BSD script; pipe-only exec is not sufficient" >&2
  exit 1
fi

DOCTOR_PTY_PROBE_DOCKER="$DOCKER_BIN"
DOCTOR_PTY_PROBE_CID="$cid"
export DOCTOR_PTY_PROBE_DOCKER DOCTOR_PTY_PROBE_CID
# shellcheck disable=SC2016
pty_cmd='"$DOCTOR_PTY_PROBE_DOCKER" exec -i -e PS1 -e PROMPT_COMMAND -e TERM -e PAGER -e GIT_PAGER -w / "$DOCTOR_PTY_PROBE_CID" /bin/bash --noprofile --norc'

if script -q -e -c "true" /dev/null >/dev/null 2>&1; then
  # util-linux: script -q -e -c '…' /dev/null
  pty_out=$(printf 'echo ok\n' | script -q -e -c "$pty_cmd" /dev/null)
else
  # BSD/macOS: script -q /dev/null command…
  pty_out=$(printf 'echo ok\n' | script -q /dev/null sh -c "$pty_cmd")
fi
expect_ok "host-PTY docker exec" "$pty_out"
