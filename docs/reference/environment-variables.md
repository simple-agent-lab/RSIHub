# Environment Variables

Environment variables provide credentials and host-specific runtime settings
that must not be committed to a recipe. An experiment can fail before or during
evaluation when authentication, endpoint, proxy, or path variables are missing
or inconsistent.

## How variables are loaded

The supported recipe launcher reads the repository-root `.env` file by default:

```bash
./scripts/run_recipe_demo.sh gepa
```

This is equivalent to launching its `uv` commands with `--env-file .env`. Use a
different file with:

```bash
ENV_FILE=/absolute/path/to/experiment.env \
  ./scripts/run_recipe_demo.sh gepa
```

Direct CLI commands and an initialized workspace's `./evolve` command do **not**
automatically discover a `.env` file. Load it explicitly:

```bash
uv run --frozen --env-file /absolute/path/to/experiment.env \
  evolve preflight /absolute/path/to/workspace \
  --recipe-path /absolute/path/to/recipe \
  --dataset /absolute/path/to/tasks
```

For subsequent workspace commands:

```bash
set -a
. /absolute/path/to/experiment.env
set +a

./evolve doctor . --profile experiment
./evolve smoke . --profile experiment
./evolve run . --max-generations 1
```

Use a secrets manager or another environment-loading mechanism if sourcing a
file is not appropriate for your host.

## Authentication

### API-key authentication

API-key authentication is the default:

```dotenv
OPENAI_API_KEY=replace-me
```

An OpenAI-compatible endpoint can be selected with:

```dotenv
OPENAI_BASE_URL=https://api.example.com/v1
```

`OPENAI_API_BASE` is accepted by some adapters for compatibility, but
`OPENAI_BASE_URL` is the preferred setting.

The model endpoint contributes to the frozen runtime identity. Use the same
`OPENAI_BASE_URL` during initialization, preflight, smoke, and the real run.
Changing the endpoint requires a new workspace.

### Codex auth file

Codex agents may use an explicit auth file instead of an API key:

```dotenv
CODEX_AUTH_JSON_PATH=/absolute/path/to/auth.json
```

The path must exist on the host. RSIHub does not implicitly search
`~/.codex/auth.json` for an experiment run. Non-Codex agents do not accept a
Codex auth file.

Do not set `CODEX_FORCE_AUTH_JSON`; it is unsupported. Choose
`CODEX_AUTH_JSON_PATH` explicitly.

## Runtime identity

Prospective preflight for a Harbor experiment requires an immutable runtime
identity:

```dotenv
EVOLVE_RUNTIME_DIGEST=sha256:replace-with-your-runtime-digest
```

Set it before running preflight or initialization:

```bash
export EVOLVE_RUNTIME_DIGEST="sha256:replace-with-your-runtime-digest"
```

Use the digest assigned to the evaluator runtime used by your experiment. Do
not reuse a placeholder in a real benchmark.

## Supported launcher overrides

The repository launch scripts accept these user-facing overrides:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKSPACE` | `runs/<recipe>-demo` | destination for the initialized experiment workspace |
| `TASKS` | recipe default | limit evaluator tasks per round |
| `GENERATIONS` | `1` | maximum generations requested by the demo runner |
| `ENV_FILE` | repository `.env` | environment file loaded by `run_recipe_demo.sh` |
| `EVOLVE_ASSET_DIR` | `.evolve-assets/terminal-bench-2.0` | reusable dataset and prepared subset location |

Example:

```bash
WORKSPACE=/data/experiments/gepa-smoke \
TASKS=1 \
GENERATIONS=1 \
ENV_FILE="$PWD/.env" \
EVOLVE_ASSET_DIR=/data/evolve-assets \
  ./scripts/run_recipe_demo.sh gepa
