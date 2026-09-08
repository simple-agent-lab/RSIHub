# Operator overview

A stage is a fixed lifecycle slot. An operator is one reusable implementation
at `library/<stage>/<name>.py`. A recipe is the code-free selection and
configuration of operators. Canonical evaluation is framework-owned and cannot
be replaced by a recipe operator.

```text
select
  → rollout
  → analyze
  → mutate
  → validate
  → novelty
  → canonical evaluation
  → gate
  → record
  → reflect
```

Optional stages are skipped when their configuration block is absent.
Canonical evaluation is framework-owned and is not an operator.

| Operator | Required | Responsibility |
| --- | --- | --- |
| [`select`](operators/select.md) | yes | choose valid parent generations |
| [`rollout`](operators/rollout.md) | yes | produce training behavior and execution evidence |
| [`analyze`](operators/analyze.md) | no | transform rollout cases into bounded mutation feedback |
| [`mutate`](operators/mutate.md) | yes | edit the candidate inside the declared surface |
| [`validate`](operators/validate.md) | no | run method-specific checks before canonical evaluation |
| [`novelty`](operators/novelty.md) | no | reject candidate edits that duplicate prior work |
| [`gate`](operators/gate.md) | yes | decide whether a canonical evaluation is parent-eligible |
| [`record`](operators/record.md) | yes | attach method-specific evidence to the archive |
| [`reflect`](operators/reflect.md) | no | derive reusable insights from verified history |

## Discover and validate the library

Discovery is filesystem-only and never imports operator code into the
framework process. Inspection runs each entry file in a subprocess:

```bash
evolve operator list
evolve operator list mutate --json
evolve operator describe mutate/hyperagents
evolve operator check mutate/hyperagents --config '{"runner":"local"}'
```

Create a complete SDK entry file in a source checkout with:

```bash
evolve operator new mutate my_operator
```

Every named library entry must expose `--describe` and `--validate-config` via
`sdk.main(..., config_schema=CONFIG)`. The declarative schema rejects unknown
or invalid settings and reports defaults during inspection. Underscore-prefixed
files and directories are helper modules, not discoverable operators.

## Compose a recipe

Each enabled stage selects exactly one `operator` or `script`. Put every
operator-specific setting under `config`:

```yaml
operators:
  select:
    operator: greedy
    timeout_s: 600
    config: {}
  mutate:
    operator: hyperagents
    timeout_s: 3600
    config:
      runner: harbor
      editable_roots: [target]
```

Check the complete composition before initialization:

```bash
evolve recipe check /path/to/recipe/evolve.yaml
evolve recipe check /path/to/recipe/evolve.yaml --json
```

Recipe-local operator directories are rejected. A `script:` binding is still
executable, but `recipe check` marks it non-portable because it depends on a
filesystem path outside the shared named catalog.

## Inspect initialized bindings

After initialization:

```text
operators/       frozen active recipe-selected operator scripts
library/         frozen runtime helpers imported by selected library operators
evolve.yaml      normalized operator config
.evolve-components.json   source identity, digest, and portability
```

Inspect the active configuration with:

```bash
./evolve operator active .
./evolve operator active . --json
cat operators/README.md
```

Direct orchestration can invoke a configured stage and retains its artifacts:

```bash
./evolve operator run . rollout --genid 1 --parent 0
./evolve operator run . mutate --genid 1 --parent 0 --config '{}'
./evolve finalize . 1 --parent 0
```

Operators execute as subprocesses. They should write diagnostics beneath their
generation run directory and return the typed result for their interface. They
must not write evaluator truth, generation tags, or archive outcomes directly.

## Agent Driven control

Start an Agent Driven session only after generation zero has a certified score
and is a valid parent. Use the ordinary evaluator verb rather than driver run,
because driver startup also creates the sealed baseline anchor:

```bash
./evolve eval . 0
./evolve agent start . \
  --max-actions 20 \
  --max-operator-calls 10 \
  --max-evaluations 3 \
  --max-cost-usd 10 \
  --max-wall-s 3600 \
  --require-clean-start
./evolve agent schema
./evolve agent status .
```

The outer agent submits one strict JSON action at a time. For example:

```bash
./evolve agent act . --action \
  '{"id":"fork-1","type":"fork","parent":"0","genid":"1"}'
./evolve agent act . --action \
  '{"id":"check-1","type":"checkpoint","parent":"0","genid":"1"}'
./evolve agent act . --action \
  '{"id":"submit","type":"submit_champion","genid":"1"}'
```

