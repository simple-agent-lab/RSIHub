# Quick Start

Run one of the supported recipes against the shared, content-pinned
Terminal-Bench 2.0 subset. The launcher requires Bash, Python 3.12+,
[`uv`](https://docs.astral.sh/uv/), Git 2.25+, and a running Docker daemon.

```bash
git clone https://github.com/simple-agent-lab/RSIHub.git
cd RSIHub

# API authentication is the default. Keep credentials out of recipe YAML.
cat > .env <<'EOF'
OPENAI_API_KEY=replace-me
# OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
EOF

docker info
```

Choose a recipe, download and verify the pinned dataset, build that recipe's
pinned mutate-runner image, and launch one generation:

```bash
RECIPE=ahe
./scripts/setup_terminal_bench.sh "$RECIPE"
./scripts/run_recipe_demo.sh "$RECIPE"
```

Supported values are `aevolve`, `ahe`, `ahe_codex`, `gepa`, `hill_climb`,
`hill_climb_codex`, `hyperagents`, `hyperagents_codex`, and
`hyperagents_dsh` (see its [recipe README](recipes/hyperagents_dsh/README.md)
for the extra dsh SDK setup it needs). Codex-capable
profiles may use `CODEX_AUTH_JSON_PATH=/absolute/path/to/auth.json` instead of
an API key. Use `WORKSPACE`, `TASKS`, `GENERATIONS`, `ENV_FILE`, or
`EVOLVE_ASSET_DIR` to override launcher defaults. See the
[recipe guide](recipes/README.md) and
[operations guide](docs/guides/operations.md) for the full configuration and
recovery workflow.

For evolution results on Terminal Bench 2 and Tau³ Banking, see the
[benchmark results](README.md#benchmark-results) in the README.
