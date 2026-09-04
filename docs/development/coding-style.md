# coding-style.md — how we write

Binding ethos for code and prose. Architecture (where things live, what is
frozen, operator contracts) lives in [the design guide](../concepts/design.md)
and the
[source architecture map](https://github.com/simple-agent-lab/RSIHub/blob/main/docs/ARCHITECTURE.md).
This file is only *how* we write.

Throughline: **don't trust discipline — build the constraint.** Prefer a rule a
machine can check over a habit a human must remember.

---

## Lint and format (ruff)

CI blocks on both. Locally, before you push:

```bash
uv run ruff check .
uv run ruff format .
```

- **ruff is the formatter and the linter** — do not hand-format to a different
  style, and do not add a second formatter (black, isort, …). Config lives in
  `pyproject.toml` (`[tool.ruff]`).
- `ruff check` must be clean (`F`/`E`/`W`/`I`/`UP`/`B` as selected there). Fix
  with `uv run ruff check --fix .` when the finding is auto-fixable.
- `ruff format` must be clean. If it would reformat a file, run it — never
  commit against the formatter.
- Prefer deleting an unused import over `# noqa`. Reach for `noqa` only when the
  ignore is intentional and local; repo-wide exceptions belong in
  `pyproject.toml`, not scattered comments.

## Strong types

- Prefer types the typechecker can enforce over conventions people must recall.
- Model domain concepts as types (`GenId`, not `str` you split on `"-"`). The
  third place that special-cases the same string shape is where a type should
  have existed.
- Fail closed at boundaries: validate inputs/outputs; ambiguity rejects, never
  silently admits.

## Prefer simple architecture

- Small over clever. One responsibility per module, stated in one true line.
- A new seam beats a second special case on an old seam. If a fix adds another
  branch to the same place, stop patching — redesign.
- Exactly one of each thing: never a v1 beside a v2, never a second validator or
  serializer for the same contract.
- Delete dead code and expired shortcuts. Temporary work carries an expiry
  condition; net-negative cleanup is a task, not a hope.

## Agent-native

- Prose that agents read (skills, operator briefs, docs under load) is a
  first-class artifact: point at files to read, state hard constraints, don't
  pre-chew. The workspace is the medium.
- Prefer constraints and contracts machines can check (types, tests, schemas)
  over tribal knowledge in chat or unwritten review habits.
- Comment **why**, not what. Weird-but-intentional is fine only if the intent is
  recorded (comment or commit). Weird-and-unexplained is forbidden.

## No AI slop

- No filler, hedging, or ceremony. Every sentence and every line should earn its
  keep.
- Don't invent abstractions, wrappers, or "flexibility" layers for hypothetical
  futures. Solve the problem in front of you.
- Don't leave scar tissue: when behavior changes, update or delete the outdated
  test/doc in the same change — never shim production code to keep stale
  artifacts green.
- Don't spawn parallel docs or duplicate sources of truth. One place owns a
  fact; the rest link or stay silent.
- Prefer concrete names and direct structure over generic scaffolding
  (`utils`, `helpers`, `manager`, `Base*`) unless the abstraction already has
  two real callers.
