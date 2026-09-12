# RSIHub Operator Protocol

This document describes protocol version `1` for workspace operators. The
mechanism launches operator files as subprocesses and consumes files from the
run directory. The Python ABCs and `sdk.main(...)` are the supported authoring
path, but the file contract is the protocol.

## Subprocess Contract

The mechanism invokes required operator files for select, rollout, mutate,
gate, and record, plus recipe-selected optional operators such as
analyze, validate, novelty, and reflect. A copied file may be a library
operator or a user-provided `script:`. Named Python operators end with
`sdk.main(OperatorClass, config_schema=CONFIG)` so discovery-time
inspection can describe the entry and validate configuration. Any explicit
script that honors the same files is valid, but script bindings are
non-portable filesystem dependencies.

Runtime state arrives through environment variables:

- `EVOLVE_WORKSPACE`: workspace root containing `archive.jsonl`, config, and
  protocol marker files.
- `EVOLVE_CHECKOUT`: checkout being evaluated or mutated.
- `EVOLVE_RUN_DIR`: current generation run directory.
- `EVOLVE_GENID`: child generation id.
- `EVOLVE_PARENT`: selected parent id, when there is one.
- `EVOLVE_ROUND`: numeric round when the runner is executing a per-round
  operator. The variable is absent when no round applies.
- `EVOLVE_STAGE_TIMEOUT_S`: the recipe-selected stage timeout. The SDK exposes
  the same value as `OperatorContext.timeout_s`.
- `EVOLVE_OPERATOR_TIMEOUT_S`: the outer subprocess deadline. It normally
  matches the stage timeout; adapters with bounded per-attempt retries may
  receive a larger framework-calculated deadline.

Operator-specific YAML settings are passed as `--config` JSON. The framework
enforces the configured timeout around the subprocess. A nonzero exit code, a
timeout, or malformed required output makes the generation an
`operator_failed` row; the row names the operator kind and includes a note about
the failed file or field when validation reaches a file read.

`sdk.main(...)` checks `.evolve-protocol-version` from `EVOLVE_WORKSPACE` or
`EVOLVE_CHECKOUT`. Protocol version `1` is accepted; a missing marker or
another value exits nonzero with `protocol_version` in stderr.

## Per-Kind Contract

All Python operators receive an `OperatorContext` with these fields: `workspace`,
`checkout`, `run_dir`, `genid`, `parent`, `round`, `fan_out`, `config`, `rng`,
and `timeout_s`. `workspace`, `checkout`, and `run_dir` are `Path`
values. `config` is only the opaque nested recipe `config` dict, `rng` is
seeded from that config, and `timeout_s` is the selected stage budget.

### Select

ABC signature:

```python
def pick(self, archive: ArchiveView, ctx) -> SelectResult:
```

Implement `pick`. Return `SelectResult` with field `parents`. The subprocess
writes `parents.json`:

```json
{"parents": ["0"]}
```

`parents` must be a non-empty list of generation id strings.

### Rollout

ABC signature:

```python
def rollout(self, checkout: Path, ctx) -> RolloutResult:
```

Implement `rollout`. Return `RolloutResult` with fields `summary` and
`artifacts`. The subprocess writes `rollout/summary.json` and
`rollout/artifacts.json`. `summary` is a JSON object; `artifacts` is a list of
artifact paths or labels.

### Analyze

ABC signature:

```python
def analyze(self, checkout: Path, ctx) -> AnalyzeResult:
```

Implement `analyze`. Read method-neutral rollout artifacts such as
`rollout/cases.json`, then write the selected and raw trace views under
`analyze/`. Return `AnalyzeResult` with `summary` and `artifacts`;
the subprocess writes `analyze/summary.json` and
`analyze/artifacts.json`.

After analysis, the mechanism writes the normalized feedback bundle under
`runs/gen-<id>/feedback/` for the mutation operator to read. If the analyzer writes
`analyze/feedback.md` and `analyze/evidence/selected.md`, the
mechanism copies the bounded selection into the feedback bundle.

### Mutate

ABC signature:

```python
def mutate(self, checkout: Path, observation: str, ctx) -> MutateResult:
```

