from pathlib import Path

from evolve.composition import resolve_builtin_recipe
from evolve.config import RECIPE_NAMES, load_config

ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "recipes"
SUPPORTED_RECIPES = {
    "aevolve",
    "ahe",
    "ahe_codex",
    "gepa",
    "gepa_local",
    "hill_climb",
    "hill_climb_codex",
    "hyperagents",
    "hyperagents_codex",
    "hyperagents_dsh",
}
UV_SOURCE_RECIPES = {"ahe", "hill_climb", "hyperagents"}
LOCAL_MUTATE_RECIPES = {"hyperagents_dsh"}
MAIN_RECIPES = SUPPORTED_RECIPES - {"gepa_local"}
TERMINAL_BENCH_DATASET = "terminal-bench-2-30-v1"
CODEX_IMAGE = "evolve-mutate-codex:20260818-codex0146"
MINISWE_IMAGE = "evolve-mutate-app:20260724-tools-mswe245"


def _config(name: str) -> str:
    return (RECIPES / name / "evolve.yaml").read_text()


def _parsed_config(name: str) -> dict[str, object]:
    return load_config(RECIPES / name / "evolve.yaml")


def _operator_config(name: str, stage: str) -> dict[str, object]:
    return resolve_builtin_recipe(name).operators[stage].config


def test_main_recipes_share_terminal_bench_and_explicit_mutate_images() -> None:
    for name in MAIN_RECIPES:
        config = _parsed_config(name)
        assert config["evaluator"]["dataset"] == TERMINAL_BENCH_DATASET
        mutate = _operator_config(name, "mutate")
        if name in LOCAL_MUTATE_RECIPES:
            assert mutate["runner"] == "local"
            assert "image" not in mutate
            continue
        expected_image = MINISWE_IMAGE if name in {"ahe", "hyperagents"} else CODEX_IMAGE
        assert mutate["image"] == expected_image


def test_all_recipes_are_recipe_artifacts_only() -> None:
    recipe_names = tuple(path.name for path in sorted(RECIPES.iterdir()) if path.is_dir())
    assert set(recipe_names) == set(RECIPE_NAMES)
    assert set(RECIPE_NAMES) == SUPPORTED_RECIPES
    for name in RECIPE_NAMES:
        recipe = RECIPES / name
        assert (recipe / "evolve.yaml").is_file()
        assert (recipe / "README.md").is_file()
        assert {path.name for path in recipe.iterdir()} <= {
            "README.md",
            "dataset-manifest.json",
            "evolve.yaml",
            "notes.md",
            "prepare_dataset.py",
            "evaluator",
            "sealed",
        }
        config = _config(name)
        for section in ("experiment:", "target:", "surface:", "operators:", "evaluator:"):
            assert section in config
        top_level_sections = [line.split(":", 1)[0] for line in config.splitlines() if line and not line[0].isspace()]
        expected = ["experiment", "target", "surface", "operators"]
        if "execution_runtime:" in config:
            expected.append("execution_runtime")
        expected.append("evaluator")
        assert top_level_sections == expected


