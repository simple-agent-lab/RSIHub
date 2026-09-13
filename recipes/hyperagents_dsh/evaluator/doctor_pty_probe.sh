#!/bin/sh
# Model-free doctor smoke: prove non-TTY `docker exec` works with the same flag
# style as seeds/dsh/runners/compositions/rollout.base.cordis.yml (never `-t`).
# Cheap and read-only aside from a short-lived throwaway container — no Harbor trial.
set -eu

DOCKER_BIN="${DSH_DOCKER_BIN:-$(command -v docker || true)}"
[ -n "$DOCKER_BIN" ] && [ -x "$DOCKER_BIN" ] || {
  echo "doctor_pty_probe: docker not found (set DSH_DOCKER_BIN)" >&2
  exit 1
}

cid=
cleanup() {
  if [ -n "${cid:-}" ]; then
    "$DOCKER_BIN" rm -f "$cid" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

# Prefer a cached alpine; fall back to busybox if present to avoid a cold pull when possible.
image=alpine:3.20
if ! "$DOCKER_BIN" image inspect "$image" >/dev/null 2>&1; then
  if "$DOCKER_BIN" image inspect busybox:1.36 >/dev/null 2>&1; then
    image=busybox:1.36
  fi
fi

cid=$("$DOCKER_BIN" run -d --rm "$image" sleep 60)
# Match rollout cordis: `docker exec -i` without `-t` on headless hosts.
out=$(
  "$DOCKER_BIN" exec -i \
    -e PS1 \
    -e PROMPT_COMMAND \
    -e TERM \
    -e PAGER \
    -e GIT_PAGER \
    -w / \
    "$cid" \
    /bin/sh -c 'echo ok'
)

case "$out" in
  ok*)
    printf 'doctor_pty_probe: non-TTY docker exec ok\n'
    ;;
  *)
    printf 'doctor_pty_probe: unexpected output: %s\n' "$out" >&2
    exit 1
    ;;
esac