The session writes an append-only action receipt at
`runs/agent-driven/actions.jsonl`. Repeating a completed action ID returns its
terminal receipt instead of executing it again. A process crash leaves the
action visible as interrupted; after inspecting its side effects, mark that
attempt failed with `agent resolve-interrupted` before choosing the next
action. While the process still holds the session lock, status reports
`running` instead. Recovery accounts for observable operator cost already
written to its run artifacts.

Candidate edits still happen in the managed child worktree returned by the
`fork` action. A `checkpoint` classifies `target/**` and `operators/**` changes
and reports surface violations. `gate` and `record` are protected process
paths. Other changed operators execute only after that candidate becomes a
valid parent: an operator action for child `gen/2` with parent `gen/1` loads
the implementation from `gen/1`, never from the dirty `gen/2` worktree.

Agent actions cannot supply operator configuration or timeout overrides, and
only controller-safe stages are accepted. The session checks the tracked
control tree and archive between actions and blocks ordinary orchestration CLI
routes while `runs/agent-driven/ACTIVE` exists.

The host owns action, operator-call, evaluation, observed-cost, and elapsed-wall
limits. Cost and wall limits are checked between actions, so a single in-flight
action can overshoot them. It also keeps
candidate identity, evaluator stamps, lineage, gate, record, and final sealed
evaluation outside the action vocabulary. The outer controller's own model
usage is not observable when it is launched externally; include that usage in
an experiment's matched budget separately.

Before a paired run, create two independent workspaces with no post-baseline
generation history. Use the supported copy command instead of `cp -a`; it
rejects a dirty or previously used source, copies the complete certified
baseline evidence, and assigns the destination a fresh workspace identity:

```bash
./evolve agent clone-arm /path/to/baseline /path/to/fixed
./evolve agent clone-arm /path/to/baseline /path/to/agent
```

Then verify the pair with:

```bash
./evolve agent audit-arms /path/to/fixed /path/to/agent
```

This command checks independent Git storage and distinct evolve workspace
identities, the same certified candidate and target/operator/evaluator trees,
and clean generation occupancy. A filesystem copy retains its source workspace
identity, so assign the copy a fresh identity (or initialize it independently)
before using it as the second arm.

For a long controller run, start the bounded session first and launch the
controller as an argument vector, without a shell:

```bash
./evolve agent start /path/to/agent --max-actions 80 --max-operator-calls 30 \
  --max-evaluations 6 --max-cost-usd 300 --max-wall-s 216000 --require-clean-start
./evolve agent run-controller /path/to/agent \
  --controller /path/to/RSIHub/scripts/codex_agent_controller.py \
  --controller-arg=--prompt --controller-arg=/path/to/controller-prompt.md \
  --controller-arg=--codex-arg --controller-arg=--model=gpt-5.4 \
  --max-attempts 3 --max-controller-tokens 1000000 \
  --max-controller-wall-s 216000
```

The launcher runs one attempt per invocation. It sets
`EVOLVE_AGENT_WORKSPACE`, `EVOLVE_CONTROLLER_ATTEMPT_DIR`, and
`EVOLVE_CONTROLLER_USAGE_RECEIPT`. Before exiting, the controller wrapper must
write the receipt as
`{"total_tokens": <integer>, "cost_usd": <number-or-null>}`. A null cost is
retained as unpriced and is allowed only when no controller or total dollar
limit was configured. Standard output, standard
error, receipts, and cumulative controller/mechanism cost remain under
`runs/agent-driven/controller/`. Re-run the exact command to resume. Inspect
`agent controller-status` first; if the host died between start and terminal
receipt, use `agent resolve-controller-interrupted` only after checking that no
controller or child action remains alive. Supply the observed or conservative
upper-bound `--total-tokens`, plus `--cost-usd` when dollar limits are active,
so recovery cannot erase spend. The bundled Codex wrapper records
Codex 0.149 JSON events and session ID, resumes that session on later attempts,
and emits token usage; Codex subscription usage remains unpriced.

These checks are audit and misuse defenses for a trusted host controller, not
a security sandbox. A same-UID process can rewrite session metadata or read
workspace files. Run an untrusted controller under a separate OS identity or
container with only a brokered action interface; do not describe the current
host-shell mode as isolated.

