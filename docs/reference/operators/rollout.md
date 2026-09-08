# Rollout

`rollout` produces the training behavior and evidence used to propose a child.
It is required. Rollout evidence may inform mutation, so a benchmark rollout
must consume only the frozen train split.

## Contract

```python
class RolloutOperator:
    def rollout(self, checkout, ctx) -> RolloutResult: ...
```

The result contains a summary and artifact paths.

## Library operators

| Operator | Behavior |
| --- | --- |
| `harbor` | run a bounded train batch through Harbor and normalize trajectories, verifier output, usage, and failures |
| `parent_evaluation` | expose sanitized, certified evaluation evidence already attached to the selected parent |
| `failure_focused` | select failure-oriented training/evaluation metadata |
| `noop` | emit empty rollout evidence for controlled tests |

## `harbor` configuration

```yaml
operators:
  rollout:
    operator: harbor
    timeout_s: 3600
    config:
      budget_tasks: 10
      task_sampling: generation_shuffle
      n_concurrent: 4
      agent_setup_timeout_multiplier: 1
      verifier_timeout_multiplier: 1
      max_retries: 1
```

Important keys:

- `budget_tasks`: maximum train tasks in this generation;
- `task_sampling`: `head` or deterministic `generation_shuffle`;
- `task_names`: optional exact names from the frozen train split;
- `n_concurrent`: concurrent Harbor trials;
- `agent_setup_timeout_multiplier`, `agent_timeout_multiplier`, and
  `verifier_timeout_multiplier`: multiply the corresponding limits declared
  by each task's `task.toml`; keep the operator `timeout_s` large enough for
  the resulting longest trial;
- `agent_env` and agent/runtime settings: inputs forwarded to the rollout
  adapter;
- `max_retries` and `timeout_s`: infrastructure retry and operator limits.

`operators.rollout.config.path` is incompatible with a resolved frozen split because
it would bypass frozen dataset membership.

## Artifacts

The Harbor operator writes normalized evidence such as:

```text
runs/gen-N/rollout/summary.json
runs/gen-N/rollout/cases.json
runs/gen-N/rollout/harbor-state.json
runs/harbor-rollouts/gen-N/
```

`cases.json` is the method-neutral input to `analyze`. It includes task
identity, ordered model/tool events, verifier evidence, outcome, exception,
usage, timing, and artifact inventory.

`harbor-state.json` is a facts-only observability artifact. It records each
train trial's Harbor lifecycle status, timestamps, stage durations, exception,
declared `task.toml` resources and timeouts, selected Harbor configuration,
bounded command events, and artifact references. It does not diagnose a cause
or recommend a mutation. Facts that current Harbor artifacts do not retain,
such as live cgroup counters after a container has exited, are explicitly
marked `unavailable` rather than reported as zero. The feedback bundle exposes
valid train-role state at `feedback/evidence/harbor-state.json`; dedicated,
gate, and sealed data are not copied into mutation feedback.

Harbor training and evaluation share `evolve.integrations.harbor._runtime_plan`
for bind mounts. Explicit `EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON` replaces the
cache default; configured Python/tool mounts are then appended. Duplicate
mount targets, relative mount paths, malformed mount objects, and missing
explicit offline executables fail before Harbor starts. Each path writes
`candidate-runtime.mounts.json` and `candidate-runtime.plan.json` alongside its
run artifacts. A mount-plan receipt describes configuration, not a successful
live connectivity check.

The training launcher streams its redacted log and writes `harbor.status.json`.
Creating the sibling `harbor.cancel` requests cancellation; it is checked before
launch and while waiting. Cancellation and timeouts send SIGTERM, allow up to thirty
seconds for cleanup (at most a quarter of the configured operator timeout), and escalate to SIGKILL only if necessary. The receipt
reports forced termination; that flag does not prove external containers were
cleaned up. Existing nonempty Harbor job directories are never silently erased.
For an inspected, stopped job, use the explicit adapter entry:

