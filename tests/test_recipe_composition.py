import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import fixture_recipe_config, generated_workspace_uv_env, run_evolve, write_locked_miniswe_seed

from evolve.config import (
    RECIPE_NAMES,
    default_config,
    library_root,
    load_config,
    recipe_root,
    render_yaml,
    scaffold_root,
    seed_root,
)
from evolve.workspace import InitOptions, _write_target, init_workspace

CANDIDATE = "evolve.integrations.harbor.miniswe_candidate:CandidateMiniSweAgent"
OPTIONAL_INTEGRATIONS = {"evolve.integrations.harbor.codex_candidate"}
FILE_TASK = "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CASES = {
    "hill_climb": ("external", CANDIDATE, "codex"),
    "aevolve": ("codex", "target.agent:HarborAgent", "codex"),
    "ahe": ("external", CANDIDATE, FILE_TASK),
    "ahe_codex": ("codex", "target.agent:HarborAgent", "codex"),
    "gepa": ("codex", "target.agent:HarborAgent", "codex"),
    "hyperagents": ("external", CANDIDATE, FILE_TASK),
    "hill_climb_codex": ("codex", "target.agent:HarborAgent", "codex"),
    "hyperagents_codex": ("codex", "target.agent:HarborAgent", "codex"),
}
MINISWE_REVISION = "388da74aad620a384ab47669b17c52133e30e7c3"


def _local_dataset(root: Path, count: int = 10) -> Path:
    root.mkdir()
    for index in range(count):
        task = root / f"task-{index}"
        task.mkdir()
        (task / "task.toml").write_text(f'version = "1.0"\nname = "task-{index}"\n')
    return root


def init_custom_recipe(
    tmp_path: Path,
    *,
    mutate_operator: str,
    mutate_config: dict[str, object],
) -> Path:
    recipe = tmp_path / "custom-recipe"
    recipe.mkdir()
    config = fixture_recipe_config("hill_climb-smoke", "custom-recipe")
    config["operators"]["mutate"] = {
        "operator": mutate_operator,
        "config": mutate_config,
    }
    (recipe / "evolve.yaml").write_text(render_yaml(config))
    workspace = tmp_path / "custom-workspace"
    init_workspace(InitOptions(workspace=workspace, recipe_path=recipe))
    return workspace


