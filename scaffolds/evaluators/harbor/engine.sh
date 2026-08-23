# harbor evaluator template
. evaluator/eval.env
if [ "${EVOLVE_EXECUTION_BACKEND:-}" = "local" ]; then
  case "${EVOLVE_HARBOR_ENVIRONMENT:-}" in
    *:LocalEnvironment) ;;
    *)
      printf 'local execution runtime requires Harbor LocalEnvironment; refusing Docker fallback\n' >&2
      printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
      exit 3
      ;;
  esac
fi
if [ -n "${EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE:-}" ]; then
  case "$EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE" in
    *[!0-9]*|""|0)
      printf 'invalid EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE=%s\n' \
        "$EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE" >&2
      exit 3
      ;;
  esac
  EVOLVE_HARBOR_N_CONCURRENT=$EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE
fi
if [ "${EVOLVE_CANDIDATE_SMOKE_MODE:-}" = "single" ]; then
  EVOLVE_TASK_LIMIT=1
fi
if [ -n "${EVOLVE_TASK_LIMIT:-}" ]; then
  case "$EVOLVE_TASK_LIMIT" in
    *[!0-9]*|""|0)
      printf 'invalid EVOLVE_TASK_LIMIT=%s\n' "$EVOLVE_TASK_LIMIT" >&2
      exit 3
      ;;
  esac
