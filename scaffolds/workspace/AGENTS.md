# RSIHub Workspace

This repository is a self-driving evolvable workspace. The operating manual
is **`skills/evolve-agent/SKILL.md`** — read it first. It works for any
agent tool (Claude Code, codex, …); the skill lives in a plain `skills/`
folder, not a tool-specific one.

Everything goes through the vendored console **`./evolve`** (the mechanism is
under `.evolve/`, so no install is needed): `./evolve status .` to see where
things stand, `./evolve run . --resume` to run the driver, and
`./evolve operator active .` to discover capabilities for an
agent-orchestrated generation.

Hard rules (enforced — do not fight them): never hand-edit scores or archive
status fields; the frozen evaluator side stamps those. To edit a candidate as
the outer agent, read `program.md`, invoke configured operators for evidence,
and finish through commit → eval → finalize. Before committing manual candidate
edits, run `./evolve surface-check <child-worktree>`, then every configured
`validate` and `novelty` operator. Commit refuses missing or stale results.

Any newly introduced Python package must be added to the workspace root
`pyproject.toml` with `uv add`, and the resulting `uv.lock` must be committed.
Harbor trials run from the locked workspace environment: never rely on a package
installed globally, injected through `PYTHONPATH`, or installed ad hoc during a
trial. Packages that are not on PyPI (for example the DeepSeek Harness SDK used
by `hyperagents_dsh`) must be added from a local clone path with `uv add
/path/to/clone/...`, never substituted with an unrelated PyPI name.

## Python runtime and uv cache

Treat the Python interpreter and the dependency cache as separate prerequisites.
The mechanism reuses its current Python 3.12 interpreter for host-side workspace
commands; do not replace `EVOLVE_FRAMEWORK_PYTHON` with an incompatible or
short-lived path. Keep `UV_CACHE_DIR` and `EVOLVE_UV_CACHE_DIR` stable across a
run, and do not point `UV_PYTHON_INSTALL_DIR` at a new empty directory merely to
isolate an experiment.

On a clean runner, prepare the runtime while network access is available before
starting an offline experiment:

```bash
uv python install 3.12
uv sync --project . --frozen --python 3.12
```

Use `UV_PYTHON_DOWNLOADS=never` when checking that an existing interpreter is
reused. Set `UV_OFFLINE=1` only after the locked dependencies are present in the
selected cache: an empty dependency cache cannot run offline. A missing wheel or
Python runtime is an infrastructure/provisioning failure, not evidence about a
candidate; preserve the stderr and runtime receipt and repair the runner before
continuing the generation.

Project-root `.env` values are loaded automatically for run, eval, preflight,
and candidate-smoke commands. Exported variables take precedence; caller and
parent `.env` files are not loaded. Use API credentials by default, or an
explicit `CODEX_AUTH_JSON_PATH` for Codex. Never commit secrets or invoke the
internal `evaluator/eval.sh` directly.
