/**
 * seed-probe: gen-0's wiring-proof plugin. Being loaded is its only job; it
 * registers no behavior.
 *
 * Rules for writing new plugins (important):
 * - Pure ESM, exporting { name, inject?, apply(ctx, config) }.
 * - Never import '@deepseek-ai/cordis' or any dsh package — external plugins
 *   sharing the runtime's cordis instance is unresolved; take every capability
 *   from the ctx that apply() receives.
 * - List the services you need in the inject array (e.g. ['tools']); cordis
 *   guarantees they are injected before apply runs.
 * - To explore the available API: the mutation session has
 *   cordis_inspect_list / cordis_inspect_query to enumerate services, events
 *   and tool schemas, and cordis_define + cordis_run to trial-run a plugin
 *   draft in memory before writing it to this directory.
 * - At rollout time this plugin runs host-side; the model's bash tool reaches
 *   inside the task container.
 */
export const name = 'evolve-seed-probe'

export function apply(ctx) {
  // Intentionally empty: proves the profile repo's local-plugin channel works.
}
