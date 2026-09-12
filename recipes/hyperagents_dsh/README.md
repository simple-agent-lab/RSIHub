# HyperAgents for DeepSeek Harness (dsh)

This profile applies HyperAgents selection, trace browsing, and recording while
evolving the built-in [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
target: dsh's own agent profile (cordis composition + plugin sources + skills).
The mutate stage runs `runner: local` — a dsh self-modification session in the
child worktree reads the failure evidence and rewrites its own persona, plugins,
and skills. Validation uses the `node_check` operator (`node --check` on evolved
plugins plus a tag-tolerant YAML syntax check), so syntactically broken
candidates are rejected before a full evaluation.

## Runtime setup

The official dsh Python SDK is **not** installed from the unrelated
`deepseek-harness` PyPI package. After `evolve init`, add it to the workspace
runtime from a local [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
clone (the documented `uv add` extension path in the generated `AGENTS.md`):

```bash
# Required: path to a deepseek-harness checkout
export DSH_HARNESS_REPO=/abs/path/to/deepseek-harness

uv add "$DSH_HARNESS_REPO/python/sdk"
uv add --editable "$DSH_HARNESS_REPO/python/sdk-runtime"
git add pyproject.toml uv.lock && git commit -m "workspace runtime: add dsh sdk"
```

`mutate_local.py` refuses to start if `deepseek_harness` is not importable in
the workspace `.venv`.

## Node and sandbox boundaries

- **Node ≥ 22.19** must be on `PATH` (or `DSH_NODE_BIN`).
  `evaluator/prepare-runtime.sh` rejects older releases (including Node 22.0–22.18)
  before every evaluation; `evaluator/doctor.json` requires `DSH_NODE_BIN` to be
  an executable so doctor/preflight surfaces a missing runtime early.
- Mutation sessions use dsh `sandbox-policy` mode `workspace-write` rooted at the
  candidate profile (`DSH_CWD` / `target/`). Do not widen this to
  `danger-full-access` for the meta session.
- Task bash runs through `docker exec` into the Harbor task container. Candidate
  plugins still load in the host-side dsh process for rollouts; treat that host
  process as trusted evaluation infrastructure (further plugin isolation is a
  follow-up).

The evaluator model is passed through to dsh and routed via `OPENAI_BASE_URL` /
`OPENAI_API_KEY` (mapped onto dsh's `DEEPSEEK_*`); the meta session's model
defaults to dsh's native default and can be overridden with `DSH_META_MODEL`.
Restricted-network hosts can set the optional `DSH_ASSETS_DIR` /
`DSH_CONTAINER_APT_MIRROR` / `DSH_CONTAINER_PIP_INDEX` /
`DSH_CONTAINER_PROXY` compensations described in `seeds/dsh/README.md`.
