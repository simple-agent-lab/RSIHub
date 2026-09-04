# Contributing

Thank you for improving RSIHub. Read [the design guide](../docs/concepts/design.md),
[the architecture map](../docs/ARCHITECTURE.md), and [the coding style](../docs/development/coding-style.md)
before making a non-trivial change.

## Setup and checks

Requires `uv`, Git, and Python 3.12 or later.

```bash
uv lock --check
uv sync --dev --locked
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen ty check
```

During development, run the smallest relevant test file or node instead of the
whole suite. The default pytest command skips tests marked `slow`; run a related
slow test explicitly with:

```bash
uv run --frozen pytest -q --run-slow path/to/test.py::test_name
```

Run the default checks before opening a pull request. Run slow tests only when
the change touches the workflow they exercise, or when a release gate requires
them. See [`AGENTS.md`](../AGENTS.md) for the test tiers and change-to-test mapping. Tests
enforce the module inventory, recipe inventory, resource layout, and behavior
contracts; do not keep stale tests green with compatibility shims.

## Source ownership

Keep these categories separate:

- **Recipes** (`recipes/`) are the five supported, user-facing configurations.
  Recipe YAML selects the target, evaluator, and operator behavior.
- **Scaffolds** (`scaffolds/`) are generated workspace structure. Common files
  live under `scaffolds/workspace/`; evaluator-specific files live under their
  engine directory.
- **Seeds** (`seeds/`) are built-in evolvable target content. They are copied
  only when a recipe selects that seed.
- **Integrations** (`src/evolve/integrations/`) are framework-owned runtime
  behavior. Harbor adapters are vendored inside `.evolve/evolve/`, never
  generated as standalone workspace packages.

Test fixtures under `tests/fixtures/` are not supported recipes. Do not add
them to the public recipe inventory.

## Architecture and tests

`docs/ARCHITECTURE.md` is an executable module map: every `src/evolve/**/*.py` file
has one row and a line budget, enforced by `tests/test_coherence.py`. Update its
row and honest budget in the same change as a source-module change.

The mechanism must not import workspace operator code in-process. Operators run
as subprocesses, while frozen evaluator state stays outside the mutable
surface. When behavior changes, update or remove the test that describes the
previous behavior in the same commit.

## Documentation and commits

Keep commits focused and update the maintained documentation with behavior:

- `README.md` explains supported public workflows.
- `docs/` contains the public MkDocs guides, reference, and system design.
- `docs/ARCHITECTURE.md` maps executable modules.
- [`docs/development/documentation.md`](../docs/development/documentation.md) defines documentation ownership.

Avoid adding new prose files when one of these documents can be made clearer.