fi
: "${EVOLVE_WORKSPACE:=$PWD}"
: "${EVOLVE_FRAMEWORK_PYTHON:=$(command -v python3)}"
if [ -n "${EVOLVE_UV_BINARY:-}" ]; then UV=$EVOLVE_UV_BINARY; else UV=$(command -v uv || true); fi
[ -n "$UV" ] && [ -x "$UV" ] || { printf 'uv is required; install uv or set EVOLVE_UV_BINARY\n' >&2; printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"; exit 3; }
# Keep candidate-runtime preparation and the Harbor adapter on the same host uv.
# This is deliberately not forwarded with --ae: the task image's uv and PATH
# remain part of the benchmark environment.
EVOLVE_UV_BINARY=$UV
export EVOLVE_UV_BINARY
if [ "${EVOLVE_HARBOR_ENVIRONMENT:-docker}" = "docker" ] && [ -z "${DOCKER_HOST:-}" ]; then
  resolved_docker_host=$(
    "$EVOLVE_FRAMEWORK_PYTHON" -m evolve.execution_runtime.command docker-host 2>/dev/null || true
  )
  if [ -n "$resolved_docker_host" ]; then
    DOCKER_HOST=$resolved_docker_host
    export DOCKER_HOST
  fi
fi
if [ -z "${EVOLVE_GENID:-}" ]; then
  EVOLVE_GENID=$(basename "$(dirname "$EVOLVE_RUN_DIR")")
  EVOLVE_GENID=${EVOLVE_GENID#gen-}
fi
export EVOLVE_GENID
: "${EVOLVE_ATTEMPT_ID:=manual-$EVOLVE_GENID}"
export EVOLVE_ATTEMPT_ID EVOLVE_FRAMEWORK_PYTHON
dataset_snapshot=
cleanup_dataset_snapshot() {
  [ -n "$dataset_snapshot" ] || return 0
  "$EVOLVE_FRAMEWORK_PYTHON" - "$dataset_snapshot" <<'PY' || :
import shutil
import sys
from pathlib import Path

snapshot = Path(sys.argv[1])
if snapshot.name != "task-dataset":
    raise SystemExit("refusing to remove an unexpected task snapshot path")
for path in (snapshot, snapshot.with_name(".task-dataset.pending")):
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
PY
  dataset_snapshot=
}
trap cleanup_dataset_snapshot EXIT
split_name=${EVOLVE_EVAL_SPLIT:-gate}
if python3 -c 'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1])).get("resolved") else 1)' evaluator/splits.json; then
  dataset_snapshot="$EVOLVE_RUN_DIR/task-dataset"
  set -- select evaluator/splits.json "$EVOLVE_HARBOR_TASKS" "$split_name" "$EVOLVE_RUN_DIR"
  if [ -n "${EVOLVE_TASK_LIMIT:-}" ]; then set -- "$@" --limit "$EVOLVE_TASK_LIMIT"; fi
  if ! "$UV" run --project "$EVOLVE_WORKSPACE" --frozen \
    --python "$EVOLVE_FRAMEWORK_PYTHON" python "$PWD/.evolve/launch_splits.py" \
    "$@"; then
    printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
    exit 3
  fi
  if [ ! -d "$dataset_snapshot" ]; then
    printf 'verified evaluator task snapshot is missing: %s\n' "$dataset_snapshot" >&2
    printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
    exit 3
  fi
  EVOLVE_HARBOR_TASKS=$dataset_snapshot
  EVOLVE_HARBOR_DATASET_MODE=path
  EVOLVE_HARBOR_TASK_FILE="$EVOLVE_RUN_DIR/task-names.txt"
  export EVOLVE_HARBOR_TASKS EVOLVE_HARBOR_DATASET_MODE EVOLVE_HARBOR_TASK_FILE
fi
if [ -n "${EVOLVE_RUN_PLAN:-}" ]; then
  if ! "$EVOLVE_FRAMEWORK_PYTHON" - "$EVOLVE_RUN_PLAN" "$EVOLVE_RUN_DIR/run-plan-tasks.txt" <<'PY'
import json
import sys
from glob import escape
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
tasks = payload.get("tasks") if isinstance(payload, dict) else None
expected = payload.get("expected_trials") if isinstance(payload, dict) else None
if not isinstance(tasks, list) or any(not isinstance(task, str) or not task for task in tasks):
    raise SystemExit("evaluation run plan has invalid tasks")
if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
    raise SystemExit("evaluation run plan has invalid expected_trials")
Path(sys.argv[2]).write_text("".join(f"{escape(task)}\n" for task in tasks))
PY
  then
    printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
    exit 3
  fi
  EVOLVE_HARBOR_TASK_FILE="$EVOLVE_RUN_DIR/run-plan-tasks.txt"
  export EVOLVE_HARBOR_TASK_FILE
fi
: "${EVOLVE_UV_CACHE_DIR:=$HOME/.evolve/uv-cache}"
runtime_mounts=${EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON:-}
runtime_env=${EVOLVE_CANDIDATE_RUNTIME_ENV_JSON:-}
evaluator_runtime_env=
if [ -f evaluator/prepare-runtime.sh ]; then
  evaluator_runtime_env="$EVOLVE_RUN_DIR/evaluator-runtime.env"
  if ! EVOLVE_HARBOR_ENVIRONMENT="${EVOLVE_HARBOR_ENVIRONMENT:-}" \
    EVOLVE_WORKSPACE="$EVOLVE_WORKSPACE" \
    sh evaluator/prepare-runtime.sh "$EVOLVE_RUN_DIR" "$evaluator_runtime_env"; then
    printf 'evaluator runtime preparation failed\n' >&2
    printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
    exit 3
  fi
  if [ ! -f "$evaluator_runtime_env" ]; then
    printf 'evaluator runtime preparation did not write %s\n' "$evaluator_runtime_env" >&2
    printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
    exit 3
  fi
fi
if [ -z "$runtime_mounts" ]; then
  mkdir -p "$EVOLVE_UV_CACHE_DIR"
  runtime_mounts=$(python3 -c 'import json,sys; print(json.dumps([{"type":"bind","source":sys.argv[1],"target":"/opt/evolve/uv/cache"}]))' "$EVOLVE_UV_CACHE_DIR")
fi
[ -n "$runtime_env" ] || runtime_env='{}'
if ! python3 - "$runtime_env" "$runtime_mounts" "$EVOLVE_RUN_DIR" <<'PY'
import json
import sys
from pathlib import Path

environment = json.loads(sys.argv[1])
mounts = json.loads(sys.argv[2])
if not isinstance(environment, dict):
    raise SystemExit("candidate runtime environment must be an object")
if not isinstance(mounts, list) or any(not isinstance(mount, dict) for mount in mounts):
    raise SystemExit("candidate runtime mounts must be a list of objects")
for mount in mounts:
    if (
        mount.get("type") != "bind"
        or not isinstance(mount.get("source"), str)
        or not isinstance(mount.get("target"), str)
        or not isinstance(mount.get("read_only", False), bool)
    ):
        raise SystemExit("invalid candidate runtime mount")
entries = []
for key, value in sorted(environment.items()):
    if not isinstance(key, str) or not isinstance(value, str) or "\n" in key + value or "=" in key:
        raise SystemExit("invalid candidate runtime environment entry")
    entries.append(f"{key}={value}")
run_dir = Path(sys.argv[3])
(run_dir / "candidate-runtime.env").write_text("\n".join(entries) + ("\n" if entries else ""))
(run_dir / "candidate-runtime.mounts.json").write_text(json.dumps(mounts, separators=(",", ":")))
PY
then
  printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
  exit 3
fi
runtime_mounts=$(cat "$EVOLVE_RUN_DIR/candidate-runtime.mounts.json")
jobs_dir="$EVOLVE_RUN_DIR/jobs"
if ! mkdir "$jobs_dir"; then
  printf 'jobs directory already exists: %s\n' "$jobs_dir" >&2
  printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
  exit 3
fi
if [ "${EVOLVE_CANDIDATE_SMOKE_MODE:-}" = "single" ]; then
  EVOLVE_HARBOR_N=1
  EVOLVE_HARBOR_ATTEMPTS=1
  EVOLVE_HARBOR_N_CONCURRENT=1
  EVOLVE_TASK_LIMIT=1
elif [ "${EVOLVE_CANDIDATE_SMOKE_MODE:-}" = "full" ]; then
  EVOLVE_HARBOR_ATTEMPTS=1
fi
cleanup_harbor() {
  case "${EVOLVE_HARBOR_ENVIRONMENT:-docker}" in
    docker) "$EVOLVE_FRAMEWORK_PYTHON" evaluator/cleanup_harbor.py "$jobs_dir" || : ;;
  esac
}
cleanup_on_exit() {
  cleanup_rc=$?
  trap - EXIT TERM INT
  cleanup_harbor
  cleanup_dataset_snapshot
  exit "$cleanup_rc"
}
cleanup_on_signal() {
  cleanup_signal=$1
  trap - EXIT TERM INT
  cleanup_harbor
  cleanup_dataset_snapshot
  if [ "$cleanup_signal" = TERM ]; then
    exit 143
  fi
  exit 130
}
trap cleanup_on_exit EXIT
trap 'cleanup_on_signal TERM' TERM
trap 'cleanup_on_signal INT' INT
harbor_rc=0
set -- run
case "${EVOLVE_HARBOR_DATASET_MODE:-path}" in
  registry|dataset)
    set -- "$@" --dataset "$EVOLVE_HARBOR_TASKS"
    ;;
  path|"")
    set -- "$@" -p "$EVOLVE_HARBOR_TASKS"
    ;;
  *)
    printf 'unknown EVOLVE_HARBOR_DATASET_MODE=%s\n' "$EVOLVE_HARBOR_DATASET_MODE" > "$EVOLVE_RUN_DIR/harbor.log"
    printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
    exit 3
    ;;
