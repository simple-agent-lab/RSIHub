import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from harbor.models.registry import DatasetMetadata
from harbor.models.task.id import PackageTaskId

from evolve import evaluation as evaluation_package
from evolve.archive import merged_rows as mechanism_merged_rows
from evolve.archive import mirror_path
from evolve.config import load_config, render_yaml
from evolve.workspace import InitOptions
from evolve.workspace import init_workspace as create_workspace

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RECIPES = ROOT / "tests" / "fixtures" / "recipes"
FIXTURE_SEEDS = ROOT / "tests" / "fixtures" / "seeds"
UV_SOURCE_RECIPES = {"ahe", "hill_climb", "hyperagents", "hyperagents_tbench_full"}


class _FixtureRegistryClient:
    async def get_dataset_metadata(self, name: str) -> DatasetMetadata:
        dataset_name, _, requested_version = name.partition("@")
        return DatasetMetadata(
            name=dataset_name,
            version=requested_version or "test-v1",
            task_ids=[
                PackageTaskId(
                    org="fixture",
                    name=f"task-{index}",
                    ref=f"sha256:{index:064x}",
                )
                for index in range(100)
            ],
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run tests marked slow (disabled by default)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-slow"):
        return

    skip_slow = pytest.mark.skip(reason="slow test; pass --run-slow when this workflow is in scope")
    for item in items:
        if item.get_closest_marker("slow") is not None:
            item.add_marker(skip_slow)


# The suite asserts exact planned environments and redacted receipts, so a
# contributor's ambient credentials, proxy settings, or model overrides must
# never leak into test processes. Tests that need one of these set it
# explicitly via monkeypatch.
_AMBIENT_ENV_VARS = (
    "ALL_PROXY",
    "CODEX_AUTH_JSON_PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "OPENAI_AUTH_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)


@pytest.fixture(autouse=True, scope="session")
def _hermetic_ambient_environment() -> None:
    for name in _AMBIENT_ENV_VARS:
        os.environ.pop(name, None)
        os.environ.pop(name.lower(), None)


@pytest.fixture(autouse=True, scope="session")
def _dependency_tool_shims(tmp_path_factory: pytest.TempPathFactory) -> None:
    # Preflight requires docker and harbor on PATH for harbor-engine
    # workspaces, but the default tier never invokes the real daemon (its
    # evaluations run under EVAL_STUB). Provide inert shims for tools the host
    # lacks — macOS CI runners have no Docker — appended after the real PATH
    # so genuine installations always win.
    shim_dir: Path | None = None
    for tool in ("docker", "harbor"):
        if shutil.which(tool) is not None:
            continue
        if shim_dir is None:
            shim_dir = tmp_path_factory.mktemp("tool-shims")
        shim = shim_dir / tool
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(0o755)
    if shim_dir is not None:
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(shim_dir)


def _uv_directory(*arguments: str) -> str:
    result = subprocess.run(
        ["uv", *arguments],
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


_UV_RUNTIME_ENV = {
    "UV_PYTHON_INSTALL_DIR": _uv_directory("python", "dir"),
    "UV_CACHE_DIR": _uv_directory("cache", "dir"),
}


def generated_workspace_uv_env() -> dict[str, str]:
    return {**os.environ, **_UV_RUNTIME_ENV}


def fixture_recipe_config(name: str, experiment_id: str) -> dict[str, Any]:
    config = copy.deepcopy(load_config(FIXTURE_RECIPES / name / "evolve.yaml"))
    config["experiment"]["id"] = experiment_id
    config["target"]["seed"] = str(FIXTURE_SEEDS / "dummy")
    return config


def init_fixture_workspace(workspace: Path, name: str = "hill_climb-smoke") -> Path:
    config = fixture_recipe_config(name, workspace.name)
    with tempfile.TemporaryDirectory(prefix="evolve-fixture-recipe-") as tempdir:
        recipe = Path(tempdir) / name
        recipe.mkdir()
        (recipe / "evolve.yaml").write_text(render_yaml(config))
        create_workspace(InitOptions(workspace=workspace, recipe_path=recipe))
    return workspace


def init_workspace_from_config(options: InitOptions, config: dict[str, Any]) -> Path:
    """Initialize through the maintained custom-recipe path using test config."""
    with tempfile.TemporaryDirectory(prefix="evolve-test-recipe-") as tempdir:
        recipe = Path(tempdir) / "custom"
        recipe.mkdir()
        (recipe / "evolve.yaml").write_text(render_yaml(copy.deepcopy(config)))
        create_workspace(replace(options, recipe=None, recipe_path=recipe))
    return options.workspace


def write_identity_dataset(root: Path, count: int = 10) -> Path:
    root.mkdir()
    for index in range(count):
        task = root / f"task-{index}"
        task.mkdir()
        (task / "task.toml").write_text(f'version = "1.0"\nname = "task-{index}"\n')
    return root


def init_recipe_with_local_inputs(tmp_path: Path, recipe: str) -> Path:
    dataset = write_identity_dataset(tmp_path / f"{recipe}-tasks", count=100)
    seed = write_locked_miniswe_seed(tmp_path / f"{recipe}-seed") if recipe in UV_SOURCE_RECIPES else None
    workspace = tmp_path / f"workspace-{recipe}"
    create_workspace(
        InitOptions(
            workspace=workspace,
            recipe=recipe,
            seed=str(seed) if seed is not None else None,
            dataset=str(dataset),
        )
    )
    return workspace


@pytest.fixture(autouse=True)
def evaluator_runtime_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVOLVE_RUNTIME_DIGEST", "sha256:test-runtime")
    monkeypatch.setenv("EVOLVE_HOME", str(tmp_path / "evolve-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.delenv("CODEX_AUTH_JSON_PATH", raising=False)
    monkeypatch.delenv("CODEX_FORCE_AUTH_JSON", raising=False)
    monkeypatch.setattr(
        "evolve.splits.RegistryClientFactory.create",
        lambda: _FixtureRegistryClient(),
    )


@pytest.fixture
def strict_workspace(tmp_path: Path) -> Path:
    return init_recipe_with_local_inputs(tmp_path, "aevolve")


@pytest.fixture
def legacy_workspace(tmp_path: Path) -> Path:
    workspace = init_fixture_workspace(tmp_path / "legacy-workspace")
    (workspace / "evaluator/runtime.json").unlink()
    (workspace / "evaluator/runtime.pin").write_text("legacy-unverified\n")
    git(workspace, "add", "evaluator/runtime.pin")
    git(workspace, "rm", "evaluator/runtime.json")
    git(workspace, "commit", "-m", "represent pre-contract workspace")
    git(workspace, "tag", "-f", "gen/0")
    return workspace


def run_evolve(
    *args: str,
    env: dict[str, str | None] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    # GitHub Actions makes rich force terminal rendering, which splits option
    # names such as ``--tasks`` across style spans and breaks plain-text
    # assertions on CLI output; pin help/output rendering to plain text.
    for name in ("GITHUB_ACTIONS", "FORCE_COLOR"):
        merged_env.pop(name, None)
    merged_env.setdefault("TERM", "dumb")
    if env:
        for key, value in env.items():
            if value is None:
                merged_env.pop(key, None)
            else:
                merged_env[key] = value
    if env and env.get("EVAL_STUB") == "1" and "EVOLVE_AGENT_COMMAND" not in env:
        merged_env["EVOLVE_AGENT_COMMAND"] = smoke_agent_command()
    return subprocess.run(
        [sys.executable, "-m", "evolve", *args],
        text=True,
        capture_output=True,
        env=merged_env,
        cwd=cwd,
        check=False,
    )


def smoke_agent_command() -> str:
    script = ROOT / "tests" / "fixtures" / "smoke_agent.py"
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def smoke_env(evolve_home: Path) -> dict[str, str]:
    return {
        "EVAL_STUB": "1",
        "EVOLVE_HOME": str(evolve_home),
        "EVOLVE_AGENT_COMMAND": smoke_agent_command(),
    }


def write_locked_miniswe_seed(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        "[project]\nname = 'mini-swe-agent'\nversion = '0.0.0'\nrequires-python = '>=3.11'\ndependencies = []\n"
    )
    package = path / "src" / "minisweagent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.0.0'\n")
    result = subprocess.run(
        ["uv", "lock", "--offline", "--python", sys.executable, "--project", str(path)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", "/tmp/rsihub-test-uv")},
    )
    assert result.returncode == 0, result.stderr
    return path


def git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def allow_local_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from evolve.preflight import checks as preflight_checks

    monkeypatch.setattr(preflight_checks, "tool_available", lambda name, env: True)


def contract_for_gen0(workspace: Path) -> evaluation_package.EvaluationContractV1:
    commit = git(workspace, "rev-parse", "gen/0^{commit}")
    return evaluation_package.resolve_evaluation_contract(
        evaluation_package.ContractResolutionContext(
            workspace=workspace,
            candidate_commit=commit,
            purpose="candidate",
            generation="0",
        )
    )


def init_workspace(tmp_path: Path, experiment: str = "experiment") -> tuple[Path, Path]:
    workspace = tmp_path / experiment
    evolve_home = tmp_path / "evolve-home"
    init_fixture_workspace(workspace)
    return workspace, evolve_home


def init_miniswe_workspace(tmp_path: Path, experiment: str = "miniswe-experiment") -> tuple[Path, Path]:
    workspace = tmp_path / experiment
    evolve_home = tmp_path / "evolve-home"
    seed = write_locked_miniswe_seed(tmp_path / "miniswe-seed")
    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "hill_climb",
        "--seed",
        str(seed),
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(evolve_home)},
    )
    assert result.returncode == 0, result.stderr
    return workspace, evolve_home


def rows_by_genid(workspace: Path) -> dict[str, dict[str, object]]:
    return {str(row["genid"]): row for row in mechanism_merged_rows(workspace / "archive.jsonl")}


def git_show(workspace: Path, spec: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(workspace), "show", spec],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def append_archive_event(workspace: Path, evolve_home: Path, event: dict[str, object]) -> None:
    line = json.dumps(event, sort_keys=True) + "\n"
    with (workspace / "archive.jsonl").open("a") as archive:
        archive.write(line)
    mirror = mirror_path(workspace.name, workspace)
    with mirror.open("a") as archive:
        archive.write(line)