After `submit_champion` closes the session, a host operator may run the one-way
final evaluation. The explicit confirmation is outside `agent act`, and the
submitted session cannot accept another action afterward:

```bash
./evolve agent seal-submitted . --confirm
```

When the frozen sealed cohort is empty, both driver final-anchor handling and
`seal-submitted` are no-ops; the latter reports `sealed: not_configured`.

### Deferred actions and continuous progression

For long operations, run the controller with `agent run-controller --continuous`.
The controller requests one action using `agent act --defer --action '<JSON>'`,
then exits with its usage receipt. Continuous mode marks the controller environment
so CLI actions are deferred even if the controller omits `--defer`; the bundled
Codex wrapper appends the one-decision handoff instructions. RSIHub executes the action outside the model
process and launches the next controller attempt after it finishes. Give the
launcher enough `--max-attempts` for the intended decision count; each handoff
uses an attempt and all attempts share the configured usage limits.

The durable handoff is `runs/agent-driven/deferred-action.json`; progression
state lives under `runs/agent-driven/progression/`. Re-run the same continuous
command after a clean interruption. A crash after action completion but before
handoff acknowledgment reuses the action receipt instead of repeating the
operation. An interrupted action remains blocked for explicit inspection and
resolution; this command never retries uncertain external effects. A controller
that exits without submitting a champion or deferring work blocks progression
instead of being invoked in a polling loop. This is a foreground execution
loop: use a process supervisor when survival across host or terminal failure is
required. It does not install a daemon.

`submit_champion` accepts an optional `stop_reason`: `completed`, `budget`,
`infrastructure`, `no_improvement`, or `cancelled`. The progression result
reports the reason and number of candidate evaluations separately from a valid
champion submission. It does not certify that a user's experimental objective
was satisfied. Controller token and dollar limits are checked between attempts,
not during an individual model invocation; wall time is enforced during the
invocation. Unknown controller cost remains unknown.

After inspecting a failed or unstarted deferred request, use
`agent resolve-deferred --reason '...'` to release that handoff without retrying
it. Resolution is recorded in `deferred-resolutions.jsonl`; it is rejected while
an action is live or unresolved. For an interrupted action, inspect external
side effects and use the existing `resolve-interrupted` flow first. Neither
resolution erases the original action or its cost.


After inspecting and explicitly resolving an interrupted action and its deferred
handoff, `evolve agent resume-controller WORKSPACE` reloads the persisted
progression configuration and continues with the original cumulative budgets.
It does not reset attempts, resubmit a terminal action, or infer that native
Harbor recovery completed an operator action. Existing unresolved-action guards
still apply. A host supervisor can invoke this command after restart, but the
CLI does not install a service or resolve uncertain external side effects.

At a configured budget boundary, continuous progression records
`progression/budget-stop.json`, releases unstarted queued work with an audit
reason, and submits the certified best with `stop_reason: budget` without another
model call. A controller's already queued submission can still finish. Interrupted
work or unknown controller cost requires reconciliation instead of automatic
closure. Limits and usage receipts are retained across resume; this is not an
in-flight dollar cap.

An `observe` action may include `claims`, a list of objects with `path`, `sha256`,
`keys`, and `value`. The path must be listed in `evidence` and resolve inside the
workspace; `keys` traverses JSON object keys. The host checks the file digest and
exact value before recording verified claims. A mismatch fails the action and
blocks its deferred handoff. For example, a claim with `keys: ["errors"]` and
`value: 0` cannot cite a JSON file containing `{"errors": 2}`. Hashes establish
which bytes were checked, not whether a mutable source is authoritative.
Free-form hypotheses and decisions remain explicitly unverified; claims are
optional and do not constitute a natural-language fact checker.

Each controller attempt receives `EVOLVE_CONTROLLER_FACTS`, a host-generated
`session-facts.json` snapshot of session identity, certified baseline/best,
evaluation count, recorded action cost, completed evaluation receipts and failed
action IDs. The bundled Codex wrapper asks observations to cite these facts.
Claims with `path: "@session"` are checked against freshly derived host receipts,
not a similarly named controller-written file. Use the snapshot's SHA256 and
list `@session` in `evidence`. Action failure counts are not trial error counts;
this projection does not infer missing task-level statistics. Arbitrary-file
claims still only establish consistency with those bytes. This mechanism does
not make the shared filesystem a hostile-code security boundary.


