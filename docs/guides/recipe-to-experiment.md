# From recipe to experiment

This guide takes a supported or custom recipe through preflight, workspace
initialization, an isolated smoke run, and a real evolution run.

Before starting, prepare authentication, runtime identity, storage, and any
proxy settings described in [Environment Variables](../reference/environment-variables.md).
If `preflight`, `doctor`, `smoke`, or the first evaluation cannot start, verify
that the same environment was loaded for every command before changing the
recipe.

## 1. Prepare the dataset

Download a Harbor dataset directly:

```bash
uv run --frozen harbor download terminal-bench@2.0 \
  --export \
  -o /absolute/path/to/terminal-bench-2
```

For a supported repository recipe, the helper also materializes the pinned
Terminal-Bench subset and builds the selected mutation-agent image:

```bash
./scripts/setup_terminal_bench.sh gepa
```

The helper also accepts a custom recipe directory or YAML path when its mutation
agent is Codex or the installed MiniSWE adapter:

```bash
./scripts/setup_terminal_bench.sh /absolute/path/to/my-recipe/evolve.yaml
```

For Codex, set the version in the existing recipe fields. This is a fragment to
merge into a complete recipe; retain its other operator and evaluator settings:

```yaml
operators:
  mutate:
    config:
      agent: codex
      image: my-codex:0.150.0
      agent_kwargs:
        version: "0.150.0"
evaluator:
  agent_kwargs:
    version: "0.143.0"
```

`operators.mutate.config.agent_kwargs.version` selects the mutation agent's CLI
and the setup script's `CODEX_VERSION` Docker build argument.
`evaluator.agent_kwargs.version` independently overrides the Codex target seed's
version. Use an exact three-part version string and a distinct image tag when
changing the mutation version; the helper rejects a mismatch with a reserved
built-in tag. Availability of the selected package is checked during the build.
For other mutation adapters, prepare the image yourself.

Existing recipes keep mutation Codex 0.146.0 and seed Codex 0.143.0 defaults.
`hyperagents_codex_tbench_full` explicitly selects 0.149.0 for both roles.
The outer Codex controller is separate: its `--require-version` option defaults
to 0.149.0 and checks the installed host CLI; it does not install it.
Initialize a fresh workspace to apply recipe changes; existing frozen experiment
configuration is not rewritten.

Role-specific defaults live in `containers/runtime-versions.env`; standalone
Dockerfile, seed and controller defaults are checked against it by
`tests/test_runtime_version_pins.py`. The setup helper prefers an existing local
image. It resolves the immutable image ID and runs an offline, read-only probe
for required tools and the actual CLI version; a label alone is not accepted.
A missing image is built. A mismatched image fails with an explicit `--rebuild`
instruction, preserving the existing image until the user chooses replacement:

```bash
./scripts/setup_terminal_bench.sh gepa --rebuild
```

The validated image ID is printed for recording or pinning in the experiment.
Validation confirms the tool/version contract, not complete environment equivalence.
Skipping setup remains supported when the dataset and image are already prepared.
Custom recipes use the commands below with an explicit dataset path;
`run_recipe_demo.sh` accepts only the built-in profiles.

## 2. Check the recipe and run prospective preflight

Resolve every binding and validate every named operator config first:

```bash
uv run --frozen evolve recipe check /absolute/path/to/my-recipe/evolve.yaml
```

For a supported recipe:

```bash
export EVOLVE_RUNTIME_DIGEST="sha256:replace-with-your-runtime-digest"

uv run --frozen evolve preflight /absolute/path/to/my-experiment \
  --recipe gepa \
  --dataset /absolute/path/to/harbor/tasks
```

For a custom recipe:

```bash
uv run --frozen evolve preflight /absolute/path/to/my-experiment \
  --recipe-path "$PWD/my-recipes/my-gepa" \
  --seed /absolute/path/to/my-agent \
  --dataset /absolute/path/to/harbor/tasks
```

Prospective preflight is read-only. It checks the recipe, seed, dataset,
runtime identity, required tools, and destination workspace before anything is
frozen.