esac
if [ "${EVOLVE_EVAL_KIND:-research}" = "anchor" ] && [ -n "${EVOLVE_HARBOR_ANCHOR_TASK_FILE:-}" ]; then
  EVOLVE_HARBOR_TASK_FILE=$EVOLVE_HARBOR_ANCHOR_TASK_FILE
fi
if [ -n "${EVOLVE_TASK_LIMIT:-}" ] && [ -n "${EVOLVE_HARBOR_TASK_FILE:-}" ] \
  && [ "$EVOLVE_HARBOR_TASK_FILE" != "$EVOLVE_RUN_DIR/task-names.txt" ]; then
  if ! "$UV" run --project "$EVOLVE_WORKSPACE" --frozen \
    --python "$EVOLVE_FRAMEWORK_PYTHON" python "$PWD/.evolve/launch_splits.py" \
    limit-file "$EVOLVE_HARBOR_TASK_FILE" "$EVOLVE_RUN_DIR" --limit "$EVOLVE_TASK_LIMIT"; then
    printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
    exit 3
  fi
  EVOLVE_HARBOR_TASK_FILE="$EVOLVE_RUN_DIR/task-names.txt"
  export EVOLVE_HARBOR_TASK_FILE
fi
if [ -n "${EVOLVE_HARBOR_TASK_FILE:-}" ]; then
  while IFS= read -r task_name || [ -n "$task_name" ]; do
    case "$task_name" in
      ""|\#*) continue ;;
    esac
    set -- "$@" --include-task-name "$task_name"
  done < "$EVOLVE_HARBOR_TASK_FILE"