Agent session status and final progression results include `health`; the same
projection is included in `@session` controller facts. It retains failed actions,
explicit `infrastructure_failed` evaluation results, failed controller attempts,
and recovered deferred handoffs, with paths to durable receipts. Completion does
not clear this history. `attention_required` means pending or uncertain evidence;
`incidents_recorded` means historical anomalies remain; `no_recorded_incidents`
means no anomalies in the covered receipts, **not** proof of an error-free run.

A terminal action found before its deferred acknowledgment is recorded once in
`runs/agent-driven/handoff-recoveries.jsonl` before acknowledgment. It proves reuse
of the terminal receipt, not the cause of the interruption. Unknown causes stay
`unknown`; only an evaluator's explicit infrastructure outcome is classified as
infrastructure. Pending controller work is not labeled a confirmed crash. This
projection covers action/controller/handoff receipts, not every native trial or
silent verifier failure. Existing workspaces retain their vendored runtime; a
source update does not retroactively invent missing recovery receipts.

Agent Driven evaluation receipts retain aggregate `execution` counts for
observed/expected/scoreable trials, timeouts by owner, and exception types.
These counts flow into `@session` host facts without task names or verifier
messages. A scored evaluation with trial timeouts records a health incident;
it does not automatically become an infrastructure failure. Health coverage is
limited to receipts and reported evaluation timeouts, not every task problem.
Older receipts lacking these fields are not evidence of zero timeouts.
Rollout usage also sums phase timing dictionaries rather than treating them as
zero; this is cumulative trial-phase time, not concurrent batch wall duration.

### Continuous outer-agent research

`agent start --mode continuous --optimizer PATH --objective TEXT` starts a
persistent research session from a certified baseline. `PATH` is a method
bundle with a non-empty `instructions.md`, optional `resume.md`, and ordinary
research tools. The host snapshots every file and validates Python syntax;
this admits a method for use, not as a demonstrated improvement. The original
agent-session format retains its submit-and-stop behavior; this is unrelated
to recipe-driven execution.

Run the controller through the existing `agent run-controller --continuous`
handoff loop. This flag executes deferred actions; the session's `--mode`
determines whether publishing a result terminates research. The Codex wrapper
loads the exact adopted bundle, receipts its file hashes, and opens a fresh
model context for each adoption. Attempts and costs remain cumulative.

Continuous-only actions are `adopt_optimizer`, `publish_best`,
`pause_research`, `resume_research`, and `finish_research`. Use
`agent schema --mode continuous` for required fields. To edit a method, copy
its snapshot into `runs/agent-driven/optimizer/drafts/`, edit there, then adopt
with `draft_path`, `expected_digest`, and `reason`. To roll back, provide an
existing `digest` instead of `draft_path`. Adoption takes effect only after
its completed action receipt. `publish_best` retains the active session;
`finish_research` closes it. A paused session requires an explicit
`resume_research` action before resuming its controller.

Research notes are ordinary files in `runs/agent-driven/notes/`; no memory
schema or save action is imposed. These files survive method changes and
rollbacks. Trusted action/usage receipts remain separate. Parse rejections
before execution can be returned to the controller for correction, with at
most three consecutive rejected attempts. Unknown external effects still
require reconciliation, and unknown continuous-session usage blocks more
paid work. Final sealed evaluation is permitted only after research finishes.

The local controller executes with its existing host permissions. Method
hashes and mutable-surface checks do **not** isolate private evaluator data.
Continuous execution alone is not a claim of leakage resistance, autonomous
learning, or generalization; those require separate execution boundaries and
independent evaluation.

### Independent candidate delivery

`evolve candidate-export WORKSPACE --ref gen/1 --output PACKAGE` exports only
the committed `target/` tree and prints a receipt containing the manifest SHA-256.
Uncommitted edits, untracked files, the Git database, evaluator files, and research
notes outside `target/` are not included. Files intentionally committed inside
`target/` remain part of the candidate, including target-owned memory. Symlinks
and submodules are refused. Export does not execute candidate code.

The host can install a package without the research repository using
`evolve.candidate.package.materialize_candidate(package, expected_sha256, destination)`.
Obtain the expected digest from the trusted export receipt, not from an untrusted
package alongside its contents. Installation checks every file digest and rejects
extra files and links before publishing the destination. This delivery boundary
does not itself isolate candidate execution or certify benchmark performance.