def test_init_freezes_resolved_operator_identity_and_nested_config(tmp_path: Path) -> None:
    workspace = init_custom_recipe(
        tmp_path,
        mutate_operator="hyperagents",
        mutate_config={"runner": "local"},
    )

    rendered = load_config(workspace / "evolve.yaml")
    assert rendered["operators"]["mutate"] == {
        "operator": "hyperagents",
        "timeout_s": 600.0,
        "config": {
            "runner": "local",
            "editable_roots": ["target"],
            "expose_gate_data": False,
            "max_retries": 0,
        },
    }
    manifest = json.loads((workspace / ".evolve-components.json").read_text())
    assert manifest["operators"]["mutate"]["name"] == "hyperagents"
    assert (
        manifest["operators"]["mutate"]["sha256"]
        == hashlib.sha256((workspace / "operators/mutate.py").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(("recipe", "expected"), sorted(CASES.items()))
def test_supported_recipe_initializes_only_selected_components(
    tmp_path: Path, recipe: str, expected: tuple[str, str, str]
) -> None:
    target_kind, evaluator_agent, mutate = expected
    workspace = tmp_path / recipe
    seed = None
    if target_kind == "external":
        seed = str(write_locked_miniswe_seed(tmp_path / f"{recipe}-seed"))
    init_workspace(InitOptions(workspace=workspace, recipe=recipe, seed=seed))

    rendered = load_config(workspace / "evolve.yaml")
    components = json.loads((workspace / ".evolve-components.json").read_text())
    assert rendered["evaluator"]["agent"] == evaluator_agent
    assert rendered["operators"]["mutate"]["config"]["agent"] == mutate
    assert components["recipe"] == recipe
    assert components["target_seed"] == rendered["target"]["seed"]
    assert components["evaluator_engine"] == "harbor"
    assert components["integrations"] == sorted(
        {ref.split(":", 1)[0] for ref in (evaluator_agent, mutate) if ref.startswith("evolve.integrations.")}
    )
    assert (workspace / "target" / "codex.toml").is_file() is (target_kind == "codex")
    assert (workspace / "evaluator" / "cleanup_harbor.py").is_file()
    assert (workspace / ".evolve" / "evolve" / "integrations" / "harbor" / "codex_candidate.py").is_file()
    assert (workspace / ".evolve" / "evolve" / "integrations" / "harbor" / "miniswe_candidate.py").is_file()
    assert not (workspace / "evolve_harbor_adapter").exists()
    assert not (workspace / "evolve_harbor_agent").exists()

    sync = subprocess.run(
        ["uv", "sync", "--project", str(workspace), "--frozen", "--offline"],
        text=True,
        capture_output=True,
        check=False,
        env=generated_workspace_uv_env(),
    )
    assert sync.returncode == 0, sync.stderr

    for reference in components["integrations"]:
        probe = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(workspace),
                "--frozen",
                "python",
                "-c",
                f"import importlib; importlib.import_module({reference!r})",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=generated_workspace_uv_env(),
        )
        assert probe.returncode == 0, probe.stderr


def test_hyperagents_dsh_recipe_initializes_builtin_seed_and_local_mutate(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents_dsh"
    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe="hyperagents_dsh",
            dataset=str(_local_dataset(tmp_path / "tasks")),
        )
    )

    rendered = load_config(workspace / "evolve.yaml")
    assert rendered["target"] == {"seed": "builtin-dsh"}
    assert rendered["evaluator"]["agent"] == "target.agent:DshAgent"
    mutate = rendered["operators"]["mutate"]["config"]
    assert mutate["runner"] == "local"
    assert mutate["command"] == "python3 target/runners/mutate_local.py"
    assert "agent" not in mutate
    for relative in (
        "agent.py",
        "dsh_trajectory.py",
        "profile.cordis.yml",
        "runners/mutate_local.py",
        "runners/compositions/rollout.base.cordis.yml",
        "runners/compositions/mutate.cordis.yml",
    ):
        assert (workspace / "target" / relative).is_file()
    components = json.loads((workspace / ".evolve-components.json").read_text())
    assert components["recipe"] == "hyperagents_dsh"
    assert components["target_seed"] == "builtin-dsh"
    assert components["evaluator_engine"] == "harbor"
    assert components["integrations"] == []
    assert not (workspace / "target" / "codex.toml").exists()
    assert (workspace / "evaluator" / "cleanup_harbor.py").is_file()


def test_recipe_path_rejects_recipe_local_operators_before_workspace_writes(tmp_path: Path) -> None:
    recipe = tmp_path / "custom-path-recipe"
    shutil.copytree(Path(recipe_root()) / "hill_climb", recipe)
    local_select = recipe / "operators" / "select"
    (local_select / "prompts").mkdir(parents=True)
    (local_select / "greedy.py").write_text('"""Recipe-local select."""\nLOCAL_RECIPE_OPERATOR = True\n')
    (local_select / "prompts" / "decision.md").write_text("LOCAL RECIPE PROMPT\n")
    workspace = tmp_path / "recipe-path-workspace"

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe-path",
        str(recipe / "evolve.yaml"),
    )

    assert result.returncode == 1
    assert "recipe-local operator libraries are not supported" in result.stderr
    assert not workspace.exists()


def test_init_copies_frozen_library_helpers_and_active_provenance(tmp_path: Path) -> None:
    workspace = init_custom_recipe(
        tmp_path,
        mutate_operator="hyperagents",
        mutate_config={"runner": "local"},
    )

    assert (workspace / "library/_shared/artifacts.py").read_bytes() == (
        Path(library_root()) / "_shared/artifacts.py"
    ).read_bytes()
    assert (workspace / "library/_shared/runners/harbor.py").read_bytes() == (
        Path(library_root()) / "_shared/runners/harbor.py"
    ).read_bytes()
    assert not (workspace / "library/mutate/_skeleton.py").exists()
    first_line = (workspace / "operators/mutate.py").read_text().splitlines()[0]
    assert "kind=mutate source=library/mutate/hyperagents.py" in first_line
    assert str(Path(library_root()).resolve()) not in first_line


