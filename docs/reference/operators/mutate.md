# Mutate

`mutate` converts observations into candidate edits. It is required and may
change only paths permitted by both `editable_roots` and the recipe's mutable
surface.

## Contract

```python
class MutateOperator:
    def mutate(self, checkout, observation, ctx) -> MutateResult: ...
```

The result contains changed paths, notes, and usage.

## Library operators

| Operator | Mutation strategy |
| --- | --- |
| `aevolve` | distill recent task observations into prompt, skill, memory, or tool updates |
| `ahe` | make one testable agent-harness change from current evidence |
| `gepa` | reflect on configured components and propose a component edit |
| `hyperagents` | self-referential mutation that may co-evolve target and selected operator policy |

## Runner configuration

`operator` chooses the mutation strategy. `config.runner` chooses how its editing agent
is launched.

```yaml
operators:
  mutate:
    operator: gepa
    timeout_s: 3600
    config:
      runner: harbor
      expose_gate_data: false
      agent: codex
      model: gpt-5.4
      environment: docker
      image: evolve-mutate-codex:20260904-codex0149
      editable_roots: [target]
      max_retries: 1
```

- `runner: local` executes a trusted host command.
- `runner: harbor` creates an isolated writable copy and transactionally
  imports allowed returned files.
- `editable_roots` does not expand `surface.include`.
- `expose_gate_data` defaults to `false`; keep it false for disjoint mutation
  and protected evaluation.

Operator-specific path keys include `components`, `prompt_path`, `skills_dir`,
and `memory_dir`.

## Artifacts

Standard outputs include:

```text
runs/gen-N/mutate/changed.json
runs/gen-N/mutate/patch.diff
runs/gen-N/mutate/surface-check.json
runs/gen-N/mutate/rationale.md
runs/gen-N/mutate/usage.json
```

Harbor runs additionally retain prompt, command, trial, artifact manifest,
jobs, and tasks under `runs/gen-N/mutate/harbor/`.

See [Mutate operator execution](../../guides/mutate-operators.md) for runner semantics,
artifact handoffs, authentication, and custom Harbor adapters.