```

## Storage and cache overrides

These variables are optional. Set them when the default home directory or
filesystem does not have enough space, is read-only, or should not hold
experiment state:

| Variable | Purpose |
| --- | --- |
| `EVOLVE_HOME` | archive mirrors and framework-level state |
| `EVOLVE_UV_CACHE_DIR` | persistent cache shared by evaluator and candidate runtime preparation |
| `EVOLVE_UV_PYTHON_INSTALL_DIR` | Python installations used by managed runtime preparation |
| `EVOLVE_UV_BINARY` | explicit path to the isolated `uv` executable used to prepare and install candidate runtimes |
| `UV_CACHE_DIR` | cache used by direct host-side `uv` commands |
| `TMPDIR` | host temporary-file location |

Use absolute paths and keep them stable when resuming the same workspace. The
candidate-runtime adapter uploads the resolved host `uv` binary to a private
path and invokes it by absolute path. It records that binary's version and
SHA-256 in the task's runtime evidence, then removes it before the target agent
runs. It does not replace an evaluator task image's own `uv`, add the runtime
binary to the task `PATH`, or expose it to commands executed by the target
agent.

```dotenv
EVOLVE_HOME=/data/evolve/home
EVOLVE_UV_CACHE_DIR=/data/evolve/uv-cache
EVOLVE_UV_PYTHON_INSTALL_DIR=/data/evolve/python
UV_CACHE_DIR=/data/uv-cache
TMPDIR=/data/evolve/tmp
```

## Harbor execution headroom

Harbor timeout multipliers are optional execution controls. Each multiplier is
applied to the corresponding limit declared by a benchmark task; it does not
replace the task's timeout:

| Variable | Purpose |
| --- | --- |
| `EVOLVE_HARBOR_AGENT_SETUP_TIMEOUT_MULTIPLIER` | agent installation/setup headroom |
| `EVOLVE_HARBOR_AGENT_TIMEOUT_MULTIPLIER` | target-agent execution headroom |
| `EVOLVE_HARBOR_VERIFIER_TIMEOUT_MULTIPLIER` | verifier execution headroom |
| `EVOLVE_HARBOR_MAX_RETRIES` | Harbor retry count for retry-eligible exceptions |
| `EVOLVE_HARBOR_N_CONCURRENT` | default concurrent Harbor trials used when an operator does not override `n_concurrent` |

Prefer explicit recipe keys for reproducible experiments. Environment values
are useful for launch profiles and diagnostics, but `operators.rollout` and
`operators.validate` may set their own concurrency and timeout multipliers.
Keep the two GEPA stages symmetric when changing execution headroom.

## Proxy variables

Standard proxy variables are optional and inherited from the host:

```dotenv
HTTP_PROXY=http://proxy.example.com:8080
HTTPS_PROXY=http://proxy.example.com:8080
NO_PROXY=localhost,127.0.0.1
```

Lowercase forms are also recognized. Ensure the model endpoint and any
host-to-container services that must be reached directly are represented
correctly in `NO_PROXY`. Proxy values must be valid URLs; malformed values can
cause HTTP clients to fail before a model request is made.

Do not copy another machine's proxy or `NO_PROXY` values. They are
host/network-specific.

## Variables managed by RSIHub

Variables such as the following are generated for an individual operator or
evaluation attempt and should not normally be set by users:

```text
EVOLVE_GENID
EVOLVE_PARENT
EVOLVE_CHECKOUT
EVOLVE_RUN_DIR
EVOLVE_RUN_PLAN
EVOLVE_EVAL_SPLIT
EVOLVE_HARBOR_TASK_FILE
EVOLVE_HARBOR_EXPECTED_TRIALS
EVOLVE_CANDIDATE_SOURCE
```

Setting internal variables manually can make retained evidence disagree with
the frozen experiment contract. Prefer recipe configuration and the documented
launcher overrides.

## Environment checklist

When an experiment does not start, check:

1. The intended environment file was actually loaded.
2. Exactly one supported authentication path is available for the selected
   agent.
3. `CODEX_AUTH_JSON_PATH`, if used, is absolute and readable.
4. `OPENAI_BASE_URL` is valid and matches the endpoint used at initialization.
5. `EVOLVE_RUNTIME_DIGEST` is set for prospective Harbor preflight.
6. Proxy variables contain valid URLs and `NO_PROXY` matches the host network.
7. Workspace, cache, and temporary paths are writable and have sufficient
   capacity.
8. The same environment is loaded for `doctor`, `smoke`, and `run`.

Use the experiment doctor to distinguish environment failures from candidate
or evaluator failures:

```bash
./evolve doctor . --profile experiment --probe-model
```