`evolve candidate-run PACKAGE --sha256 DIGEST --image IMAGE --command sh
--arg /input/run.sh --output RUN_DIR` verifies and materializes the package before
executing it in Docker. The image must already exist locally and is resolved to
its immutable ID. The process sees only `/input` (read-only candidate files),
`/output` (untrusted writable results), the selected image, and a temporary
filesystem. It receives no network, Docker socket, host credentials or host PID
namespace. The default timeout is 60 seconds (`--timeout` overrides it).

The host retains `execution.json`, stdout, and stderr outside the writable
output mount; Docker cleanup is attempted even on timeout or cancellation.
Unconfirmed cleanup is an error. This is an offline execution interface, not a
replacement for canonical benchmark evaluation: it supplies neither a model
service nor private verifier access, and does not stamp a benchmark score.

### Isolated continuous controller transport

`evolve agent run-controller WORKSPACE --continuous --sandbox-image sha256:IMAGE_ID
--controller python3 --controller-arg /input/optimizer/controller.py` runs the
controller inside the offline boundary above. Use `--sandbox-timeout` to set the
per-attempt wall limit. The immutable local image ID and limits persist with the
controller configuration and are reused on resume. Without a model handler, model tokens and charges are authoritatively zero on
the host. With a handler, the host broker accounts for each request.
Controller-owned usage files cannot alter either ledger.

Each invocation receives read-only `/input/controller-input.json`,
`session-facts.json`, `session.json`, `actions.json`, the active `optimizer/`,
a target-only `champion/`, and target copies for existing managed children.
`correction.json` appears after a known no-effect parse rejection. The Git
repository, evaluator directory and host action ledger are not mounted.

The method can freely read/write `/output/notes`, `/output/drafts` and
`/output/contexts`. These regular-file trees are retained on the host between
successful handoffs. Contexts can be organized by the activation ID in the
controller input. Write `/output/method-load.json` matching the loaded optimizer
identity and one `/output/action.json` with the existing typed action payload.
Draft adoption uses the host-relative path `runs/agent-driven/optimizer/drafts/NAME`.

Optional `/output/edits.json` maps `target/RELATIVE_PATH` to
`{"data": "BASE64_BYTES", "executable": false}`; `data: null` deletes a file.
Edits require an existing managed child selected by a checkpoint, operator or
commit request. The host validates paths and the declared mutable surface before
importing these bytes. Only the typed request reaches the existing action queue;
the container cannot write the queue or cost history itself. File edits remain
ordinary non-transactional research changes; an interrupted acceptance requires
inspection before resuming, rather than replaying an uncertain operation.

This isolates the controller execution process. Candidate adapter isolation is
configured separately below. Once the isolated controller manifest exists,
operator invocations use its container image and data handoff boundary too.
Legacy execution before that configuration remains outside this boundary. The legacy host controller path remains
available without `--sandbox-image` and has no filesystem confidentiality boundary.

#### Host model handler

An isolated controller can use `--model-handler EXECUTABLE` with repeated
`--model-handler-arg ARG` and a cumulative controller or total dollar limit.
The handler is a trusted host program, not code from an optimizer or candidate.
It receives two final arguments: an absolute request-body file and an absolute
response-file destination, both inside the private host request ledger. It must
constrain its provider/model, retain credentials on the host, and obtain usage
from that provider. The framework never treats the request body as a command.

The handler writes JSON containing `status` (HTTP status), `content_type`,
`body` (base64 response bytes) and `usage` (`total_tokens`, `cost_usd`). Missing,
invalid, or null cost remains unknown and stops additional calls. Request and
response envelopes are limited to 32 MiB. `--max-model-requests` bounds calls per
controller attempt; `--model-request-timeout` bounds each handler. Remaining
cumulative dollar budgets also constrain each broker invocation, including after
method adoption. Limits are checked between calls, not hard in-flight cost caps.

Inside the container, atomically publish
`/output/broker/requests/32_LOWERCASE_HEX_DIGITS.json` containing only
`{"body":"BASE64_REQUEST_BYTES"}`. Read the corresponding file in
`/output/broker/responses/`. Its public response omits usage. Request IDs are
idempotent within the invocation; changing bytes under a used ID fails. Host
request receipts are retained under the controller attempt's `model-requests/`,
which is never mounted into the container. A started request without known usage
requires reconciliation and is never automatically reissued.

