"""Declarative configuration for the Harbor rollout operator."""

from __future__ import annotations

from evolve.frozen.config import (
    Config,
    array,
    boolean,
    integer,
    number,
    string,
)
from evolve.frozen.config import (
    object as object_field,
)


def _validate_optional_values(config: dict[str, object]) -> None:
    for key in (
        "agent_setup_timeout_multiplier",
        "agent_timeout_multiplier",
        "verifier_timeout_multiplier",
    ):
        if key in config and float(config[key]) <= 0:
            raise ValueError(f"{key} must be positive")
    if "task_names" in config and not config["task_names"]:
        raise ValueError("task_names must be a non-empty list of task names")


CONFIG = Config(
    {
        "budget_tasks": integer(default=8, minimum=1),
        "task_sampling": string(
            default="head",
            choices=("head", "generation_shuffle"),
        ),
        "n_concurrent": integer(minimum=1),
        "agent_setup_timeout_multiplier": number(),
        "agent_timeout_multiplier": number(),
        "verifier_timeout_multiplier": number(),
        "max_retries": integer(minimum=0),
        "stop_after_errors": integer(minimum=1),
        "stop_after_failures": integer(minimum=1),
        "field_limit": integer(default=2000, minimum=1),
        "pass_threshold": number(default=1.0),
        "environment": string(),
        "environment_kwargs": object_field(additional_properties=True),
        "agent": string(),
        "agent_env": object_field(additional_properties=True),
        "model": string(),
        "include_task_name": string(),
        "jobs_dir": string(),
        "path": string(),
        "split": string(),
        "task_names": array(string()),
        "reuse_completed": boolean(default=False),
        "seed": integer(default=0),
    },
    refine=_validate_optional_values,
)
