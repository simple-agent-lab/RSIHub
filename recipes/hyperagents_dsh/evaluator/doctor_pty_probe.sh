#!/bin/sh
# Model-free doctor smoke: same docker argv as rollout.base.cordis.yml terminal-bash.
# Policy (docker resolve, exec -i without -t, bash -i, host-PTY readiness):
#   recipes/hyperagents_dsh/README.md § Canonical policy: docker CLI + terminal-bash argv
# Fail closed if `script` is missing — pipe-only exec is a false green.
# Cheap throwaway container; no Harbor trial.
set -eu

# Resolve docker like DshAgent / rollout_driver (DSH_DOCKER_BIN or PATH).
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

# Mirror @deepseek-ai/dsh-terminal-bash childEnvironment controlled prompt.
# Without interactive bash these never fire → agent readiness hang.
export PS1='dsh> '
# shellcheck disable=SC2016
export PROMPT_COMMAND='printf "\033]133;D;%s\007" "$?"; PS1='"'"'dsh> '"'"''
export TERM="${TERM:-dumb}"
export PAGER="${PAGER:-cat}"
export GIT_PAGER="${GIT_PAGER:-cat}"

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

# Bound hangs when interactive prompt readiness never arrives (agent analogue of
# PERSISTENT_BASH_TIMEOUT). macOS may lack GNU timeout — fall through then.
# Args must be real executables (timeout cannot invoke shell functions).
run_bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "${DOCTOR_PTY_TIMEOUT_SEC:-20}" "$@"
  else
    "$@"
  fi
}

# Cordis-aligned argv fragments (stdin open via -i, no -t, trailing bash -i).
cordis_docker_exec() {
  run_bounded "$DOCKER_BIN" exec -i \
    -e PS1 \
    -e PROMPT_COMMAND \
    -e TERM \
    -e PAGER \
    -e GIT_PAGER \
    -w / \
    "$cid" \
    /bin/bash --noprofile --norc -i
}

expect_ready() {
  label=$1
  # script(1) may emit CR; match markers anywhere. Avoid piping script -e into
  # head (SIGPIPE). Require both echo marker and prompt readiness.
  out=$(printf '%s\n' "$2" | tr -d '\r')
  case "$out" in
    *ok*)
      ;;
    *)
      printf 'doctor_pty_probe: %s missing echo marker (ok): %s\n' "$label" "$out" >&2
      exit 1
      ;;
  esac
  # Controlled prompt: printable PS1 and/or OSC 133;D; from PROMPT_COMMAND.
  case "$out" in
    *'dsh> '*|*'133;D;'*)
      printf 'doctor_pty_probe: %s ok\n' "$label"
      ;;
    *)
      printf 'doctor_pty_probe: %s missing prompt readiness (dsh> / OSC 133;D) — non-interactive bash would hang the agent: %s\n' "$label" "$out" >&2
      exit 1
      ;;
  esac
}

# 1) Pipe stdin with the real interactive bash argv (not /bin/sh -c). Merge
# stderr so PS1 prompt text is visible alongside PROMPT_COMMAND OSC on stdout.
pipe_out=$(printf 'echo ok\nexit\n' | cordis_docker_exec 2>&1) || {
  echo "doctor_pty_probe: non-TTY docker exec failed or timed out (interactive bash readiness hang?)" >&2
  exit 1
}
expect_ready "non-TTY docker exec" "$pipe_out"

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
pty_cmd='"$DOCTOR_PTY_PROBE_DOCKER" exec -i -e PS1 -e PROMPT_COMMAND -e TERM -e PAGER -e GIT_PAGER -w / "$DOCTOR_PTY_PROBE_CID" /bin/bash --noprofile --norc -i'

if script -q -e -c "true" /dev/null >/dev/null 2>&1; then
  # util-linux: script -q -e -c '…' /dev/null
  pty_out=$(printf 'echo ok\nexit\n' | run_bounded script -q -e -c "$pty_cmd" /dev/null 2>&1) || {
    echo "doctor_pty_probe: host-PTY docker exec failed or timed out (interactive bash readiness hang?)" >&2
    exit 1
  }
else
  # BSD/macOS: script -q /dev/null command…
  pty_out=$(printf 'echo ok\nexit\n' | run_bounded script -q /dev/null sh -c "$pty_cmd" 2>&1) || {
    echo "doctor_pty_probe: host-PTY docker exec failed or timed out (interactive bash readiness hang?)" >&2
    exit 1
  }
fi
expect_ready "host-PTY docker exec" "$pty_out"