def test_supported_recipes_use_harbor_and_method_mutate() -> None:
    expected_operators = {
        "aevolve": {
            "select": "greedy",
            "rollout": "harbor",
            "analyze": "trajectory_only",
            "mutate": "aevolve",
            "gate": "hillclimb",
            "record": "jsonl",
        },
        "ahe": {
            "select": "ahe_latest",
            "rollout": "parent_evaluation",
            "analyze": "ahe",
            "mutate": "ahe",
            "gate": "ahe_artifact_valid",
            "record": "jsonl",
        },
        "ahe_codex": {
            "select": "ahe_latest",
            "rollout": "parent_evaluation",
            "analyze": "ahe",
            "mutate": "ahe",
            "gate": "ahe_artifact_valid",
            "record": "jsonl",
        },
        "gepa": {
            "select": "pareto",
            "rollout": "harbor",
            "analyze": "gepa",
            "mutate": "gepa",
            "validate": "minibatch_improvement",
            "gate": "parent_eligible",
            "record": "gepa",
        },
        "gepa_local": {
            "select": "pareto",
            "rollout": "harbor",
            "analyze": "gepa",
            "mutate": "gepa",
            "validate": "minibatch_improvement",
            "gate": "parent_eligible",
            "record": "gepa",
        },
        "hill_climb": {
            "select": "greedy",
            "rollout": "harbor",
            "analyze": "failure_patterns",
            "mutate": "hyperagents",
            "gate": "hillclimb",
            "record": "jsonl",
        },
        "hill_climb_codex": {
            "select": "greedy",
            "rollout": "harbor",
            "analyze": "failure_patterns",
            "mutate": "hyperagents",
            "gate": "hillclimb",
            "record": "jsonl",
        },
        "hyperagents": {
            "select": "score_child_prop",
            "rollout": "parent_evaluation",
            "analyze": "trace_browser",
            "mutate": "hyperagents",
            "validate": "hyperagents",
            "gate": "parent_eligible",
            "record": "hyperagents",
        },
        "hyperagents_codex": {
            "select": "score_child_prop",
            "rollout": "parent_evaluation",
            "analyze": "trace_browser",
            "mutate": "hyperagents",
            "validate": "hyperagents",
            "gate": "parent_eligible",
            "record": "hyperagents",
        },
        "hyperagents_dsh": {
            "select": "score_child_prop",
            "rollout": "parent_evaluation",
            "analyze": "trace_browser",
            "mutate": "hyperagents",
            "validate": "node_check",
            "gate": "parent_eligible",
            "record": "hyperagents",
        },
    }
    for name in SUPPORTED_RECIPES:
        resolved = resolve_builtin_recipe(name)
        config = resolved.config
        assert config["evaluator"]["engine"] == "harbor"
        assert "target/**" in config["surface"]["include"]
        assert {stage: binding.name for stage, binding in resolved.operators.items()} == expected_operators[name]
        mutate = resolved.operators["mutate"].config
        assert "evolve_tools" not in mutate
        if name == "aevolve":
            assert config["target"]["seed"] == "builtin-codex"
            assert mutate["trajectory_only"] is True
            assert mutate["evolve_prompts"] is True
            assert mutate["evolve_skills"] is True
            assert mutate["evolve_memory"] is False
        elif name == "gepa":
            assert resolved.operators["rollout"].config["task_sampling"] == "generation_shuffle"
            assert resolved.operators["analyze"].config["components"] == {
                "system_prompt": ["target/prompt.md"],
                "task_execution_skill": ["target/skills/task-execution"],
            }
            assert resolved.operators["validate"].config["criterion"] == "strict"
        elif name == "gepa_local":
            assert config["target"]["seed"] == "builtin-local-smoke"
            assert resolved.operators["validate"].config["criterion"] == "non_decreasing"
            assert mutate["agent"] == "evolve.integrations.harbor.local_auto_agent:LocalAutoAgent"
        elif name in {"ahe", "hyperagents"}:
            assert config["target"]["revision"] == "388da74aad620a384ab47669b17c52133e30e7c3"
            assert mutate["agent"] == "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent"
        elif name == "hyperagents_dsh":
            assert config["target"]["seed"] == "builtin-dsh"
            assert mutate["runner"] == "local"
            assert mutate["command"] == "python3 target/runners/mutate_local.py"
            assert "agent" not in mutate
        else:
            assert mutate["agent"] == "codex"


def test_ahe_and_hyperagents_share_the_pinned_mutate_image() -> None:
    expected = "evolve-mutate-app:20260724-tools-mswe245"
    for name in ("ahe", "hyperagents"):
        assert _operator_config(name, "mutate")["image"] == expected