fi
if [ -n "${EVOLVE_TASK_LIMIT:-}" ]; then
  effective_task_limit=$EVOLVE_TASK_LIMIT
  if [ -f "$EVOLVE_RUN_DIR/task-split.json" ]; then
    effective_task_limit=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["tasks"]))' \
      "$EVOLVE_RUN_DIR/task-split.json")
  fi
  set -- "$@" --n-tasks "$effective_task_limit"
  export EVOLVE_HARBOR_EXPECTED_TRIALS=$((effective_task_limit * EVOLVE_HARBOR_ATTEMPTS))
fi
set -- "$@" --agent "$EVOLVE_HARBOR_AGENT"
if [ -n "${EVOLVE_HARBOR_ENVIRONMENT:-}" ]; then
  set -- "$@" --env "$EVOLVE_HARBOR_ENVIRONMENT"
fi
if [ -f evaluator/environment.kwargs ]; then
  while IFS= read -r environment_kwarg || [ -n "$environment_kwarg" ]; do
    [ -n "$environment_kwarg" ] && set -- "$@" --environment-kwarg "$environment_kwarg"
  done < evaluator/environment.kwargs
fi
if [ -f evaluator/agent.kwargs ]; then
  while IFS= read -r agent_kwarg || [ -n "$agent_kwarg" ]; do
    [ -n "$agent_kwarg" ] && set -- "$@" --agent-kwarg "$agent_kwarg"
  done < evaluator/agent.kwargs
fi
set -- "$@" --ae "EVOLVE_CANDIDATE_SOURCE=$PWD/target"
set -- "$@" --mounts "$runtime_mounts"
if [ "${EVOLVE_HARBOR_CODEX_SUBSCRIPTION:-0}" != "1" ]; then
  for credential_name in OPENAI_API_KEY OPENAI_BASE_URL OPENAI_API_BASE; do
    eval "credential_value=\${$credential_name-}"
    if [ -n "$credential_value" ]; then
      set -- "$@" --ae "$credential_name=$credential_value"
    fi
  done
fi
agent_proxy_http=
agent_proxy_https=
agent_proxy_no=
agent_model_base=
if [ -f evaluator/agent.env ]; then
  while IFS= read -r agent_entry || [ -n "$agent_entry" ]; do
    if [ -n "$agent_entry" ]; then
      set -- "$@" --ae "$agent_entry"
      case "$agent_entry" in
        http_proxy=*|HTTP_PROXY=*) agent_proxy_http=${agent_entry#*=} ;;
        https_proxy=*|HTTPS_PROXY=*) agent_proxy_https=${agent_entry#*=} ;;
        no_proxy=*|NO_PROXY=*) agent_proxy_no=${agent_entry#*=} ;;
        OPENAI_BASE_URL=*|OPENAI_API_BASE=*) agent_model_base=${agent_entry#*=} ;;
      esac
    fi
  done < evaluator/agent.env
fi
if [ -f evaluator/verifier.env ]; then
  while IFS= read -r verifier_entry || [ -n "$verifier_entry" ]; do
    [ -n "$verifier_entry" ] && set -- "$@" --ve "$verifier_entry"
  done < evaluator/verifier.env
fi
if [ -n "$evaluator_runtime_env" ]; then
  while IFS= read -r evaluator_runtime_entry || [ -n "$evaluator_runtime_entry" ]; do
    case "$evaluator_runtime_entry" in
      ""|\#*) continue ;;
      *=*) set -- "$@" --ae "$evaluator_runtime_entry" --ve "$evaluator_runtime_entry" ;;
      *)
        printf 'invalid evaluator runtime environment entry: %s\n' "$evaluator_runtime_entry" >&2
        printf 'infra_failed\n' > "$EVOLVE_RUN_DIR/status"
        exit 3
        ;;
    esac
  done < "$evaluator_runtime_env"