Implement `mutate`. Return `MutateResult` with fields `changed`, `notes`, and
`usage`. The subprocess writes `mutate/changed.json`, may write
`mutate/rationale.md`, and writes `mutate/usage.json`. `usage` is a JSON
object, commonly including `usd`.

The workspace also exposes gitignored durable storage under `artifacts/`.
`artifacts/user/` is user-managed, while the mutation operator may persist arbitrary
files only under `artifacts/generations/<EVOLVE_GENID>/`. An optional free-form
`handoff.md` in that directory is the handoff convention. Shipped prompts point
to the selected parent's handoff when it exists; absence is non-fatal. Harbor
copies all durable artifacts into its disposable workspace but imports only the
current generation namespace, so user and prior-generation content remains
host-authoritative and artifacts do not enter candidate patches.

### Novelty (optional)

ABC signature:

```python
def assess(self, checkout: Path, ctx) -> NoveltyResult:
```

Implement `assess`. Return `NoveltyResult` with fields `novelty` (0.0–1.0) and
`accept`. The subprocess writes `novelty.json`. Runs only when a recipe
configures `operators.novelty` (DESIGN §8, off by default): it sees the
uncommitted mutation diff, and an `accept: false` discards the generation before
eval — a near-duplicate is never committed. `NoveltyOperator` implementations
should read the ledger for prior accepted diffs, not reach for policy helpers.

### Reflect (optional)

ABC signature:

```python
def reflect(self, archive, ctx) -> ReflectResult:
```

Implement `reflect`. Return `ReflectResult` with field `ops`. A configured
credit-reflection variant may consume optional `verified_fixes` annotations;
strategies such as AHE that do not emit predictions simply produce no credit.

### Gate

ABC signature:

```python
def decide(self, child: Row, parent: Row | None, ctx) -> GateResult:
```

Implement `decide`. Return `GateResult` with fields `decision` and `reason`.
`decision` is `accept` or `reject`. The subprocess writes `gate.json` with
`valid_parent`, `verdict`, and `reason`; `accept` maps to `valid_parent: true`
and `verdict: keep`, while `reject` maps to `valid_parent: false` and
`verdict: discard`. Contradictory pairs are rejected as malformed output.

For non-Python operators, the runner writes `gate/input.json` before launching
the gate subprocess:

```json
{"child": {"genid": "1", "task_set_hash": "hash"}, "parent": null}
```

`child` is the merged archive row for `EVOLVE_GENID`. `parent` is the selected
parent row only when that parent has a score for the child's `task_set_hash`;
otherwise it is `null`. Python operators using `sdk.main(...)` receive these
same values as `decide(child, parent, ctx)`.

### Record

ABC signature:

```python
def annotate(self, child: Row, ctx) -> RecordResult:
```

Implement `annotate`. Return `RecordResult` with field `fields`. The subprocess
writes `record/fields.json`. The driver strips mechanism-owned fields before
appending the remaining object to the archive.

## Evaluation Contracts and Receipts

A strict evaluation attempt contains `evaluation-contract.json`. The mechanism
generates it atomically before candidate runtime preparation; operator code and
experiment scripts do not populate its fields. Its `contract_id` hashes the
canonical immutable inputs, including candidate and evaluator Git identities,
selected task content, repetitions, runtime, model, concurrency, retry policy,
and framework version.

`EvaluationRecord` remains the evaluation-level receipt and may include the
contract ID, a workspace-relative contract artifact reference, and whether
observed runtime evidence certified the contract. A strict candidate runtime
receipt binds the same contract ID, candidate commit, dependency digest, and
resolved runtime digest. Any mismatch is evaluator infrastructure failure and
prevents benchmark launch. Legacy receipts retain their historical schema and
do not gain a fabricated strict runtime identity.

## Evaluation Diagnostics

Strict receipts include diagnostics derived automatically from canonical
`TrialResult` evidence and the immutable contract. They record expected,
observed, scoreable, and missing trial counts; outcome and owner counts; bounded
safe failure categories; retry eligibility; and hashed workspace-relative
artifact references.