def test_init_copies_binary_shared_and_stage_local_helper_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolve import workspace as workspace_module
    from evolve.composition import materialize as materialize_module
    from evolve.composition import recipe as recipe_module

    library = tmp_path / "library"
    shutil.copytree(Path(library_root()), library, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shared_bytes = b"\x00\xffSHARED\x80"
    stage_bytes = b"\x89PNG\r\n\x1a\n\x00\xff"
    (library / "_shared/fixtures").mkdir()
    (library / "_shared/fixtures/payload.bin").write_bytes(shared_bytes)
    (library / "mutate/_support/payload.bin").write_bytes(stage_bytes)
    root_helper = b"ROOT_HELPER = True\n"
    root_asset = b"\x00\xffROOT\x80"
    (library / "_root_helper.py").write_bytes(root_helper)
    (library / "_root_assets").mkdir()
    (library / "_root_assets/payload.bin").write_bytes(root_asset)
    monkeypatch.setattr(materialize_module, "library_root", lambda: library)
    monkeypatch.setattr(recipe_module, "library_root", lambda: library)
    monkeypatch.setattr(workspace_module, "library_root", lambda: library)

    workspace = init_custom_recipe(
        tmp_path,
        mutate_operator="hyperagents",
        mutate_config={"runner": "local"},
    )

    assert (workspace / "library/_shared/fixtures/payload.bin").read_bytes() == shared_bytes
    assert (workspace / "library/mutate/_support/payload.bin").read_bytes() == stage_bytes
    assert (workspace / "library/_root_helper.py").read_bytes() == root_helper
    assert (workspace / "library/_root_assets/payload.bin").read_bytes() == root_asset


def test_init_freezes_script_with_redacted_nonportable_provenance(tmp_path: Path) -> None:
    recipe = tmp_path / "credentials-should-not-appear" / "script-recipe"
    recipe.mkdir(parents=True)
    source = recipe / "private-select.py"
    source.write_text("print('private select')\n")
    config = fixture_recipe_config("hill_climb-smoke", "script-recipe")
    config["operators"]["select"] = {
        "script": str(source),
        "timeout_s": 17,
        "config": {"opaque": True},
    }
    (recipe / "evolve.yaml").write_text(render_yaml(config))
    workspace = tmp_path / "script-workspace"

    init_workspace(InitOptions(workspace=workspace, recipe_path=recipe))

    active = (workspace / "operators/select.py").read_text()
    manifest_text = (workspace / ".evolve-components.json").read_text()
    manifest = json.loads(manifest_text)
    assert "source=script/private-select.py" in active.splitlines()[0]
    assert "credentials-should-not-appear" not in active.splitlines()[0]
    assert manifest["operators"]["select"] == {
        "stage": "select",
        "source": "script",
        "name": "private-select.py",
        "sha256": hashlib.sha256((workspace / "operators/select.py").read_bytes()).hexdigest(),
        "portable": False,
    }
    assert "credentials-should-not-appear" not in manifest_text


def test_init_refuses_operator_source_changed_after_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evolve import workspace as workspace_module

    recipe = tmp_path / "mutating-script-recipe"
    recipe.mkdir()
    source = recipe / "select.py"
    source.write_text("print('resolved source')\n")
    config = fixture_recipe_config("hill_climb-smoke", "mutating-script-recipe")
    config["operators"]["select"] = {"script": "select.py", "config": {}}
    (recipe / "evolve.yaml").write_text(render_yaml(config))
    real_write_target = workspace_module._write_target

    def write_target_then_mutate(workspace: Path, target: dict[str, object]) -> None:
        real_write_target(workspace, target)
        source.write_text("print('changed after resolution')\n")

    monkeypatch.setattr(workspace_module, "_write_target", write_target_then_mutate)
    workspace = tmp_path / "mutating-script-workspace"

    with pytest.raises(ValueError, match=r"operators\.select source changed after recipe resolution"):
        init_workspace(InitOptions(workspace=workspace, recipe_path=recipe))

    assert not (workspace / ".evolve-components.json").exists()
    assert not (workspace / "operators/select.py").exists()


def test_recipe_name_and_path_are_mutually_exclusive(tmp_path: Path) -> None:
    result = run_evolve(
        "init",
        str(tmp_path / "workspace"),
        "--recipe",
        "ahe",
        "--recipe-path",
        str(Path(recipe_root()) / "hill_climb"),
    )

    assert result.returncode == 2
    assert "cannot combine --recipe with --recipe-path" in ANSI_ESCAPE.sub("", result.stderr)


@pytest.mark.parametrize("reserved_name", ["eval.sh", "splits.json"])
def test_recipe_path_rejects_evaluator_assets_that_replace_generated_files(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    recipe = tmp_path / f"colliding-{reserved_name.replace('.', '-')}-recipe"
    shutil.copytree(Path(recipe_root()) / "hill_climb", recipe)
    collision = recipe / "evaluator" / reserved_name
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_text("RECIPE MUST NOT REPLACE GENERATED CONTENT\n")
    seed = write_locked_miniswe_seed(tmp_path / f"{reserved_name}-seed")
    workspace = tmp_path / f"{reserved_name}-workspace"

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe-path",
        str(recipe),
        "--seed",
        str(seed),
    )

    assert result.returncode == 1
    assert f"recipe evaluator asset collides with generated file: evaluator/{reserved_name}" in result.stderr
    assert not (workspace / "evaluator" / reserved_name).exists()


def test_recipe_path_rejects_case_variant_of_generated_evaluator_asset(
    tmp_path: Path,
) -> None:
    recipe = tmp_path / "case-colliding-recipe"
    shutil.copytree(Path(recipe_root()) / "hill_climb", recipe)
    collision = recipe / "evaluator" / "EVAL.SH"
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_text("RECIPE MUST NOT REPLACE GENERATED CONTENT\n")
    seed = write_locked_miniswe_seed(tmp_path / "case-collision-seed")
    workspace = tmp_path / "case-collision-workspace"

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe-path",
        str(recipe),
        "--seed",
        str(seed),
    )

    assert result.returncode == 1
    assert "recipe evaluator asset collides with generated file: evaluator/EVAL.SH" in result.stderr
    assert not (workspace / "evaluator" / "eval.sh").exists()


