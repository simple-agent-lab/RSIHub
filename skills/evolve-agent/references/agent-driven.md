# Agent Driven control

Use this control path only when the experiment asks an outer Agent to decide
how evolution proceeds. HyperAgents may be the starting method, but Agent
Driven describes who controls the loop, not which operators are installed.

## Contract

RSI Hub owns candidate identity, evaluator execution, stamps, lineage, and the
action/call boundary. The outer Agent owns the search policy:
it may inspect evidence, choose a parent, invoke or repeat direct operators,
edit the target and permitted process files, abandon a branch, or submit a
champion.

The Agent may adapt the sequence after every observation. Do not turn the
following into a required stage list:

```text
inspect state → choose one useful action → observe → revise or stop
```

Use `./evolve operator active . --json` to discover the live capabilities and
their access modes. Use only the workspace console for state transitions. A
configured prerequisite or admission check remains binding even when the Agent
would prefer to skip it; that is a mechanism boundary, not search policy.

Start only from a certified valid parent, then use the durable action surface:

```bash
./evolve agent prepare .
./evolve agent start . --max-actions 20 --max-operator-calls 10 --max-evaluations 3 \
  --max-cost-usd 10 --max-wall-s 3600 --require-clean-start
./evolve agent schema
./evolve agent status .
./evolve agent act . --action '<one JSON action>'
```

Use a fresh action ID for each decision. Record evidence and a falsifiable
hypothesis with `observe`, use `fork` before editing the managed child
worktree, and run `checkpoint` before `commit`. Choose a generation ID that is
absent from `occupied_generations` in `agent status`; archive history can occupy
an ID even when it is not the current champion. A repeated completed action ID
is idempotent. If status reports an interrupted action, inspect its retained
side effects and use `agent resolve-interrupted` before continuing. A live
action reports `running`; do not recover it while its process holds the session
lock.

## Process changes

When `operators/**` is mutable, the Agent may change an active operator after
reading its protocol and relevant evidence. Record target and process paths
separately. Treat the new process as a proposal for later invocations; do not
claim it improved search until a later candidate actually used it.

Runtime configuration in `evolve.yaml` is protected. Until candidate-scoped
operator configuration exists, change executable process behavior only through
permitted `operators/**` files. Agent operator actions cannot override
configuration, timeouts, gate, record, or sealed partitions. Do not edit
protected configuration to simulate inheritance.

`gate` and `record` are protected from Agent Driven candidates. Other operator
changes become executable only after the candidate is certified and used as a
later parent. Treat the `operator_source` field in the action receipt as the
authority for which process version ran.

## Budget and stopping

Before the first live action, state cost, wall, action, operator-call, and
evaluation bounds plus what evidence would stop the run. The runtime checks
observed operator/evaluation cost and elapsed wall time between actions; one
in-flight action can overshoot either bound. An ad hoc external controller's
model usage remains outside these counters. For a long run, use the host
launcher and make its controller wrapper write a token and dollar receipt:

```bash
./evolve agent run-controller . --controller /path/to/controller \
  --max-attempts 3 --max-controller-tokens 1000000 \
  --max-controller-wall-s 216000
```

The launcher executes an argument vector without a shell and exports
`EVOLVE_AGENT_WORKSPACE`, `EVOLVE_CONTROLLER_ATTEMPT`,
`EVOLVE_CONTROLLER_ATTEMPT_DIR`, and `EVOLVE_CONTROLLER_USAGE_RECEIPT`. The
last path must receive a JSON object containing non-negative `total_tokens` and
numeric or null `cost_usd`. Null is permitted only without a controller/total
dollar cap and is reported as unpriced. Each invocation runs one controller attempt; repeat the exact
command to resume. Inspect `agent controller-status` first. An interrupted
controller attempt requires explicit inspection and
`agent resolve-controller-interrupted`, including observed or conservative
upper-bound token usage, just as an interrupted action does.
A rejected or invalid action consumes budget; do not silently replace it with
a fresh attempt.

Submit a champion when another action lacks a concrete evidence-backed reason,
the budget is exhausted, or the experiment's stopping rule fires. The host,
not the Agent, performs any final sealed evaluation after the champion and
analysis rules are frozen. Sealed results never feed another search action.
The host may then run `./evolve agent seal-submitted . --confirm`; this command
is deliberately absent from the controller action schema.