fi
if [ -f "$EVOLVE_RUN_DIR/runtime-agent.env" ]; then
  while IFS= read -r agent_entry || [ -n "$agent_entry" ]; do
    [ -n "$agent_entry" ] && set -- "$@" --ae "$agent_entry"
  done < "$EVOLVE_RUN_DIR/runtime-agent.env"
fi
if [ -f "$EVOLVE_RUN_DIR/runtime-verifier.env" ]; then
  while IFS= read -r verifier_entry || [ -n "$verifier_entry" ]; do
    [ -n "$verifier_entry" ] && set -- "$@" --ve "$verifier_entry"
  done < "$EVOLVE_RUN_DIR/runtime-verifier.env"
fi
if [ "${EVOLVE_HARBOR_CODEX_SUBSCRIPTION:-0}" = "1" ]; then
  set -- "$@" --ae "CODEX_FORCE_AUTH_JSON=${CODEX_FORCE_AUTH_JSON:-1}"
  for credential_name in OPENAI_API_KEY OPENAI_BASE_URL OPENAI_API_BASE; do
    set -- "$@" --ae "$credential_name="
  done
fi
while IFS= read -r runtime_entry || [ -n "$runtime_entry" ]; do
  if [ -n "$runtime_entry" ]; then
    case "$runtime_entry" in
      UV_OFFLINE=*) set -- "$@" --ae "$runtime_entry" ;;
      *) set -- "$@" --ae "$runtime_entry" --ve "$runtime_entry" ;;
    esac
  fi
done < "$EVOLVE_RUN_DIR/candidate-runtime.env"
if [ -n "${EVOLVE_CANDIDATE_SMOKE_MODE:-}" ]; then
  set -- "$@" --install-only --ae "EVOLVE_CANDIDATE_SMOKE_MODE=$EVOLVE_CANDIDATE_SMOKE_MODE"
fi
if [ -n "${EVOLVE_HARBOR_MODEL:-}" ]; then
  set -- "$@" --model "$EVOLVE_HARBOR_MODEL"
  set -- "$@" --ve "EVOLVE_HARBOR_MODEL=$EVOLVE_HARBOR_MODEL"
elif [ -n "${OPENAI_MODEL:-}" ]; then
  set -- "$@" --model "openai/$OPENAI_MODEL"
  set -- "$@" --ve "EVOLVE_HARBOR_MODEL=openai/$OPENAI_MODEL"
fi
if [ -n "${EVOLVE_HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER:-}" ]; then
  set -- "$@" --agent-setup-timeout-multiplier "$EVOLVE_HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER"
fi
if [ -n "${EVOLVE_HARBOR_AGENT_TIMEOUT_MULTIPLIER:-}" ]; then
  set -- "$@" --agent-timeout-multiplier "$EVOLVE_HARBOR_AGENT_TIMEOUT_MULTIPLIER"
fi
if [ -n "${EVOLVE_HARBOR_VERIFIER_TIMEOUT_MULTIPLIER:-}" ]; then
  set -- "$@" --verifier-timeout-multiplier "$EVOLVE_HARBOR_VERIFIER_TIMEOUT_MULTIPLIER"
