# RSIHub

Evidence-driven evolution of agents, prompts, and agent harnesses. Each
experiment is one Git repository (a workspace) in which a frozen evaluator
scores successive candidates and a mechanism records their lineage. This
glossary is the single ubiquitous language for the framework repository, the
skill, and every generated workspace.

## The experiment

**Workspace**:
A Git repository holding one evolution experiment: its target, evaluator,
operators, and lineage.
_Avoid_: experiment repo, evolution directory

**Target**:
The artifact under evolution, at `target/`.
_Avoid_: subject, candidate (a candidate is a snapshot, not the artifact)

**Seed**:
The initial content vendored into `target/` at initialization.

**Scaffold**:
Framework-owned files used to generate a workspace around the selected target
and evaluator engine.

**Integration**:
Framework-owned runtime behavior for an external system. Harbor integrations
live under `src/evolve/integrations/harbor/` and are vendored with the
mechanism.

**Evaluator**:
The frozen scoring contract at `evaluator/`. Fixed from generation zero; the
only source of scores.
_Avoid_: ruler, scoring contract, canonical evaluator, frozen side

**Evaluate**:
The framework-owned trusted mechanism that scores an exact candidate snapshot.
It is a fixed lifecycle action, not a recipe-selected operator.

**Mutable surface**:
The set of paths a candidate change may touch, declared under `surface` in
`evolve.yaml`.
_Avoid_: declared surface, mutation surface, mutation scope, write scope

**Recipe**:
A code-free init-time selection and configuration of operators, target,
surface, and evaluator for one method (`aevolve`, `ahe`, `gepa`,
`hill_climb`, `hyperagents`).

**Supported recipe**:
A public configuration under `recipes/`. Development-only configurations under
`tests/fixtures/recipes/` are test fixtures, not supported recipes.

**Test fixture**:
Deterministic test-only data under `tests/fixtures/`; never part of the public
recipe or seed inventory.

**Method**:
The evolution strategy a workspace runs (Hill Climb, A-Evolve, GEPA, AHE,
HyperAgents), expressed as a configuration of stages over the same workspace
contract.
_Avoid_: algorithm, mode

## Candidates and lineage

**Candidate**:
One exact snapshot of the workspace tree proposed for evaluation.
_Avoid_: version, variant

**Generation**:
A candidate's position in the lineage, numbered and tagged `gen/<id>`.

**Baseline**:
The certified generation-zero primary evaluation of the untouched seed.
Recorded in the archive with purpose `genesis`. A separate non-selectable
generation-zero `anchor` evaluates the same seed on the sealed split and is not
mutation feedback.
_Avoid_: genesis (in prose)

**Parent**:
The certified candidate a new child starts from.

**Child**:
A candidate under construction in a worktree, not yet committed.
_Avoid_: draft, work-in-progress candidate

**Champion**:
The best accepted candidate, recomputed by the mechanism and recorded in
`best_ever.json`.
_Avoid_: best-ever (in prose), winner

**Lineage**:
The parent-child graph of all candidates, carried by Git tags and the archive.
_Avoid_: history, genealogy

**Archive**:
The append-only record `archive.jsonl`, one row per generation event.
_Avoid_: ledger

**Stamp**:
The evaluation record written only by the frozen side; the sole way a score
enters the lineage.
_Avoid_: self-reported score, result

## Control

**Mechanism**:
The frozen framework code behind `./evolve` that owns state transitions,
stamping, gating, and recording.
_Avoid_: framework side, lineage mechanism, workspace mechanism, evaluation
mechanism

**Console**:
The vendored `./evolve` entry point inside a workspace; the only supported way
to invoke the mechanism.
_Avoid_: CLI (for the workspace entry point)

**Outer agent**:
The coding agent operating a workspace from outside during an agent-led
generation.
_Avoid_: mutate operator (for the outer agent)

**Mutate operator** (`mutate`):
The configured stage that edits the child from inside the loop during a
driver-led generation. The outer agent plays this same mutating role from
outside during an agent-led generation; the name `mutate` stays with the
stage and its files.
_Avoid_: outer agent (for the configured mutate operator)

**Driver**:
The unattended loop started by `evolve run`.
_Avoid_: built-in loop, unattended loop (as a name)

**Control path**:
Who owns producing a generation: driver-led (the configured `mutate`
stage) or agent-led (the outer agent). Both are the same role — an agent
mutating the target — which is why exactly one may own a generation.

**Agent Driven session**:
A bounded agent-led control path in which the outer agent chooses one typed
action at a time and RSIHub executes and receipts it. The session may alter
permitted target and process files. Its action API excludes sealed evaluation
and mechanism-owned scoring and lineage. This is a trusted-controller control
contract, not an OS security boundary; untrusted controllers require process or
container isolation.

## Stages and decisions

**Stage**:
One fixed lifecycle slot, named exactly as registered: `select`, `rollout`,
`analyze`, `mutate`, `validate`, `novelty`, `gate`, `record`,
`reflect`.
_Avoid_: feedback, mutation, trace analysis, validation (as stage names)

**Operator**:
A reusable stage implementation at `library/<stage>/<name>.py`. A recipe
selects one implementation; initialization freezes it at
`operators/<stage>.py` with normalized config and provenance.
_Avoid_: stage (for an implementation), plugin, component

**Admission**:
The pre-evaluation checks bound to an exact candidate tree: `surface-check`
plus every configured `validate` and `novelty` stage. Each produces a receipt;
editing the tree invalidates them.
_Avoid_: gate (for pre-evaluation checks)

**Gate**:
The post-evaluation accept-or-reject decision, applied only by `finalize`.
_Avoid_: admission (for the accept decision)
