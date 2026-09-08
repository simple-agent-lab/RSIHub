import json

import pytest

from evolve.integrations.harbor._time_budget import (
    agent_time_budget,
    execution_time_budget,
    read_agent_time_budget,
    summarize_time_budgets,
)


@pytest.mark.parametrize(
    ("agent", "phase", "expected"),
    [
        ({}, None, 1800),
        ({}, 0.5, 450),
        ({"override_timeout_sec": 600}, None, 1200),
        ({"override_timeout_sec": 600, "max_timeout_sec": 300}, 3, 900),
    ],
)
def test_effective_limit_applies_cap_before_multiplier(tmp_path, agent, phase, expected):
    task = tmp_path / "task.toml"
    task.write_text("[agent]\ntimeout_sec = 900\n")
    config = {"agent": agent, "timeout_multiplier": 2, "agent_timeout_multiplier": phase}
    assert agent_time_budget(config, task)["limit_s"] == expected


@pytest.mark.parametrize("bad", [None, True, -1, 0, float("nan"), float("inf"), "900"])
def test_invalid_or_missing_metadata_is_unknown(tmp_path, bad):
    config = {"agent": {"override_timeout_sec": bad}, "timeout_multiplier": 1}
    assert agent_time_budget(config)["status"] == "unavailable"
    assert read_agent_time_budget(tmp_path / "agent")["status"] == "unavailable"


def test_budget_reader_uses_trial_metadata_not_global_environment(tmp_path, monkeypatch):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("[agent]\ntimeout_sec = 120\n")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "task": {"path": str(task)},
                "agent": {},
                "timeout_multiplier": 1,
            }
        )
    )
    monkeypatch.setenv("EVOLVE_AGENT_TIMEOUT_SEC", "9999")
    assert read_agent_time_budget(tmp_path / "agent")["limit_s"] == 120


def test_success_near_deadline_and_zero_reward_timeout_remain_distinct():
    config = {"agent": {"override_timeout_sec": 100}, "timeout_multiplier": 1}
    success = execution_time_budget(config, None, 85, "")
    timeout = execution_time_budget(config, None, 101, "AgentTimeoutError")
    unknown = execution_time_budget({}, None, 90, "")
    assert success["fraction_used"] == 0.85
    assert timeout["remaining_s"] == 0
    assert timeout["fraction_used"] == 1.01
    assert "fraction_used" not in unknown
    cases = [
        {"reward": 1, "execution": {"time_budget": success}},
        {"reward": 0, "execution": {"time_budget": timeout}, "exception": {"type": "AgentTimeoutError"}},
        {"reward": 0, "execution": {"time_budget": unknown}},
    ]
    result = summarize_time_budgets(cases)
    assert result["agent_timeout_count"] == 1
    assert result["agent_time_budget"] == {
        "tasks_with_fraction": 2,
        "tasks_without_fraction": 1,
        "max_fraction_used": 1.01,
        "tasks_at_least_80_percent": 2,
    }


def test_budget_reader_accepts_native_config_with_default_fields_omitted(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("[agent]\ntimeout_sec = 900\n")
    # Harbor Trial writes exclude_defaults=True, unlike the final result payload.
    (tmp_path / "config.json").write_text(json.dumps({"task": {"path": str(task)}, "agent": {"name": "codex"}}))
    assert read_agent_time_budget(tmp_path / "agent")["limit_s"] == 900
    config = {"task": {"path": str(task)}, "agent": {}, "timeout_multiplier": None}
    assert agent_time_budget(config)["status"] == "unavailable"
