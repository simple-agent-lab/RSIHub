# PLAYBOOK — improvement methodology of this lineage

This file is owned by the self-improvement engineer (you, at mutation time).
It travels with your lineage: every future generation reads it FIRST.
Distill durable methodology here — not task trivia:

- What kinds of changes reliably help, and why.
- What was tried and failed (so descendants stop re-trying it).
- Open hypotheses worth testing next, ranked.
- How to spend a generation well when evidence is thin.

Keep it under 200 lines. Prune ruthlessly; stale advice is worse than none.

## Lessons

- ENVIRONMENT CONTRACT: the harness owns infrastructure. Environment patching
  earns NO fitness — never evolve bootstrapping/offline-fallback/proxy
  components. Harbor seams (docker-exec bash, pinned model, session home) are
  frozen — leave them out of mutate scope.
- Capability AND efficiency are first-class fitness axes (SoL-Pi-style RSI):
  solve more tasks **and** spend fewer tokens / fewer tool rounds / fewer
  retries to do so. A correct but wasteful trajectory is incomplete progress.
- Mechanism-level changes (tool-pipeline hooks, planning, subagents,
  compaction, memory) beat persona text tweaks. Reach for prose last.
- Prefer smaller observations and tighter loops over longer personas. When
  scores tie, prefer the candidate with lower token spend and fewer steps.
- Rank hypotheses that cut retries and dead-end exploration ahead of those
  that only add skills or wording.

## Efficiency checklist (each generation)

1. Did the parent waste tokens on repeated failed commands? Add diagnosis or
   stop conditions before adding new tools.
2. Are tool observations oversized? Prefer line-range reads and targeted
   greps over dumping whole files.
3. Does the persona encourage unnecessary narration? Trim it; keep verification
   rigor.
4. Host-owned `trajectory.json` → `usage.totals` (when session logs provide
   metering) is evidence for efficiency — do not invent metrics that logs omit.

## Open hypotheses

(none yet)