Missing contract identities are materialized as evaluator-owned `missing`
results with a null reward. Infrastructure results also keep a null reward.
Diagnostics never contain raw exception messages, verifier output, absolute
host paths, credentials, proxy values, or private expected answers.
Candidate-owned failures are actionable only for candidate/genesis purposes;
anchor and sealed evidence remains non-actionable. The frozen SDK exposes the
receipt-validated projection through `evaluation_diagnostics(workspace,
genid)`, and reports render coverage and certification independently from score.

## Write Your Own Named Operator

1. Run `evolve operator new <stage> <name>` in a source checkout.
2. Implement the stage method and return its result dataclass.
3. Declare accepted fields with `evolve.frozen.config.Config` and pass the
   declaration to `sdk.main(..., config_schema=CONFIG)`.
4. Run `evolve operator describe <stage>/<name>` and
   `evolve operator check <stage>/<name> --config '<json>'`.
5. Select it under `operator:` with all settings under `config`, then run
   `evolve recipe check` before initialization.

Underscore-prefixed modules are helpers that discovery excludes, so helper
refactors do not create accidental public operators. Put generic helpers in
`library/_shared/`, cross-stage helpers private to one method in
`library/_methods_shared/<method>/`, and stage-private helpers in paths such as
`library/mutate/_support/`. Generic helpers are always materialized; a method
bundle is materialized only when selected code imports it. Shared local and
Harbor runners live under `library/_shared/runners/`.

`Config` supports strings, integers, finite numbers, booleans, arrays, objects,
arbitrary JSON values, required/default/optional fields, choices, bounds, and
descriptions. Use `Config.extend` for shared fragments. Use `custom` only to
normalize one field and `refine` only to validate a relationship between fields.

## Shipped Operators

The shipped library uses canonical algorithm names only. Named recipe bindings
point to these entry files:

- select: `ahe_latest`, `greedy`, `newest`, `pareto`, `random`,
  `score_child_prop`, `score_weighted`
- rollout: `failure_focused`, `harbor`, `noop`, `parent_evaluation`
- analyze: `ahe`, `artifact_rubric`, `failure_patterns`, `gepa`,
  `trace_browser`, `trajectory_only`
- mutate: `aevolve`, `ahe`, `gepa`, `hyperagents` (`runner`: `local` or `harbor`)
- validate: `hyperagents`, `minibatch_improvement`, `node_check`
- novelty: `accept_all`, `diff_similarity`
- gate: `ahe_artifact_valid`, `hillclimb`, `parent_eligible`
- record: `gepa`, `hyperagents`, `jsonl`
- reflect: `credit`

## Stability Tiers

| Tier | Stability | What to depend on |
| --- | --- | --- |
| Verbs and file contract | Major-version protected | CLI verbs, subprocess execution, required files, JSON fields |
| Interfaces and SDK | Additive-only paved road | ABC names, result dataclasses, `OperatorContext`, `sdk.main(...)` |
| Library operators | Evolve freely | Algorithms under `library/<kind>/` may change as practice improves |
| Recipes | Examples, no promise | Presets encode experiment policy and can change as evidence changes |

## Escape-Hatch Ladder

1. YAML: choose a shipped `operator:` and tune its nested `config`.
2. `script:` escape hatch: point one stage at your own executable file; this
   remains runnable but is non-portable.
3. Agent orchestration: discover capabilities with `evolve operator list`, run
   configured stages with `evolve operator run`, edit a child worktree, then
   use `commit`, `eval`, and `finalize`.
4. Own driver from evolve verbs: use `evolve init`, archive files, evaluator
   outputs, and surface checks while orchestrating the loop yourself. Stamping
   and canonical evaluation live in the verbs, so those invariants survive a
   user-written driver.

Direct stage reruns retain the previous output under
`runs/gen-<id>/operator-attempts/<kind>/attempt-<n>/`. A configured validate or
novelty result includes a mechanism-owned candidate-tree receipt; the
Agent-facing commit refuses missing, rejected, or stale admission results.

## Files Are Normative

Files, not classes, are normative. The mechanism runs subprocess files and
consumes `parents.json`, `rollout/summary.json`, `rollout/artifacts.json`,
`analyze/summary.json`, `analyze/artifacts.json`,
`mutate/usage.json`, `gate.json`,
`record/fields.json`, and the other artifacts listed above. Non-Python
operators are valid when they honor those files, environment variables, exit
behavior, and protocol version expectations.
