---
name: evolve-agent
description: "Run evidence-driven evolution of agents, prompts, skills, and agent harnesses. Use when asked to initialize or operate an evolution workspace, choose Hill Climb, A-Evolve, GEPA, AHE, or HyperAgents, let an outer Agent adapt the evolution process, invoke operators directly, improve a candidate through repeated evaluation, recover interrupted evolution, or report an evidence-backed champion."
---

# Build an evidence chain

Treat evolution as an evidence chain:

```text
contract → baseline → evidence → hypothesis → candidate → evaluation → lineage
```

A higher score alone is insufficient. Link every candidate to the evidence that
motivated it, its exact snapshot, frozen evaluation, and lineage decision.

## 1. Establish the contract

Detect whether the current directory is an initialized evolution workspace.

- In a workspace, read `AGENTS.md`, `evolve.yaml`, `program.md`, then run
  `./evolve status .` and `./evolve verify .`.
- For a new experiment, identify the target, mutable surface, evaluator, data
  partitions, budget, and execution boundary before initialization.
- Read [the workspace contract](references/workspace-contract.md) before
  creating, operating, recovering, or interpreting a workspace. Its "Create a
  workspace" section gives the initialization and baseline-certification
  commands and the preconditions they enforce.

**Completion check:** Name the target, mutable surface, frozen evaluator (the
`evaluator/` contract that scores every candidate), data
partitions, candidate budget, and execution boundary. In an existing workspace,
also identify the current champion, next generation, and interrupted state.

## 2. Choose a method and control path

The initialized operators define the starting method. The control path may be
driver-led, where `evolve run` fixes the lifecycle, or agent-led, where the
outer Agent chooses which direct capabilities to invoke and may change the
active process when the surface permits it. For a new experiment, GEPA is the
default; choose Hill Climb when the experiment needs the simplest attributable
control. Match the method and control path to the research question, available
evidence, and mutable surface. Read only the relevant method card.

| Observable condition | Method | Read |
| --- | --- | --- |
| A minimal attributable control is enough | Hill Climb | [hill-climb.md](references/hill-climb.md) |
| Behavioral traces or generated-artifact rubrics should guide prompt or skill mutation | A-Evolve | [a-evolve.md](references/a-evolve.md) |
| The evaluator returns per-task results and the target splits into components | GEPA | [gepa.md](references/gepa.md) |
| Failures are execution-shaped and justify harness changes | AHE | [ahe.md](references/ahe.md) |
| The evolution process itself may also change | HyperAgents | [hyperagents.md](references/hyperagents.md) |

When the outer Agent, rather than a configured mutate stage, should decide the
sequence of investigation, operator calls, edits, retries, and stopping, read
[agent-driven control](references/agent-driven.md). This is an experimental
control path over existing workspace capabilities, not a new operator method.

Read [scientific foundations](references/scientific-foundations.md) only when
defining or changing evaluator semantics, partitions, acceptance rules, or
research claims.

**Completion check:** State why the method and control path match the evidence
and declared mutable surface. If they do not, choose again before running.

For artifact-producing Skills, prefer replaying a selected parent's certified
artifacts over executing that parent again. Re-execute the parent only when the
current task set, evaluator identity, runtime identity, or required artifacts
do not match the retained evidence. Execute every child freshly.

## 3. Author reusable operators in a source checkout

Use the library when creating a reusable policy, not an edit to one already
initialized workspace. Discover available entries, then scaffold and verify one
operator before selecting it from a recipe:

```bash
uv run --frozen evolve operator list [stage]
uv run --frozen evolve operator new mutate <name>
uv run --frozen evolve operator describe mutate/<name>
uv run --frozen evolve operator check mutate/<name> --config '{"attempts": 3}'
uv run --frozen evolve recipe check <recipe-path>
```

`new` writes exactly one entry at `library/mutate/<name>.py`. Implement the
generated `MutateOperator`, keep `validate_config`, and use
`sdk.main(..., validate_config=validate_config)`. A recipe selects it with an
`operator:` value and nested `config:` mapping:

