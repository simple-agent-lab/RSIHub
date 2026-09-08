"""Host-reviewed public artifact protocol; never loaded from a candidate.

Version 1 preserves the existing stage outputs and aggregate-only projection.
Adding a field requires review of its information exposure.
"""

SCHEMA_VERSION = 1
FEEDBACK_FILES = ("rollout/summary.json", "analyze/summary.json", "mutate/usage.json")

OUTPUTS = {
    "select": ("parents.json",),
    "rollout": ("rollout",),
    "analyze": ("analyze",),
    "mutate": ("mutate",),
    "validate": ("validate",),
    "novelty": ("novelty", "novelty.json"),
    "gate": ("gate.json",),
    "record": ("record",),
    "reflect": (),
}


ROW_FIELDS = {
    "genid",
    "parent",
    "tag",
    "candidate_commit",
    "target_tree",
    "operators_tree",
    "evaluator_tree",
    "task_set_hash",
    "score",
    "cost_usd",
    "valid_parent",
    "kind",
    "round",
    "purpose",
    "status",
    "outcome",
    "eval_scope",
    "n_tasks",
    "n_trials",
}
METRICS = {
    "score",
    "accuracy",
    "pass_rate",
    "cost_usd",
    "usd",
    "total_cost_usd",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "n_tasks",
    "total_tasks",
    "passed_tasks",
    "failed_tasks",
    "tasks_total",
    "tasks_passed",
    "tasks_failed",
    "agent_errors",
    "infra_errors",
    "incomplete_tasks",
    "agent_timeout_count",
    "n_trials",
    "expected_trials",
    "scoreable_trials",
}
