# Built-in DeepSeek Harness (dsh) Target

This target runs [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
— a Node.js, plugin-composed agent harness — as the candidate. The evolvable
content is dsh's own agent profile; the harness pieces ship alongside it,
excluded from the mutable surface by the recipe.

Evolvable (the genome):

- `profile.cordis.yml` composes the entire model-visible layer: persona,
  toolset, skills, and evolved-plugin mounts.
- `plugins/**` holds candidate-authored dsh plugins (`*.mjs`); `seed-probe.mjs`
  is a no-op wiring proof.
- `skills/**` contains skill packages; `task-execution` is the baseline.
- `PLAYBOOK.md` / `EVOLUTION_LOG.md` carry lineage methodology and per-generation
  design notes for the mutation agent.

Harness-side (in `surface.exclude`, candidates cannot edit):

- `agent.py` — the Harbor candidate adapter. It runs host-side, spawns a dsh
  session per trial through the dsh Python SDK, bridges the session's bash tool
  into the task container via `docker exec`, and converts the session log into
  `trajectory.json` for the analyze operators.
- `dsh_trajectory.py` — the session-log converter.
- `runners/` — the SDK drivers (`rollout_driver.py`, `mutate_driver.py`), the
  local mutate command (`mutate_local.py`, the dsh self-modification session),
  and the two frozen cordis **patches** (rollout overlay with the docker-exec
  bridge + candidate include; mutation overlay with the self-improvement
  persona and cordis prototyping tools). Drivers construct `DeepSeekHarness`
  with `dsh_home` + `profile` + `patches` (not the removed `session_root` /
  `cordis` kwargs). Per-trial `DSH_SESSION_ROOT` is the isolated harness home;
  session JSONL lands under `$DSH_SESSION_ROOT/sessions/`.

Model routing follows the workspace's frozen identity: `OPENAI_BASE_URL` /
`OPENAI_API_KEY` are mapped onto dsh's `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`.
The dsh Python SDK is not on PyPI under a trustworthy name for this harness —
add it from a deepseek-harness clone with `uv add` as documented in
`recipes/hyperagents_dsh/README.md`. Task containers are assumed to have
network access (the Terminal-Bench 2 graders assume it too); restricted-network
hosts can opt into compensations via `DSH_ASSETS_DIR` (preload `uv`/`uvx` and a
portable Python 3.13 for graders that cannot reach github),
`DSH_CONTAINER_APT_MIRROR`, `DSH_CONTAINER_PIP_INDEX`, and
`DSH_CONTAINER_PROXY` — none of them touch scoring. Never commit credentials
under `target/`.