Direct CLI commands do not automatically load `.env`. Either export the
variables first or add `--env-file` to the `uv run` invocation:

```bash
uv run --frozen --env-file /absolute/path/to/experiment.env \
  evolve preflight /absolute/path/to/my-experiment \
  --recipe-path "$PWD/my-recipes/my-gepa" \
  --dataset /absolute/path/to/harbor/tasks
```

## 3. Initialize a fresh workspace

Repeat the same inputs with `init`:

```bash
uv run --frozen evolve init /absolute/path/to/my-experiment \
  --recipe-path "$PWD/my-recipes/my-gepa" \
  --seed /absolute/path/to/my-agent \
  --dataset /absolute/path/to/harbor/tasks
```

Initialization creates a separate Git repository and freezes:

- the resolved `evolve.yaml`;
- the target seed under `target/`;
- active operators under `operators/`;
- runtime helpers imported by selected library operators under `library/`;
- operator source names, normalized config, and SHA-256 provenance in
  `.evolve-components.json`;
- the evaluator and task membership under `evaluator/`;
- the framework mechanism under `.evolve/`;
- the initial `gen/0` tag and archive row.

Do not edit the source recipe and expect an existing workspace to update.
Create a new workspace whenever an initialization input changes.

## 4. Inspect the initialized contract

Use the workspace's vendored console from this point onward:

```bash
cd /absolute/path/to/my-experiment

./evolve operator active .
git show gen/0:evolve.yaml
git show gen/0:evaluator/splits.json
git status --short
```

`operators/README.md` lists the recipe-selected implementations. Generic root
helpers, method-private bundles imported by selected code, and each selected
named stage's helper bundle are copied under `library/`. Unselected catalog
operators and method-private bundles remain in the source installation.

## 5. Run doctor and an isolated smoke

The experiment doctor is read-only:

```bash
./evolve doctor . --profile experiment
```

Run workspace commands from a shell with the same credentials, endpoint,
runtime, proxy, and storage variables used during initialization. See the
[environment checklist](../reference/environment-variables.md#environment-checklist)
before diagnosing the recipe or framework.

Add `--probe-model` when you want it to make a real model request:

```bash
./evolve doctor . --profile experiment --probe-model
```

Run the full-loop canary before a long experiment:

```bash
./evolve smoke . --profile experiment
```

The smoke command works on a disposable clone under
`runs/experiment-smoke/`; it does not add smoke candidates or scores to the
source workspace.

## 6. Launch the experiment

Start with one child and stream operator output:

```bash
./evolve run . \
  --max-generations 1 \
  --children-per-gen 1 \
  --verbose
```

Scale only after the one-generation run reaches a recipe-valid terminal state:

```bash
./evolve run . \
  --max-generations 20 \
  --children-per-gen 1 \
  --verbose
```

Runs resume by default. `--resume` is accepted for compatibility but is a
no-op. By default, the command fails if a requested generation does not reach a
recipe-valid terminal state.

## 7. Monitor and verify

```bash
./evolve status .
./evolve verify .
git tag --list 'gen/*' --sort=version:refname
tail -n 20 archive.jsonl
```

Useful evidence locations include:

```text
runs/gen-N/select/
runs/gen-N/rollout/
runs/gen-N/analyze/
runs/gen-N/mutate/
runs/gen-N/validate/
runs/gen-N/gate/
runs/gen-N/record/
runs/evaluations/
artifacts/generations/
```

A successful experiment should have consistent archive rows, generation tags,
candidate commits, evaluator receipts, and operator artifacts. A live process
or container alone is not evidence of a completed generation.

## 8. Diagnose a failed stage

First identify the failing stage:

```bash
./evolve status .
```

If no stage started, or the failure is authentication, endpoint, proxy, cache,
or filesystem related, check
[Environment Variables](../reference/environment-variables.md) first.

Then inspect its generation directory and retained logs. Use:

```bash
./evolve doctor . --profile experiment
./evolve verify .
```

Do not rewrite generation tags or archive history to hide a failed attempt.
Preserve the failure as experiment evidence, repair the external precondition or
recipe in its source location, and initialize a new workspace when the frozen
experiment contract must change.