@pytest.mark.parametrize("recipe", sorted(CASES))
def test_dataset_override_preserves_recipe_target_and_integrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recipe: str
) -> None:
    from evolve import workspace as workspace_module

    clone_source = write_locked_miniswe_seed(tmp_path / "clone-source")

    def clone_reviewed_miniswe(url: str, destination: Path, *, revision: str | None = None) -> None:
        assert url == "https://github.com/SWE-agent/mini-swe-agent.git"
        assert revision == MINISWE_REVISION
        shutil.copytree(clone_source, destination)

    monkeypatch.setattr(workspace_module, "_git_clone", clone_reviewed_miniswe)
    workspace = tmp_path / recipe
    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe=recipe,
            dataset=str(_local_dataset(tmp_path / "tasks")),
        )
    )

    rendered = load_config(workspace / "evolve.yaml")
    target_kind, evaluator_agent, mutate = CASES[recipe]
    expected_target = (
        {
            "seed": "https://github.com/SWE-agent/mini-swe-agent.git",
            "revision": MINISWE_REVISION,
            "generate_lock": True,
        }
        if target_kind == "external"
        else {"seed": "builtin-codex"}
    )
    assert rendered["target"] == expected_target
    assert rendered["evaluator"]["agent"] == evaluator_agent
    assert rendered["operators"]["mutate"]["config"]["agent"] == mutate
    assert json.loads((workspace / ".evolve-components.json").read_text())["integrations"] == sorted(
        {
            reference.split(":", 1)[0]
            for reference in (evaluator_agent, mutate)
            if reference.startswith("evolve.integrations.")
        }
    )


def test_every_production_resource_has_a_supported_consumer() -> None:
    configs = [load_config(recipe_root() / name / "evolve.yaml") for name in RECIPE_NAMES]
    engines = {str(config["evaluator"]["engine"]) for config in configs}
    builtin_seeds = {
        str(config["target"]["seed"]).removeprefix("builtin-")
        for config in configs
        if str(config["target"].get("seed", "")).startswith("builtin-")
    }
    agent_refs = {str(config["evaluator"].get("agent", "")) for config in configs} | {
        str(config["operators"]["mutate"].get("config", {}).get("agent", "")) for config in configs
    }

    evaluator_scaffolds = {path.name for path in (scaffold_root() / "evaluators").iterdir() if path.is_dir()}
    seeds = {path.name for path in seed_root().iterdir() if path.is_dir()}
    integration_modules = {
        f"evolve.integrations.harbor.{path.name[:-3]}"
        for path in (Path(__file__).resolve().parents[1] / "src" / "evolve" / "integrations" / "harbor").glob("*.py")
        if path.name != "__init__.py" and not path.name.startswith("_")
    }

    assert evaluator_scaffolds == engines
    assert seeds == builtin_seeds
    configured_integrations = {ref.split(":", 1)[0] for ref in agent_refs if ref.startswith("evolve.integrations.")}
    assert integration_modules == configured_integrations | OPTIONAL_INTEGRATIONS