def test_codex_mutates_use_the_preinstalled_codex_image() -> None:
    expected = "evolve-mutate-codex:20260818-codex0146"
    for name in ("aevolve", "ahe_codex", "gepa", "hill_climb", "hill_climb_codex", "hyperagents_codex"):
        assert _operator_config(name, "mutate")["image"] == expected


def test_terminal_bench_method_recipes_use_full_curated_dataset() -> None:
    expected_datasets = {
        "ahe": TERMINAL_BENCH_DATASET,
        "hyperagents": TERMINAL_BENCH_DATASET,
        "hyperagents_codex": TERMINAL_BENCH_DATASET,
        "hyperagents_dsh": TERMINAL_BENCH_DATASET,
    }
    for name, expected_dataset in expected_datasets.items():
        recipe = _parsed_config(name)
        evaluator = recipe["evaluator"]
        assert isinstance(evaluator, dict)
        assert evaluator["dataset"] == expected_dataset
        assert "split" not in evaluator
        assert evaluator["sampling"] == "static"
        assert evaluator["tasks_per_round"] == 30
        assert evaluator["task_scope"] == "full"
        assert evaluator["evaluation_split"] == "train"
        assert evaluator["repetitions"] == 1
        assert "k" not in evaluator
        assert evaluator["n_concurrent"] == (5 if name == "hyperagents_codex" else 10)
        assert _operator_config(name, "mutate")["expose_gate_data"] is False

    ahe_analyze = _operator_config("ahe", "analyze")
    assert ahe_analyze["max_tasks"] == 30
    assert ahe_analyze["max_concurrent"] == 10


def test_supported_uv_recipes_enable_inline_candidate_runtime_and_task_retry() -> None:
    for name in UV_SOURCE_RECIPES:
        evaluator = _parsed_config(name)["evaluator"]
        assert isinstance(evaluator, dict)
        assert evaluator["runtime"]["candidate"] == {
            "variant": "uv",
            "project": "target",
            "python": "3.12",
        }
        assert "candidate_runtime" not in evaluator
        assert evaluator["max_retries"] == 1
        assert evaluator["benchmark_timeout_is_zero"] is True


def test_shared_optimization_recipes_use_native_candidate_agent_timeout() -> None:
    for name in ("ahe", "hyperagents"):
        evaluator = _parsed_config(name)["evaluator"]
        assert isinstance(evaluator, dict)
        assert evaluator["agent_timeout_multiplier"] == 1


def test_all_explicit_recipe_retry_and_multiplier_values_are_one() -> None:
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if "retry" in key or "retries" in key or "multiplier" in key:
                    expected = 2 if key == "debugger_max_retries" else 1
                    assert item == expected, f"{key} must be {expected}, got {item!r}"
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for name in RECIPE_NAMES:
        config = _parsed_config(name)
        walk(config)
        assert "infra_repair_attempts" not in _config(name)


def test_miniswe_method_agents_use_the_rollout_model_version() -> None:
    expected_model = "openai/gpt-5.4-2026-03-05"
    for name in ("ahe", "hyperagents"):
        config = _parsed_config(name)
        mutate = _operator_config(name, "mutate")
        assert mutate["model"] == expected_model
        evaluator = config["evaluator"]
        assert isinstance(evaluator, dict)
        assert evaluator["model"] == expected_model