The host-shell controller is trusted. Tracked-tree and archive integrity checks
detect accidental or unsophisticated bypasses, but a same-UID process can read
workspace files and rewrite session records. Put an untrusted controller behind
an OS/container boundary that exposes only the action broker; never claim this
mode alone is a sandbox or a confidentiality boundary.

## Comparison

For a fixed-driver comparison, start two independent, clean arms from the same
candidate and operator snapshot. Do not use a raw filesystem copy because it
retains the runtime mirror identity. Use the supported clone command, then
audit the resulting pair:

```bash
./evolve agent clone-arm /path/to/baseline /path/to/fixed
./evolve agent clone-arm /path/to/baseline /path/to/agent
./evolve agent audit-arms /path/to/fixed /path/to/agent
```

Match model, cost, token, evaluation, and wall-time allowances. Generations
alone are not a sufficient budget because the Agent Driven arm chooses how many
actions occur inside one iteration.

Report whether the Agent used control that the fixed driver lacks, which
process changes were later exercised, final evaluator-stamped performance, and
cumulative cost. A changed process file without later execution is capability
use, not evidence of benefit.

## Continuous execution without model polling

When `EVOLVE_CONTROLLER_ACTION_MODE=deferred`, make one decision, request one
`agent act --defer` action, and end the controller turn. CLI actions in this mode
are automatically deferred. Do not poll the queued request: the host's
`agent run-controller --continuous` loop executes it after you exit and invokes
the next decision with the durable result. Inspect that result before choosing
another action. The shared attempt budget counts decisions, not generations.

A pending or failed operation is not permission to start a replacement. Inspect
its evidence and use explicit interruption/handoff resolution before recovery.
Do not edit queue, controller, archive, or evaluation receipts. On submission,
include `stop_reason` (`completed`, `budget`, `infrastructure`, `no_improvement`,
or `cancelled`) so retaining a valid champion is distinguishable from satisfying
the requested experiment. A valid baseline with zero new evaluations does not
establish that a requested multi-round experiment completed.

### Time pressure is execution evidence

Inspect `evaluations[*].execution` in the host facts even when an evaluation has
an ordinary numeric score. A benchmark-owned timeout can be a scoreable zero;
it is neither an absent result nor automatically an infrastructure failure.
The action receipt exposes aggregate owner and exception counts, not held-out
task identities. Historical receipts without this field have unknown coverage.

For Harbor training, inspect `agent_timeout_count`, `agent_time_budget` in the
rollout summary and `execution.time_budget` in cases. Successful cases close to
the limit are relevant too. Missing limits or timing are unknown, not zero.
When designing a training sample, retain representative agent timeouts and slow
successes if they exist in the training pool; do not discard every exception as
infrastructure noise. Preserve the split boundary and document missing coverage.

A time-management hypothesis needs an observable clock, an adaptation to the
remaining budget, and a verifiable task result. Compare success rate, timeout
count and cost together. Early abandonment can remove the timeout label without
solving anything. Keep frozen scores, deadlines and selection rules unchanged;
choose any new resource objective before a new experiment, never after viewing
its final results. Final holdout failures cannot guide another candidate in the
same independent comparison.

## Continuous research sessions

For a new persistent research task, start with `--mode continuous`,
`--optimizer <method-directory>`, and `--objective <goal>`. The method must
contain `instructions.md`; optional tools and `resume.md` are snapshotted
with it. Use `agent schema --mode continuous` for the action contract.

In this mode, `publish_best` publishes without ending research; use
`finish_research` to finish or `pause_research` with a resume condition.
`submit_champion` remains an original-mode action and is rejected here.
Modify a copy of the method in `runs/agent-driven/optimizer/drafts/`, then
request `adopt_optimizer` with `expected_digest` and a reason. Next launch
uses that snapshot in a fresh model context, while all fees stay cumulative.

Write research notes and complex data directly into the persistent
`runs/agent-driven/notes/` directory as needed. Method rollback does not roll
back these files. Facts in host receipts and hypotheses in notes are distinct.
Local execution is not an isolation boundary for private evaluation data.
