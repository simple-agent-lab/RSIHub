# Built-in Codex Target

This target wraps Harbor's installed Codex agent while keeping the behavior
surface under `target/**` so RSIHub can mutate it.

- `agent.py` injects the candidate-owned prompt, skills, and Codex flags.
- `prompt.md` is the task prompt template and must retain `{{ instruction }}`.
- `skills/**` contains reusable workflows copied into each Harbor task container.
- `codex.toml` pins the initial model/CLI version and exposes reasoning,
  web-search, compaction, and tool-output settings.
- `.agents/plugins/marketplace.json` and `plugins/evolve-target/**` define an
  evolvable local plugin. The initial `SessionStart` hook injects concise
  candidate-owned context into every evaluated Codex session.

Compaction overrides are off by default so Codex uses model defaults. Set
`compaction.override_defaults = true` to evaluate the candidate values.

Authentication is runtime state, not part of the genome. The default `auto`
mode uses `CODEX_AUTH_JSON_PATH` or `CODEX_FORCE_AUTH_JSON` when either is set,
switches to API mode when `OPENAI_BASE_URL` or `OPENAI_API_BASE` is set, and
otherwise requires `~/.codex/auth.json`. Set `EVOLVE_CODEX_AUTH_MODE` to
`auth_json` or `api` to choose explicitly. API mode requires
`OPENAI_API_KEY`, configures an OpenAI-compatible Responses provider, disables
WebSockets, and supplies the key through both the normal provider auth and the
`api-key` header. Never commit credentials under `target/`.

When task containers cannot reach package repositories, set
`EVOLVE_CODEX_BINARY_PATH` and optionally `EVOLVE_CODEX_RG_PATH` to absolute
host executable paths. Harbor bind-mounts them read-only into each task
container, so its version check can skip the network installer.

The Harbor wrapper installs the candidate plugin into its temporary
`CODEX_HOME` for each isolated task. It bypasses interactive hook review only
for that externally sandboxed evaluator invocation, so changed hook definitions
are exercised instead of silently skipped.

## Execution clock

The built-in target reads only the agent-limit metadata from Harbor's trial
`config.json` and `task.toml`. Prompt rendering exposes the effective limit and
starts an approximate Unix deadline before in-run setup. The Codex subprocess
inherits `EVOLVE_AGENT_DEADLINE_UNIX`; tools can subtract `time.time()` to observe
remaining seconds. The deadline does not reset at the first model call. Harbor
still owns the actual cutoff and cancellation; this is a planning signal, not a
new enforcement or retry policy. Missing or invalid metadata is explicitly
unavailable and never gets a guessed limit. A custom agent or nonstandard trial
layout must provide its own budget bridge.

The skill asks the target to budget long operations and leave time for final
verification. It remains candidate-owned policy: availability of a clock does
not guarantee that a model will use it effectively. Existing frozen workspaces
are not rewritten; a new comparison must use the same runtime bridge for its
baseline and candidates.
