# HyperAgents for DeepSeek Harness (dsh)

This profile applies HyperAgents selection, trace browsing, and recording while
evolving the built-in [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
target: dsh's own agent profile (cordis composition + plugin sources + skills).
The mutate stage runs `runner: local` — a dsh self-modification session in the
child worktree reads the failure evidence and rewrites its own persona, plugins,
and skills. Validation uses the `node_check` operator (`node --check` on evolved
plugins plus a tag-tolerant YAML syntax check), so syntactically broken
candidates are rejected before a full evaluation.


## Dataset setup (no mutate image)

`hyperagents_dsh` uses `runner: local` for mutate — there is **no** Codex/MiniSWE
mutate image to build. Prepare Terminal-Bench dataset assets only:

```bash
./scripts/setup_terminal_bench.sh hyperagents_dsh
# then, after the SDK install below:
EVOLVE_ASSET_DIR=.evolve-assets/terminal-bench-2.0 ./scripts/run_recipe_demo.sh hyperagents_dsh
```

`setup_terminal_bench.sh` downloads/prepares the pinned 30-task subset and skips
mutate-image resolve/build for this recipe. Docker is still required for Harbor
task containers at evaluation time.

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


## Runtime carrier (exe vs node)

Editable `uv add` of `sdk-runtime` does **not** by itself produce a launchable
carrier. From the deepseek-harness checkout, build it:

```bash
pnpm exec tsx scripts/build-exe-for-python-sdk.ts
```

That materializes the platform exe and/or `runtime/node/` under `sdk-runtime`.

### Prepare path vs driver preference

| Layer | When `DSH_RUNTIME_MODE` is unset | Notes |
| --- | --- | --- |
| `evaluator/prepare-runtime.sh` | Forced to **`node`** | Writes `DSH_RUNTIME_MODE=node` into the trial env (`--ae`/`--ve`). This is the Stage B / doctor eval path. |
| `seeds/dsh/runners/runtime_mode.py` | May prefer **`exe`** if the bundled exe resolves | Used by mutate/rollout drivers when env is still unset. |

**Prepare usually wins for real Harbor eval**: once prepare runs, trials see
`DSH_RUNTIME_MODE=node` even though a bare driver call would prefer `exe`.

- **`DSH_RUNTIME_MODE=node`** — system Node ≥ 22.19 (`PATH` or `DSH_NODE_BIN`);
  prepare requires it when mode is `node` or empty. Missing node closure →
  drivers fail early with a pointer to the build script above.
- **`DSH_RUNTIME_MODE=exe`** — bundled platform exe; no system Node required at
  prepare time. Drivers still preflight the exe via `runtime_mode.py`.

**Want exe for eval?** Set `DSH_RUNTIME_MODE=exe` explicitly in the environment
*before* prepare (and ensure the carrier was built). Do not rely on the driver
preference alone under Harbor — prepare will otherwise pin `node`.

## Node and sandbox boundaries

- **Node ≥ 22.19** must be on `PATH` (or `DSH_NODE_BIN`) when using
  `DSH_RUNTIME_MODE=node` (the default prepare path).
  `evaluator/prepare-runtime.sh` rejects older releases (including Node 22.0–22.18)
  before every evaluation; `evaluator/doctor.json` requires `DSH_RUNTIME_MODE` and
  runs the docker-exec smoke so doctor/preflight fails before a long evolve.
- Mutation sessions use dsh `sandbox-policy` mode `workspace-write` rooted at the
  candidate profile (`DSH_CWD` / `target/`). Do not widen this to
  `danger-full-access` for the meta session.
- Candidate plugins still load in the host-side dsh process for rollouts; treat
  that host process as trusted evaluation infrastructure (further plugin
  isolation is a follow-up).

### Canonical policy: docker CLI + terminal-bash argv

Proven combo for Harbor / Colima headless hosts (keep doctor, cordis, and agent
aligned):

1. **Resolve docker** via `DSH_DOCKER_BIN` (if set and executable) or `PATH`
   (`shutil.which` / `command -v`). Homebrew Mac is fine
   (`/opt/homebrew/bin/docker`). **Never** hard-code `/usr/bin/docker`.
2. **`docker exec -i`** into the task container — **never** add `-t` (container
   TTY under headless Harbor/node-pty → "PTY shell exited during startup").
3. **Trailing `/bin/bash --noprofile --norc -i`** — keep bash `-i` so
   terminal-bash prompt readiness (PS1 / `PROMPT_COMMAND` OSC) can settle.
   Dropping `-i` yields `PERSISTENT_BASH_TIMEOUT` (no prompts), not a PTY fix.

Doctor (`evaluator/doctor_pty_probe.sh`) must exercise the same argv, including
a host-PTY spawn and prompt-readiness markers — not a weaker `/bin/sh -c`
pipe-only check. Timeout without a prompt = readiness hang.

Elsewhere (doctor headers, cordis comments, seed READMEs, tests) should **point
here** rather than restate the full essay; tests still assert the behavioral
locks (`-i`, no `-t`, docker resolve).

## Config layers (operator checklist)

| Layer | What to pin / watch | Footgun |
| --- | --- | --- |
| `evolve.yaml` `evaluator.model` ↔ workspace `evaluator/eval.env` `EVOLVE_HARBOR_MODEL` | Pin **both**, commit, then `git tag -f gen/0` | Dirty WT alone still serves the **old** model from the tagged genesis identity |
| `evaluator/prepare-runtime.sh` → Harbor `--ae` / `--ve` | KEY=VALUE lines become trial agent/verifier env | Empty `DSH_RUNTIME_MODE` → prepare writes **`node`** (see Runtime carrier) |
| Agent `DSH_*` / `OPENAI_*` → `DEEPSEEK_*` | Adapter maps `OPENAI_BASE_URL` / `OPENAI_API_KEY` onto dsh `DEEPSEEK_*`; optional `DSH_META_MODEL`, `DSH_ASSETS_DIR`, container mirror/proxy knobs | Forgetting the mapping looks like "wrong endpoint" on the candidate |
| Dual timeouts | Harbor agent timeout (~900s default) **vs** `DSH_TASK_TIMEOUT_SEC` (recipe default 1800) | Long tasks can hit Harbor first; raise Harbor multipliers only when intentional |
| `EVOLVE_HARBOR_EXPECTED_TRIALS` / `run-plan.json` vs task-split length | For smoke / `--tasks N`, expected_trials must match the planned count | `parse_score` priority: a complete limited run must not be `infra_failed` solely because the 30-member split file is larger |

Restricted-network hosts: optional `DSH_ASSETS_DIR` /
`DSH_CONTAINER_APT_MIRROR` / `DSH_CONTAINER_PIP_INDEX` /
`DSH_CONTAINER_PROXY` — see `seeds/dsh/README.md`.

## Pre-experiment checklist

Run these before a multi-generation evolve so infra bugs do not burn tokens:

1. **Model pin** — update `evaluator.model` *and* regenerate/commit `eval.env`,
   then `git tag -f gen/0` (see Config layers).
2. **Limited-run expected_trials** — confirm `run-plan.json` /
   `EVOLVE_HARBOR_EXPECTED_TRIALS` matches the planned task count.
3. **PTY / docker-exec doctor probe** — `./evolve doctor . --profile experiment`
   must pass `evaluator_runtime_smoke` (same argv as the canonical policy above).
4. **Timeouts** — confirm Harbor agent budget vs `DSH_TASK_TIMEOUT_SEC`.