With a handler configured, the launcher wraps the controller with a container-local
HTTP bridge. `OPENAI_BASE_URL` and `EVOLVE_MODEL_BASE_URL` point to its loopback
`/v1` endpoint; `OPENAI_API_KEY` is a local placeholder, never the host key.
Clients may POST to `responses` or `chat/completions`. Headers and URL paths are
not forwarded to the host: the trusted handler receives only the body and must
choose its own fixed provider endpoint and protocol. Client-specific endpoint
configuration may still be necessary.

Responses, including SSE bodies, are buffered until the handler completes and
then returned byte-for-byte. This does not provide incremental token streaming.
A bridge timeout returns HTTP 504 without reissuing the request; the host broker
retains responsibility for its final accounting. Configure client retries with
care: a new HTTP request is a new billable request. A trusted provider handler
is still required; the framework does not infer provider credentials or prices.


#### Isolated Harbor candidate adapter

Build `containers/candidate-worker/Dockerfile` using a context containing the
repository's `pyproject.toml` and `uv.lock`, then resolve the resulting local image
to its full immutable Docker image ID. The image installs the locked Harbor
dependencies; the host supplies framework and committed candidate source as
read-only input on each trial.

Select the frozen proxy in the recipe before workspace initialization:

```yaml
evaluator:
  agent: evolve.integrations.harbor.isolated_candidate:IsolatedCandidateAgent
  agent_kwargs:
    candidate_import_path: target.agent:HarborAgent
    worker_image: sha256:FULL_IMAGE_ID
    worker_timeout_s: 3600
```

The proxy imports candidate code only inside the worker. It preserves adapter
state across setup, run, and post-run log parsing. Its task-environment bridge
accepts execution and byte-based file transfers only; it cannot select a host
file to read or write. Post-run parsing receives agent logs, with no further
task-environment requests serviced. Candidate exceptions retain their failure
classification, partial context, and same-instance log parsing when necessary.

This path currently targets single-step trials. Explicit `extra_env` values are
passed to the adapter; these may include scoped model credentials. Adapter
reported usage is not independently verified by the research model broker.
An abruptly killed worker cannot finish its parser; missing usage must remain
unknown. Legacy adapter selection continues to execute on the host.


#### Operators in an isolated controller session

Operator invocations detect the persisted controller sandbox configuration and
run inside the same image. They receive copies of candidate target/operator files,
operator source, public archive data, surface rules and prior operator artifacts.
They receive neither the original Git repository nor the evaluator directory.
The image must contain Python and dependencies needed by those operators.

The host accepts only declared mutable-surface changes and the invoked stage's
result files. Invalid paths and symlinks reject the return before candidate edits
are applied; executable bits are preserved. Reflection can append validated
operations to the existing insights file. Host action/evaluation receipts cannot
be replaced through operator outputs.

This operator transport is currently offline. Model-based mutation, task rollout
and operators requiring repository history need additional explicit services or
public inputs; they do not silently fall back to host execution. Offline mutation
usage is recorded as zero by the host. Operator data import is not transactional;
an interrupted import requires inspection before retrying.

Operator feedback is an explicit development projection. Archive views retain
candidate identities, aggregate scores, costs and host-certified parent choices;
they omit task vectors, free-text diagnostics and sealed evaluation entries.
Prior observation files are restricted to numeric summary metrics and the gate
decision. Nested task jobs and verifier logs are not copied. Each operator attempt
retains a host-side feedback-exposure receipt; these development observations are
not independent holdout evidence. Richer training feedback requires a separately
authorized projection rather than copying an entire job directory.

#### Codex controller with file transport

Place `scripts/codex_agent_controller.py` in the method bundle as `controller.py`,
alongside `instructions.md`. Select a local immutable image containing Python and
the intended Codex CLI version, and configure the controller arguments as:

```text
/input/optimizer/controller.py --isolated-input /input --require-version VERSION
```

Pass model and reasoning choices explicitly with repeated `--codex-arg` options.
The wrapper configures a Responses provider pointing at the local model bridge,
disables provider request/stream retries, and uses a separate persisted Codex home
for each method activation. Its instructions describe the action/edits file
protocol and persistent notes. The outer Docker boundary supplies isolation;
Codex can execute tools within that container without an interactive approval.
Host billing still comes from the trusted model handler, not CLI token reports.
The provider settings follow the [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
