# Built-in DeepSeek Harness (dsh) Target

This target runs [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
— a Node.js, plugin-composed agent harness — as the candidate. The evolvable
content is dsh's own agent profile; the harness pieces ship alongside it,
excluded from the mutable surface by the recipe.

Evolvable (the genome):

- `profile.cordis.yml` composes the model-visible layer against **sdk-minimal**
  packages only: `system-prompt` (persona) + `persistent-bash`
  (`@deepseek-ai/dsh-tool-bash-persistent`, `backendType: shell`) + local
  `./plugins/*.mjs` mounts. Do not reference removed demo spine packages.
- `plugins/**` holds candidate-authored dsh plugins (`*.mjs`); `seed-probe.mjs`
  is a no-op wiring proof.
- `skills/**` holds skill packages for future evolution notes; gen-0 does not
  load skill plugins (they are not part of sdk-minimal).
- `PLAYBOOK.md` / `EVOLUTION_LOG.md` carry lineage methodology and per-generation
  design notes for the mutation agent.

Harness-side (in `surface.exclude`, candidates cannot edit):

- `agent.py` — the Harbor candidate adapter. It runs host-side, spawns a dsh
  session per trial through the dsh Python SDK, bridges the session's bash tool
  into the task container via `docker exec`, and converts the session log into
  `trajectory.json` for the analyze operators.
- `dsh_trajectory.py` — the session-log converter.
- `runners/` — SDK drivers, local mutate, and frozen cordis **patches**:
  - `rollout.base.cordis.yml` — Harbor seams (docker-exec terminal-bash, pinned
    model). No foreign `cordis-plugin-include`.
  - `mutate.cordis.yml` — self-improvement persona on sdk-minimal packages.
  - `candidate_overlay.py` — materializes `profile.cordis.yml` under the
    per-trial `dsh_home` with absolute plugin paths so `@deepseek-ai/*` resolves
    via `$DSH_HOME/profiles/node_modules` (include-from-`checkout/target` would
    not).
  Drivers construct `DeepSeekHarness` with `dsh_home` + `profile=sdk-minimal` +
  `patches=(harbor, candidate_overlay)` (not the removed `session_root` /
  `cordis` kwargs). `DSH_SESSION_ROOT` is the isolated harness home; session
  JSONL lands under `$DSH_SESSION_ROOT/sessions/`.

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