COMMON_OUTPUTS = {
    ".gitignore": ".gitignore",
    ".python-version": ".python-version",
    "AGENTS.md": "AGENTS.md",
    "README.md": "README.md",
    "launch_evolve.py": ".evolve/launch_evolve.py",
    "launch_splits.py": ".evolve/launch_splits.py",
    "operators/preflight.sh": "operators/preflight.sh",
    "operators/gate.md": "operators/gate.md",
    "operators/record.md": "operators/record.md",
    "operators/rollout.md": "operators/rollout.md",
    "operators/select.md": "operators/select.md",
    "program.md": "program.md",
    "pyproject.toml": "pyproject.toml",
    "uv.lock": "uv.lock",
    "evaluator/stub_eval.py": "evaluator/stub_eval.py",
}
HARBOR_OUTPUTS = {
    "cleanup_harbor.py": "evaluator/cleanup_harbor.py",
    "harbor_artifacts.py": "evaluator/harbor_artifacts.py",
    "parse_score.py": "evaluator/parse_score.py",
    "smoke.sh": "evaluator/smoke.sh",
}


def _relative_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def test_every_scaffold_resource_is_rendered_into_a_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "aevolve"
    init_workspace(InitOptions(workspace=workspace, recipe="aevolve"))
    common = Path(scaffold_root()) / "workspace"
    harbor = Path(scaffold_root()) / "evaluators" / "harbor"

    assert _relative_files(common) == set(COMMON_OUTPUTS) | {"evaluator/eval-prefix.sh"}
    assert _relative_files(harbor) == set(HARBOR_OUTPUTS) | {"engine.sh"}
    for source, destination in COMMON_OUTPUTS.items():
        assert (workspace / destination).read_bytes() == (common / source).read_bytes()
    for source, destination in HARBOR_OUTPUTS.items():
        assert (workspace / destination).read_bytes() == (harbor / source).read_bytes()
    assert (workspace / "evaluator/eval.sh").read_text() == (
        (common / "evaluator/eval-prefix.sh").read_text() + (harbor / "engine.sh").read_text()
    )


def test_initialization_reports_selecting_configuration_fields(tmp_path: Path) -> None:
    def init_config(config: dict[str, object], destination: Path) -> None:
        recipe = tmp_path / f"{destination.name}-recipe"
        recipe.mkdir()
        (recipe / "evolve.yaml").write_text(render_yaml(config))
        init_workspace(InitOptions(workspace=destination, recipe_path=recipe))

    with pytest.raises(ValueError, match=r"unsupported recipe: unknown"):
        default_config("unknown", "experiment")

    missing_agent = fixture_recipe_config("hill_climb-smoke", "missing-agent")
    missing_agent["evaluator"].pop("agent")
    with pytest.raises(ValueError, match=r"evaluator\.agent is required"):
        init_config(missing_agent, tmp_path / "missing-agent")

    missing_engine = fixture_recipe_config("hill_climb-smoke", "missing-engine")
    missing_engine["evaluator"]["engine"] = "missing"
    with pytest.raises(ValueError, match=r"unsupported evaluator\.engine: missing"):
        init_config(missing_engine, tmp_path / "missing-engine")

    with pytest.raises(ValueError, match=r"seed is not a local directory or git URL"):
        _write_target(tmp_path / "missing-seed", {"seed": str(tmp_path / "absent")})

    missing_seed = fixture_recipe_config("hill_climb-smoke", "required-seed")
    missing_seed["target"].pop("seed")
    destination = tmp_path / "required-seed"
    with pytest.raises(ValueError, match=r"target\.seed is required"):
        init_config(missing_seed, destination)
    assert not destination.exists()
