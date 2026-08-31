# RSIHub design

This document describes the framework model and rationale. The executable
module inventory lives in
[ARCHITECTURE.md on GitHub](https://github.com/simple-agent-lab/RSIHub/blob/main/ARCHITECTURE.md);
the operator contract in `src/evolve/frozen/interfaces.py` is authoritative for
interfaces.

## The model

RSIHub evolves a candidate under a frozen evaluator while retaining a Git
lineage. A workspace is a separate Git repository: generation tags identify
candidates, `archive.jsonl` records stamped outcomes, and the evaluator stays
outside the candidate's mutable surface.

The governing boundary is simple: framework code owns the mechanics that make
scores trustworthy; a recipe selects the evolvable policy. Operators run as
subprocesses, so the mechanism never imports workspace operator code in-process.

A **stage** is a fixed lifecycle slot. An **operator** is a reusable
implementation at `library/<stage>/<name>.py`. A **recipe** is code-free
selection and configuration of operators. **Evaluate** is the framework-owned
trusted mechanism and is never resolved from the operator library.

At the start of `evolve run`, the mechanism evaluates the untouched `gen/0`
seed on the configured primary split (normally `gate`) and then evaluates the
same snapshot on the complete non-empty `sealed` split. The sealed result is a
non-selectable `anchor`, is stored as auxiliary evidence, and is excluded from
the mutation feedback projection. Generation one cannot begin until both
required evaluations complete.

## Recipe-driven initialization

`evolve init` resolves one supported recipe YAML, validates every named
operator in a subprocess, and freezes its normalized configuration. The recipe
is the selection authority; adding `library/<stage>/<name>.py` makes an
operator discoverable without editing a registry.

```text
recipe YAML
  -> target seed resolution
  -> common workspace scaffold
  -> evaluator-engine scaffold
  -> rendered config and component manifest
  -> vendored framework runtime
  -> generation-zero Git snapshot
```

The framework ships `aevolve`, `ahe`, `ahe_codex`, `gepa`, `gepa_local`,
`hill_climb`, `hill_climb_codex`, `hyperagents`, `hyperagents_codex`, and
`hyperagents_dsh`.
Development smoke recipes live under `tests/fixtures/recipes/` and are not part
of the public recipe inventory.

## Declarative operator configuration

An operator owns one declarative `Config` schema. The schema is the single
source of truth for accepted fields, required inputs, defaults, constraints,
descriptions, normalization, and inspection. Operators receive the normalized
result as the existing JSON-compatible `ctx.config` dictionary; configuration
classes never enter the runtime operator interface.

The design has two governing principles:

1. The framework guarantees every required input and normalized output shape,
   while operators retain an explicit escape hatch for genuinely custom JSON
   fields and cross-field constraints.
2. The declaration and implementation stay small and readable. RSIHub does
   not implement the full JSON Schema standard or add a second configuration
   framework.

The framework-owned, dependency-free vocabulary lives in
`evolve.frozen.config` because it is part of the frozen operator authoring
contract. A typical operator declares:

```python
from evolve.frozen.config import Config, array, integer, string

CONFIG = Config(
    {
        "strategy": string(
            default="round_robin",
            choices=("round_robin", "all"),
            description="How components are selected.",
        ),
        "max_examples": integer(
            default=10,
            minimum=1,
            description="Maximum examples to include.",
        ),
        "required_placeholders": array(
            string(),
            default=[],
            description="Placeholders that edits must preserve.",
        ),
    }
)
```

The operator exposes it through one SDK entrypoint:

```python
sdk.main(MyOperator, config_schema=CONFIG)
```

Field presence is explicit: `required=True` requires an input, `default=...`
emits a value when absent, and a field with neither is optional and remains
absent. Top-level unknown fields reject by default. Nested objects choose their
own `additional_properties` policy. Shared declarations compose through
`Config.extend(...)`; duplicate names reject instead of silently overriding a
shared field. Defaults are validated when the schema is built and copied when
used, so mutable JSON defaults are never shared between normalizations.

The initial vocabulary is deliberately closed:

- `string`, `integer`, `number`, and `boolean`;
- `array`, `object`, and unrestricted JSON values;
- required fields and JSON-compatible defaults;
- choices, numeric bounds, and descriptions;
- schema composition and nested additional-property policy;
- one validation-only `refine` callback for cross-field constraints; and
- one explicitly described custom field normalizer for legacy or otherwise
  irreducible value shapes.

There are no metaclasses, configuration inheritance hierarchy, automatic
dependency injection, or promise of complete JSON Schema compatibility.
Exceptional callbacks stay local: `refine` may report violations but may not
rewrite the normalized mapping, while a custom field normalizer may transform
only its own value. The engine still verifies that every custom result is valid
JSON and requires the field to publish an input description for inspection.

Configuration flows through the existing subprocess boundary:

```text
recipe YAML config
  -> operator subprocess --validate-config
  -> Config.normalize(raw)
  -> normalized JSON object
  -> resolved recipe and workspace evolve.yaml
  -> ctx.config dictionary
```

Independent field violations are collected and rendered in deterministic path
order. Messages identify the field and expected contract without echoing raw
values, which may contain credentials. Invalid schema declarations, including
contradictory required/default settings and non-JSON defaults, fail during
operator inspection as authoring errors.

`--describe` exports the same declaration as a JSON-compatible description for
CLI and tooling use. The export resembles the useful subset of JSON Schema but
is an RSIHub contract. Operator identity still comes from
`library/<stage>/<name>.py`, prose metadata comes from its docstring, and stage
output remains governed by the frozen result validators. Configuration does
not absorb metadata, execution, lifecycle, or output validation.

This refactor does not change the shape or meaning of any valid recipe YAML.
Existing field names, defaults, and normalized values remain exact. All shipped
operators migrate to `config_schema`; the procedural `validate_config` SDK path,
per-operator allowed-key sets, and unused parsing helpers are removed rather
than retained as a parallel system. Operators with no settings declare
`Config({})`.

Tests enforce the boundary at four levels: schema primitives and error paths;
real subprocess inspection; a catalog requirement that every shipped operator
has a valid schema; and exact before/after normalized configuration equality
for every supported recipe. Initialization and runtime acceptance tests confirm
that workspaces freeze the same dictionaries and operator behavior remains
unchanged.

## Source ownership

| Source | Responsibility |
| --- | --- |
| `recipes/` | supported experiment configurations |
| `scaffolds/workspace/` | files common to every generated workspace |
| `scaffolds/evaluators/harbor/` | Harbor-specific evaluator files |
| `seeds/` | built-in evolvable target content |
| `src/evolve/integrations/harbor/` | Harbor adapters owned by the framework |
| `library/` | discoverable, reusable operator implementations |
| `tests/fixtures/` | deterministic test-only resources |

Harbor integrations are framework modules. A generated workspace vendors them
as part of `.evolve/evolve/integrations/harbor/`; it does not receive standalone
adapter packages.

## Workspace boundaries

```text
<workspace>/
├─ target/          candidate selected by the recipe's seed
├─ operators/       frozen active recipe-selected operator scripts
├─ library/         frozen runtime helpers imported by selected library operators
├─ evaluator/       frozen evaluator and selected engine files
├─ skills/          workspace operating manual
├─ .evolve/         vendored framework runtime and launcher
├─ evolve.yaml      rendered experiment configuration
├─ .evolve-components.json
├─ archive.jsonl    append-only lineage ledger
└─ runs/, artifacts/  generated run state and durable context
```

Initialization records normalized operator config in `evolve.yaml`, freezes
selected bytes in `operators/`, and records source identity and SHA-256
provenance in `.evolve-components.json`. Existing initialized workspaces are
never rewritten when the source catalog changes. The mutable surface in
`evolve.yaml` controls what a candidate may edit; the evaluator, archive stamps,
and vendored mechanism remain outside it.

## Invariants

1. The evaluator is frozen for the lineage and cannot be mutated by a candidate.
2. Scores enter the archive only through the mechanism's stamped evaluation path.
3. Reports recompute best-known results from stamped archive entries.
4. A local Harbor dataset is frozen by task name and by deterministic task-tree
   digests (paths, file bytes, file types, and modes) at initialization. Each
   canonical run executes a fresh selected-task snapshot verified against those
   digests, never the mutable source directory that was checked earlier.
5. A selectable score is bound to the commit currently named by its `gen/<id>`
   tag; moving the tag invalidates the score instead of transferring it.
6. Candidate dependency preparation uses an immutable shared seed plus a
   disposable per-attempt overlay. Candidate project build code runs only in
   the evaluator environment, not on the host preparation path.
7. Evaluation replay verifies every indexed artifact's path, size, and digest,
   then collects cases from a temporary view containing only those certified
   bytes.
8. A candidate enters the lineage only through canonical evaluation.

Resolved version-1 split manifests remain readable for historical inspection,
but they are not eligible for new canonical evaluation or parent selection
because they do not contain task-content identities. Start a new experiment to
upgrade that boundary; do not silently compare new scores with a legacy task
set.

## Versioning

The repository source is the single framework source of truth. Vendoring it
into `.evolve/` deploys that source with a generated workspace; it does not
create a second implementation lineage. Changes to required operator interfaces
are made through the frozen interface contract and its tests.