fi
if [ -n "${EVOLVE_HARBOR_MAX_RETRIES:-}" ]; then
  set -- "$@" --max-retries "$EVOLVE_HARBOR_MAX_RETRIES"
  set -- "$@" --retry-exclude AgentTimeoutError
  set -- "$@" --retry-exclude EvolveCandidateInvalidError
  set -- "$@" --retry-exclude ApiUsageLimitError
fi
model_base=${agent_model_base:-${OPENAI_BASE_URL:-${OPENAI_API_BASE:-}}}
proxy_no_configured=${EVOLVE_HARBOR_NO_PROXY:-${agent_proxy_no:-}}
proxy_bypass=$(
  "$EVOLVE_FRAMEWORK_PYTHON" - "$model_base" "$proxy_no_configured" "${no_proxy-}" "${NO_PROXY-}" <<'PY'
import sys
from urllib.parse import urlsplit

base_url, override, *configured = sys.argv[1:]
entries = []
for value in ([override] if override else configured):
    for entry in value.split(","):
        entry = entry.strip()
        if entry and entry not in entries:
            entries.append(entry)
if base_url:
    hostname = urlsplit(base_url).hostname
    if not hostname:
        raise SystemExit("configured model base URL has no hostname")
    if hostname not in entries:
        entries.append(hostname)
print(",".join(entries))
PY
)
proxy_http=${EVOLVE_HARBOR_HTTP_PROXY:-${agent_proxy_http:-${http_proxy:-${HTTP_PROXY:-}}}}
proxy_https=${EVOLVE_HARBOR_HTTPS_PROXY:-${agent_proxy_https:-${https_proxy:-${HTTPS_PROXY:-}}}}
for proxy_entry in \
  "http_proxy=$proxy_http" "HTTP_PROXY=$proxy_http" \
  "https_proxy=$proxy_https" "HTTPS_PROXY=$proxy_https" \
  "no_proxy=$proxy_bypass" "NO_PROXY=$proxy_bypass"; do
  if [ -n "${proxy_entry#*=}" ]; then set -- "$@" --ae "$proxy_entry" --ve "$proxy_entry"; fi
done
set -- "$@" --job-name "$EVOLVE_ATTEMPT_ID" --jobs-dir "$jobs_dir" --n-attempts "${EVOLVE_HARBOR_ATTEMPTS:-1}" -n "${EVOLVE_HARBOR_N_CONCURRENT:-$EVOLVE_HARBOR_N}" -y -q
if [ -n "${EVOLVE_CANDIDATE_SMOKE_MODE:-}" ]; then
  "$UV" run --project "$EVOLVE_WORKSPACE" --frozen \
    --python "$EVOLVE_FRAMEWORK_PYTHON" harbor "$@"
  exit $?
fi
if [ "${EVOLVE_LIVE_OUTPUT:-0}" = "1" ]; then
  live_fifo="$EVOLVE_RUN_DIR/.harbor-live.fifo"
  rm -f "$live_fifo"
  mkfifo "$live_fifo"
  tee "$EVOLVE_RUN_DIR/harbor.log" < "$live_fifo" &
  tee_pid=$!
  "$UV" run --project "$EVOLVE_WORKSPACE" --frozen \
    --python "$EVOLVE_FRAMEWORK_PYTHON" harbor "$@" > "$live_fifo" 2>&1 || harbor_rc=$?
  wait "$tee_pid" || true
  rm -f "$live_fifo"
else
  "$UV" run --project "$EVOLVE_WORKSPACE" --frozen \
    --python "$EVOLVE_FRAMEWORK_PYTHON" harbor "$@" > "$EVOLVE_RUN_DIR/harbor.log" 2>&1 || harbor_rc=$?
fi
"$EVOLVE_FRAMEWORK_PYTHON" evaluator/parse_score.py "$jobs_dir" "$EVOLVE_RUN_DIR" "$harbor_rc"
parser_rc=$?
exit "$parser_rc"