```bash
evolve harbor-resume /absolute/path/to/job \
  --config-sha256 <sha256-of-original-config.json> \
  --owner-status /absolute/path/to/harbor.status.json \
  --error-type CancelledError --timeout-s 3600
```

Run from the original working directory with the same Harbor executable and
runtime environment. The owner receipt must be terminal; the expected digest
must match the saved job config, whose destination must match the supplied job.
The adapter refuses symlink-bearing evidence, snapshots the entire job under
its sibling `.<job-name>-recovery/attempt-N/before-resume`, and only then invokes
Harbor's native resume with explicit exception filters. Logs and the recovery
receipt are private files under that attempt. Logs may contain Harbor's raw
output; inspect before sharing. A successful process exit is not a successful
benchmark score and does not update the archive or a failed operator action.

The recovery lock excludes concurrent calls through this entry. The original
owner must remain stopped; do not race it with direct Harbor commands. A crash
leaving a `running` or `backing_up` recovery receipt blocks another attempt and
requires manual process/evidence inspection. The config digest does not freeze
external mount contents or credentials. Changed runtime configuration needs a
separate job/revision; this entry does not automate that migration.


Optional `stop_after_errors` and `stop_after_failures` positive counts stop an
active training batch after completed trial results reach the configured limit.
Errors count explicit Harbor `exception_info`; failures count completed trials
whose nonempty numeric rewards are all exactly zero. Missing rewards never
count as zero. These are budget/stop policies, not infrastructure diagnoses;
choose limits in the recipe, without embedding task identities in the adapter.
The launcher polls persisted trial results, records counts and limits in
`harbor.status.json`, and requests graceful termination with exit 125. Already
running trials and a dispatch racing the observation may execute before
cancellation takes effect. This is not an atomic Harbor scheduler hook.
A stopped rollout retains partial evidence and cannot be reported as completed.
Silent verifier provisioning failures reported as ordinary zero rewards require
separate environment checks; an error-only threshold cannot detect them.

Training and evaluation both accept `EVOLVE_HARBOR_EXTRA_DOCKER_COMPOSE_JSON`,
a JSON array of absolute paths to existing Compose overlay files. Both paths
use the same resolver and forward each path as `--extra-docker-compose` in the
listed order. Spaces in paths are preserved. Malformed, missing, or duplicate
inputs fail before Harbor launches. Each run writes
`candidate-runtime.compose.json` with the source paths and SHA-256 digests.

Keep these files and their referenced resources unchanged for the entire job;
the receipt records the overlay bytes at preparation time, but does not freeze
transitive files or environment interpolation. Compose merging and platform
support remain Harbor/Docker responsibilities. Network choices and package
manager configuration belong in experiment-owned overlays and bind mounts,
not task-specific framework branches. A changed overlay requires a new runtime
revision and validation before comparing scores.

Harbor rollout summaries count execution exceptions independently from scored
outcomes: a trial may contribute to `passed` or `failed` and also to an error
count. `exception_counts` preserves exception types. The existing `infra_errors`
aggregate also includes incomplete results; `incomplete_tasks` identifies that
subset explicitly. Agent-side exception classification does not establish root
cause. Rewards and native exception evidence remain unchanged.

Harbor cases also expose `execution.time_budget`: the declared effective agent
limit, native agent elapsed seconds, fraction consumed, remaining seconds, and
an independent `timed_out` flag. The limit follows Harbor's
`min(override-or-task-limit, cap) * phase-or-global-multiplier` rule. Unknown
metadata remains unavailable. Replay rewards remain authoritative for replay;
native result metadata supplies timing only. The rollout summary exposes
`agent_timeout_count` and `agent_time_budget`, including coverage, maximum
fraction, and the count consuming at least 80% of the budget. That threshold is
an observation bucket, not a stopping rule or a penalty. No rewards change.
