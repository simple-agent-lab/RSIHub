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

The dsh Python SDK is not on PyPI; add it to the workspace runtime from a dsh
clone after `evolve init` (the documented `uv add` extension path):

```bash
uv add /path/to/deepseek-harness/python/sdk
uv add --editable /path/to/deepseek-harness/python/sdk-runtime
git add pyproject.toml uv.lock && git commit -m "workspace runtime: add dsh sdk"
```

Node >= 22.19 must be on PATH (or `DSH_NODE_BIN`); `evaluator/prepare-runtime.sh`
verifies it before every evaluation. The evaluator model is passed through to
dsh and routed via `OPENAI_BASE_URL` / `OPENAI_API_KEY` (mapped onto dsh's
`DEEPSEEK_*`); the meta session's model defaults to dsh's native default and can
be overridden with `DSH_META_MODEL`. Restricted-network hosts can set the
optional `DSH_ASSETS_DIR` / `DSH_CONTAINER_APT_MIRROR` / `DSH_CONTAINER_PIP_INDEX`
/ `DSH_CONTAINER_PROXY` compensations described in `seeds/dsh/README.md`.
