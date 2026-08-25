# Experiment viewer

`evolve view` serves a read-only browser for one generated experiment
workspace or a catalog of related workspaces. Use it to answer four questions
without opening raw files by hand:

- Is the experiment healthy and which generation is active?
- What changed between generations?
- How did canonical benchmark performance move?
- What happened inside an individual Harbor trial?

## Start the viewer

Pass the experiment root—the directory containing `evolve.yaml`,
`archive.jsonl`, and `runs/`:

```bash
uv run evolve view /absolute/path/to/experiment
```

An installed CLI can use `evolve view` directly. The default listener is
`127.0.0.1`, and the command selects the first free port from `8080-8089`.
Override either value when necessary:

```bash
evolve view /absolute/path/to/experiment --host 127.0.0.1 --port 9000-9009
```

The command prints the selected URL and a matching SSH tunnel command.

## Compare multiple experiments

Create a YAML catalog manually when several completed or active workspaces
should share one index. A catalog is not needed when viewing one workspace:

```yaml
experiments:
  - slug: miniswe-aevolve
    label: A-Evolve · MiniSWE
    method: A-Evolve
    agent: MiniSWE
    selection_metric: Gate
    workspace: /absolute/path/to/experiment
  - slug: codex-gepa
    label: GEPA · Codex
    method: GEPA
    agent: Codex
    selection_metric: Gate
    workspace: /absolute/path/to/another-experiment
```

Only `workspace` is required. The remaining fields control presentation:

| Field | Required | Behavior |
| --- | --- | --- |
| `workspace` | yes | Absolute path, or a path relative to the catalog file, to a workspace containing `evolve.yaml` and `archive.jsonl`. |
| `slug` | no | Stable URL segment; generated from `label` or the workspace directory name when omitted. |
| `label` | no | Human-readable experiment label; defaults to the workspace directory name. |
| `method` | no | Evolution-method label used by cards and method filters. It is not currently inferred from `evolve.yaml`. |
| `agent` | no | Target-agent label used for grouping and agent filters. It is not currently inferred from `evolve.yaml`; arbitrary new agent names are supported. |
| `selection_metric` | no | Score label shown on the experiment card. |
| `benchmark` | no | Explicit display name. When omitted, the viewer derives it from `evaluator.benchmark`, `evaluator.dataset_name`, or `evaluator.dataset` in `evolve.yaml`. |

Start the catalog with:

```bash
evolve view --catalog /absolute/path/to/catalog.yaml
```

The catalog groups experiments by target agent, filters by method, and draws
the actual generation-parent topology with selection score on the vertical
axis. Each entry opens the complete single-workspace viewer under the same
server, including champion replay, artifacts, trials, and Harbor evidence.
Catalog paths may be absolute or relative to the catalog file. Every workspace
must contain `evolve.yaml` and `archive.jsonl`.

The catalog heading summarizes the benchmarks represented by all entries. Each
experiment card and every page inside an experiment also show that experiment's
benchmark. A catalog spanning several benchmarks lists all distinct benchmark
names rather than displaying a fixed benchmark label.

## View a remote experiment

Start the server on the remote machine and leave it running:

```bash
# Remote machine
evolve view /data00/home/$USER/experiments/my-run --port 8080
```

Forward the same port from the laptop:

```bash
# Laptop
ssh -N -L 8080:127.0.0.1:8080 user@remote-host
```

Open `http://127.0.0.1:8080/`. Keep both terminal processes alive while using
the viewer.

## Read the pages

### Overview

The overview combines current health, latest modification, canonical score
history, and recent generations. The performance chart always uses a `0` to
`1` score axis. `G0`, `G1`, and so on identify generations; selecting a
generation shows history only through that generation.

The large performance value is the selected generation's canonical score. The
delta compares it with its parent when both evaluations are comparable. The
best score is reported separately in the health summary.

### Generations

The generations table shows stage status, canonical score, changed-file count,
and insertion/deletion totals. Open a generation to inspect its stage evidence,
modification rationale, artifacts, and canonical trials.

### Trials and Harbor

The trials page supports generation, purpose, status, and exact-task filters.
When retained Harbor evidence can be matched to a canonical trial, **Full
Harbor inspection** opens Harbor's trajectory, agent logs, verifier output,
artifacts, configuration, lock file, and exception details on the same server.

The viewer builds a disposable hard-link index for referenced Harbor jobs.
This lets Harbor enforce its normal path-containment checks without copying the
job contents. The index is removed when the viewer exits; source experiment
files are not modified.

### Artifact previews

Registered text artifacts open inside the viewer. Unified diffs use Diff2Html;
JSON and common source formats use highlight.js; unsupported content falls back
to plain text. Use **Raw** for the original bounded response and **Wrap lines**
for long lines.

Previews are limited to the first 1 MiB. A notice appears when content is
truncated. Artifact URLs contain registered opaque IDs rather than arbitrary
filesystem paths.

## Refresh and safety

RSIHub pages refresh filesystem summaries every three seconds. Shell and asset
responses disable browser caching so a restarted deployment does not reuse stale
viewer code.

The composed server permits only `GET`, `HEAD`, and `OPTIONS`, and blocks
Harbor run, delete, upload, summarize, and authentication actions. It is still
an inspection tool—not an authorization boundary. Anyone who can access the
listener or SSH tunnel can read the exposed experiment and Harbor evidence.
Keep the default loopback binding unless access is protected separately.

## Troubleshooting

**The laptop cannot connect.** Confirm the remote viewer server is still running, the
tunnel uses the port printed by `evolve view`, and the browser opens the local
side of the tunnel (`127.0.0.1`).

**A different port was selected.** Another process owns the requested port.
Use the URL and tunnel command printed by the viewer, or pass a different port
or ascending port range.

**A trial has no Harbor link.** The experiment must retain a readable Harbor
job directory, and the Harbor task must map uniquely to the canonical task and
repetition recorded by RSIHub.

**An artifact is truncated.** Use the relative path shown in the preview to
inspect the original file on the experiment host when more than 1 MiB is
required.

**The page reports a refresh warning.** The viewer keeps the last valid
snapshot when a filesystem refresh fails. Inspect the warning, repair or finish
the partially written artifact, and allow the next polling cycle to retry.