```yaml
operators:
  mutate:
    operator: critic_editor
    timeout_s: 3600
    config:
      attempts: 3
```

Do not put a reusable implementation beside a recipe or alter a library entry
to change a running workspace. Run recipe check before initialization; a new
workspace freezes the selected source. Existing workspaces retain their own
frozen active operators.

**Completion check:** The operator is in the central library, its configuration
passes `operator check`, the recipe passes `recipe check`, and the source change
is separated from any initialized workspace it does not retroactively alter.

## 4. Prefer capabilities over source

For agent-led evolution, start from the stable workspace interface:

```bash
./evolve operator active . --json
./evolve operator run . <stage> --genid <id> [stage arguments]
```

Treat `operator active --json` as the live authority for which stages are
configured and whether their access is `direct`, `driver`, or `finalize`.
Invoke configured direct operators, read their retained artifacts under
`runs/gen-<id>/`, and make the candidate change yourself.

Escalate progressively:

1. Tune one call with `--config` when the capability is right but its bounds are
   wrong.
2. Read `PROTOCOL.md`, operator guidance, or `operators/README.md` when an input
   or artifact is unclear.
3. Read the active `operators/<stage>.py` only to diagnose behavior or change
   the active evolution process.
4. Read `library/<stage>/` only to compare or adapt another implementation.

Do not read implementation source merely to invoke a working operator. Do not
edit `library/` and assume runtime behavior changed; active code lives under
`operators/`.

Use the configured driver when its mutation stage should own the edit and an
unattended run is desired:

```bash
./evolve run . --max-generations 1
```

Driver and agent-led paths share the same evaluation and lineage mechanism. Do
not run them concurrently. Ordinary agent-led work should close one generation
through the stable commands below. For an explicitly Agent Driven experiment,
the outer Agent may adapt its action sequence under the Agent Driven control
reference; it must still use the mechanism for candidate identity, evaluation,
and finalization.

**Completion check:** Choose exactly one control path for the active work. For
agent-led evolution, name the available direct operators, hard budget, and the
evidence supporting the next action; source inspection must have a concrete
reason.

## 5. Close the loop

1. Establish and inspect the certified baseline.
2. Select a parent and retain the method's required evidence.
3. State one evidence-linked hypothesis and predicted effect.
4. Produce one candidate inside the declared mutable surface.
5. Run every configured admission check against the final candidate snapshot.
6. Evaluate and finalize through the workspace mechanism.
7. Verify lineage before beginning another generation.

**Completion check:** The candidate has an exact lineage identity; required
admission decisions and evaluator-stamped results exist; lineage verification
passes; accepted and rejected outcomes remain auditable.

## 6. Report only what the chain proves

Start from `./evolve report .`, which writes the experiment report and
research-claim checklist from stamped records. Around it, report the baseline,
champion, parent-child changes, accepted and rejected mutations, evaluation
scope, retained evidence, and limitations. Tie every quality claim to
evaluator-stamped artifacts from the run.

**Completion check:** Every score and champion identity is derivable from
trusted lineage records, and every generalization claim names its data
partition.

## Guard the chain

- Keep one evaluator and runtime identity within an experiment. Start a new
  experiment when the evaluator changes.
- Take scores and champion state only from mechanism-owned stamped records.
- Keep optimization, gate, and sealed task identities disjoint.
- Change only the declared mutable surface.
- Treat linked worktrees outside `runs/worktrees/` as user-owned. Report them;
  never remove or modify them without explicit authorization.
- Match the execution boundary to candidate trust.
- Keep credentials out of prompts, artifacts, and reports.
- Spend live evaluation budget only when the request authorizes execution.

## Historical-workspace note

Older initialized workspaces retain the stage files and configuration frozen at
creation time. Treat those as historical metadata only; start a new workspace
to use the current operator model.
