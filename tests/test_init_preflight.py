from pathlib import Path

from conftest import FIXTURE_RECIPES, FIXTURE_SEEDS, run_evolve

SMOKE_RECIPE = str(FIXTURE_RECIPES / "hill_climb-smoke" / "evolve.yaml")
DUMMY_SEED = str(FIXTURE_SEEDS / "dummy")
TASKS_LOCAL = FIXTURE_RECIPES.parent / "tasks-local"


def test_preflight_passes_with_valid_inputs(tmp_path: Path) -> None:
    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe-path",
        SMOKE_RECIPE,
        "--seed",
        DUMMY_SEED,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ready for `evolve init`" in result.stdout
    assert not (tmp_path / "ws").exists()


def test_preflight_blocks_missing_runtime_digest(tmp_path: Path) -> None:
    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe-path",
        SMOKE_RECIPE,
        "--seed",
        DUMMY_SEED,
        env={"EVOLVE_RUNTIME_DIGEST": None},
    )

    assert result.returncode == 1
    assert "EVOLVE_RUNTIME_DIGEST" in result.stdout
    assert "blocking" in result.stdout


def test_preflight_rejects_test_only_seed(tmp_path: Path) -> None:
    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe-path",
        SMOKE_RECIPE,
        "--seed",
        "builtin-dummy",
    )

    assert result.returncode == 1
    assert "test-only" in result.stdout


def test_preflight_rejects_unknown_recipe(tmp_path: Path) -> None:
    result = run_evolve("preflight", str(tmp_path / "ws"), "--recipe", "nope")

    assert result.returncode == 1
    assert "unsupported recipe" in result.stdout
    assert "hill_climb" in result.stdout


def test_preflight_rejects_missing_seed_directory(tmp_path: Path) -> None:
    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe-path",
        SMOKE_RECIPE,
        "--seed",
        str(tmp_path / "no-such-seed"),
    )

    assert result.returncode == 1
    assert "seed directory does not exist" in result.stdout


def test_preflight_rejects_initialized_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "evolve.yaml").touch()
    (workspace / ".git").mkdir()

    result = run_evolve(
        "preflight",
        str(workspace),
        "--recipe-path",
        SMOKE_RECIPE,
        "--seed",
        DUMMY_SEED,
    )

    assert result.returncode == 1
    assert "already initialized" in result.stdout


def test_preflight_default_recipe_needs_no_external_seed(tmp_path: Path) -> None:
    result = run_evolve("preflight", str(tmp_path / "ws"), "--dataset", str(TASKS_LOCAL))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "gepa" in result.stdout
    assert "builtin-codex" in result.stdout
    assert "git URL" not in result.stdout


def test_preflight_accepts_builtin_dsh_seed_for_hyperagents_dsh(tmp_path: Path) -> None:
    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe",
        "hyperagents_dsh",
        "--dataset",
        str(TASKS_LOCAL),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "hyperagents_dsh" in result.stdout
    assert "builtin-dsh" in result.stdout
    assert "seed directory does not exist" not in result.stdout


def test_preflight_blocks_harbor_rollout_without_local_dataset(tmp_path: Path) -> None:
    result = run_evolve("preflight", str(tmp_path / "ws"), "--recipe", "gepa_local")

    assert result.returncode == 1
    assert "harbor rollout" in result.stdout
    assert "--dataset" in result.stdout


def test_preflight_rejects_dataset_harbor_cannot_discover(tmp_path: Path) -> None:
    dataset = tmp_path / "tasks"
    for index in range(10):
        task = dataset / f"task-{index}"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(f'[metadata]\nname = "task-{index}"\n')

    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe",
        "gepa_local",
        "--dataset",
        str(dataset),
    )

    assert result.returncode == 1
    assert "no directory Harbor discovers as a task" in result.stdout


def test_preflight_accepts_valid_local_task_dataset(tmp_path: Path) -> None:
    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe",
        "gepa_local",
        "--dataset",
        str(TASKS_LOCAL),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "builtin-local-smoke" in result.stdout
    assert "(10 tasks)" in result.stdout


def test_preflight_accepts_valid_harbor_task_as_dataset_root(tmp_path: Path) -> None:
    import yaml

    config = yaml.safe_load((Path(__file__).parents[1] / "recipes/gepa_local/evolve.yaml").read_text())
    config["evaluator"]["split"] = {"train": 1.0, "gate": 0.0, "sealed": 0.0, "seed": 0}
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    (recipe_dir / "evolve.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe-path",
        str(recipe_dir),
        "--dataset",
        str(TASKS_LOCAL / "task-0"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "(1 tasks)" in result.stdout


def test_preflight_writes_nothing(tmp_path: Path) -> None:
    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe-path",
        SMOKE_RECIPE,
        "--seed",
        "builtin-dummy",
    )

    assert result.returncode == 1
    assert list(tmp_path.iterdir()) == []


def test_preflight_rejects_recipe_with_omitted_seed(tmp_path: Path) -> None:
    import yaml

    config = yaml.safe_load(Path(SMOKE_RECIPE).read_text())
    config["target"] = {}
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    (recipe_dir / "evolve.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    result = run_evolve(
        "preflight",
        str(tmp_path / "ws"),
        "--recipe-path",
        str(recipe_dir),
    )

    assert result.returncode == 1
    assert "target.seed is required" in result.stdout


def test_preflight_recipe_failure_short_circuits_later_checks(tmp_path: Path) -> None:
    import yaml

    config = yaml.safe_load(Path(SMOKE_RECIPE).read_text())
    config["operators"]["select"] = {"variant": "greedy"}
    del config["operators"]["record"]
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    recipe = recipe_dir / "evolve.yaml"
    recipe.write_text(yaml.safe_dump(config, sort_keys=False))
    workspace = tmp_path / "workspace"

    result = run_evolve(
        "preflight",
        str(workspace),
        "--recipe-path",
        str(recipe),
        "--seed",
        str(tmp_path / "missing-seed"),
        "--dataset",
        str(tmp_path / "missing-dataset"),
    )

    assert result.returncode == 1
    assert "operators.select.variant:" in result.stdout
    assert "operators.record:" in result.stdout
    assert "seed directory does not exist" not in result.stdout
    assert "does not resolve to a local task directory" not in result.stdout
    assert not workspace.exists()


def test_preflight_yaml_set_config_uses_recipe_failure_and_short_circuits(tmp_path: Path) -> None:
    import yaml

    config = yaml.safe_load(Path(SMOKE_RECIPE).read_text())
    config["operators"]["mutate"]["config"]["agent_kwargs"] = {"opaque": "YAML_VALUE"}
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    recipe = recipe_dir / "evolve.yaml"
    rendered = yaml.safe_dump(config, sort_keys=False)
    assert "opaque: YAML_VALUE" in rendered
    recipe.write_text(rendered.replace("opaque: YAML_VALUE", "opaque: !!set {alpha: null}"))
    workspace = tmp_path / "workspace"

    result = run_evolve(
        "preflight",
        str(workspace),
        "--recipe-path",
        str(recipe),
        "--seed",
        str(tmp_path / "missing-seed"),
        "--dataset",
        str(tmp_path / "missing-dataset"),
    )

    assert result.returncode == 1
    assert "operators.mutate.config:" in result.stdout
    assert "not JSON-serializable" in result.stdout
    assert "seed directory does not exist" not in result.stdout
    assert "does not resolve to a local task directory" not in result.stdout
    assert "TypeError" not in result.stderr
    assert not workspace.exists()
