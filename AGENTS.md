# Repository instructions

Read `.github/CONTRIBUTING.md`, `docs/concepts/design.md`, and `docs/ARCHITECTURE.md` before
making a
non-trivial change. Preserve unrelated work in a dirty worktree and keep edits
inside the requested scope.

## Test policy

The repository test suite is intentionally layered because some end-to-end
checks create complete workspaces and can take minutes. Do not run every test
after every edit.

1. **Focused checks (during development):** run the smallest relevant test
   node or file for the code being changed. Repeat this layer while iterating.
2. **Default suite (before handoff):** `uv run --frozen pytest -q` runs the
   normal unit, behavior, and lightweight integration tests. Tests marked
   `slow` are skipped by default.
3. **Slow integration checks (only when related):** run a marked test with
   `uv run --frozen pytest -q --run-slow <test-path-or-node>`. Use this layer
   only when the change touches the workflow that test exercises, when a
   release gate explicitly requires it, or when investigating a failure in
   that workflow. Run all slow tests only for broad cross-cutting changes.
4. **External/live checks (manual):** tests that require Docker, Harbor jobs,
   model calls, credentials, network services, or a real evolution campaign
   are never routine validation. Run them only when the task explicitly needs
   that integration and report the environment and artifacts separately.

Choose tests by behavior, not just by filename. Typical mappings are:

- CLI or one source module: its direct test file or exact test nodes.
- Recipe, scaffold, or seed changes: the recipe-specific tests plus
  `tests/test_recipe_composition.py` and/or `tests/test_resource_layout.py` as
  applicable.
- Architecture/module inventory changes: the relevant
  `tests/test_coherence.py` nodes.
- `gepa_local` local-Harbor generation changes: the focused recipe tests and
  `tests/test_gepa_local_recipe.py::test_gepa_local_full_generation_improves_champion`
  with `--run-slow`.
- Packaging/release changes: build the distribution and run
  `tests/test_release_artifact.py` with `EVOLVE_RELEASE_DIST` set, following
  the release workflow.

If a new test routinely takes tens of seconds, launches a complete evolution,
or depends on expensive setup, mark it `@pytest.mark.slow` and document the
feature/path that should trigger it. A slow test must stay deterministic and
must not silently depend on live external services.