def test_mutate_image_provides_harbor_workspace_parent() -> None:
    dockerfile = ROOT / "containers" / "mutate" / "Dockerfile"
    contents = dockerfile.read_text()
    assert contents.startswith(
        "FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90"
    )
    assert "ARG MINISWE_VERSION=2.4.5" in contents
    assert "ARG SOURCE_REVISION=unknown" in contents
    assert "WORKDIR /app" in contents
    assert "\n        python3 \\" in contents
    assert "\n        python-is-python3 \\" in contents
    assert '"mini-swe-agent==${MINISWE_VERSION}"' in contents
    assert 'io.evolve.miniswe.version="${MINISWE_VERSION}"' in contents
    assert 'org.opencontainers.image.revision="${SOURCE_REVISION}"' in contents
    assert "git config --system --add safe.directory /app/task/workspace" in contents
    for package in ("git", "jq", "python3", "python-is-python3", "ripgrep", "rsync"):
        assert f"        {package} \\" in contents
    assert 'uv tool install --python 3.13 --with fastapi --with orjson "mini-swe-agent==${MINISWE_VERSION}"' in contents
    assert "COPY uv-wrapper /root/.local/bin/uv" in contents

    wrapper = (dockerfile.parent / "uv-wrapper").read_text()
    assert '"$1" = "tool"' in wrapper
    assert '"$2" = "install"' in wrapper
    assert 'version="${EVOLVE_MINISWE_VERSION:-2.4.5}"' in wrapper
    assert 'uv-real tool install --python 3.13 --with fastapi --with orjson "mini-swe-agent==$version"' in wrapper


def test_mutate_required_tools_match_tier_zero_contract() -> None:
    tools = (ROOT / "containers" / "mutate" / "required-tools.txt").read_text().splitlines()
    assert tools == [
        "bash",
        "git",
        "curl",
        "diff",
        "file",
        "find",
        "jq",
        "patch",
        "python",
        "rg",
        "rsync",
        "sed",
        "tree",
        "uv",
        "mini-swe-agent",
    ]


def test_codex_mutate_image_pins_the_mutator_cli_version() -> None:
    dockerfile = (ROOT / "containers" / "mutate-codex" / "Dockerfile").read_text()
    assert dockerfile.startswith(
        "FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46"
    )
    assert "FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90" in dockerfile
    assert "ARG CODEX_VERSION=0.146.0" in dockerfile
    assert 'npm install --global "@openai/codex@${CODEX_VERSION}"' in dockerfile
    assert dockerfile.count("&& codex --version") == 2
    assert "COPY --from=codex-build /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert 'io.evolve.codex.version="${CODEX_VERSION}"' in dockerfile
    assert "git config --system --add safe.directory /app/task/workspace" in dockerfile


def test_ahe_recipe_configures_reasoning_without_cost_caps() -> None:
    recipe = _parsed_config("ahe")
    assert _operator_config("ahe", "mutate")["agent_kwargs"] == {
        "reasoning_effort": "high",
        "cost_limit": 0,
        "max_tokens": 64_000,
    }
    assert recipe["evaluator"]["agent_env"]["MINISWE_REASONING_EFFORT"] == "high"
    assert recipe["evaluator"]["agent_env"]["MINISWE_COST_LIMIT"] == "0"


def test_hyperagents_recipe_configures_reasoning_without_cost_caps() -> None:
    recipe = _parsed_config("hyperagents")
    assert _operator_config("hyperagents", "mutate")["agent_kwargs"] == {
        "reasoning_effort": "high",
        "cost_limit": 0,
        "max_tokens": 64_000,
    }
    assert "budget_usd" not in recipe["experiment"]
    assert recipe["evaluator"]["agent_env"] == {
        "MINISWE_COST_LIMIT": "0",
        "MINISWE_ENV_TIMEOUT": "30",
        "MINISWE_REASONING_EFFORT": "high",
        "MINISWE_STEP_LIMIT": "100",
    }


def test_recipe_retry_and_partial_floor_defaults_remain_method_specific() -> None:
    for name in ("ahe", "hill_climb", "hill_climb_codex", "hyperagents", "hyperagents_codex", "hyperagents_dsh"):
        evaluator = _parsed_config(name)["evaluator"]
        assert evaluator["max_retries"] == 1
        assert evaluator["partial_floor"] == 0.8


def test_harbor_evaluator_accepts_validated_runtime_concurrency_override() -> None:
    contents = (ROOT / "scaffolds/evaluators/harbor/engine.sh").read_text()
    assert "EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE" in contents
    assert "invalid EVOLVE_HARBOR_N_CONCURRENT_OVERRIDE" in contents
