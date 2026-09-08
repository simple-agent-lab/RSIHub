import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import (
    git,
    init_fixture_workspace,
    init_recipe_with_local_inputs,
    run_evolve,
    write_identity_dataset,
    write_locked_miniswe_seed,
)
from typer.testing import CliRunner

from evolve.config import load_config, surface_lists
from evolve.workspace import InitOptions, _write_target, init_workspace

MINISWE_REVISION = "388da74aad620a384ab47669b17c52133e30e7c3"


@pytest.mark.parametrize(
    ("recipe", "managed_candidate"),
    [
        ("aevolve", False),
        ("ahe", True),
        ("gepa", False),
        ("hill_climb", True),
        ("hyperagents", True),
        ("hyperagents_codex_tbench_full", False),
        ("hyperagents_tbench_full", True),
    ],
)
def test_init_generates_canonical_resolved_runtime(tmp_path: Path, recipe: str, managed_candidate: bool) -> None:
    workspace = init_recipe_with_local_inputs(tmp_path, recipe)

    payload = json.loads((workspace / "evaluator/runtime.json").read_text())
    assert payload["engine"] == "harbor"
    assert ("candidate" in payload) is managed_candidate
    assert payload["digest"] == (workspace / "evaluator/runtime.pin").read_text().strip()
    assert "model.example" not in json.dumps(payload)
    assert git(workspace, "show", "gen/0:evaluator/runtime.json")


def test_init_certifies_default_runtime_when_recipe_omits_runtime(tmp_path: Path) -> None:
    workspace = init_fixture_workspace(tmp_path / "workspace")

    payload = json.loads((workspace / "evaluator/runtime.json").read_text())
    assert payload["engine"] == "harbor"
    assert "candidate" not in payload
    assert "proxy" not in payload
    assert (workspace / "evaluator/runtime.pin").read_text().strip() == payload["digest"]


def test_generated_preflight_wrapper_only_delegates_to_framework(tmp_path: Path) -> None:
    workspace = init_recipe_with_local_inputs(tmp_path, "aevolve")

    assert (workspace / "operators/preflight.sh").read_text() == (
        "#!/bin/sh\n"
        "set -eu\n"
        'HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)\n'
        'exec "$ROOT/evolve" preflight "$ROOT" "$@"\n'
    )


def _miniswe_seed(root: Path) -> Path:
    return write_locked_miniswe_seed(root / "miniswe")


def _identity_dataset(root: Path) -> Path:
    return write_identity_dataset(root / "tasks")


def _versioned_candidate_seed(path: Path, *, locked: bool) -> Path:
    seed = write_locked_miniswe_seed(path)
    if not locked:
        (seed / "uv.lock").unlink()
    git(seed, "init")
    git(seed, "config", "user.name", "Seed Test")
    git(seed, "config", "user.email", "seed@example.invalid")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "candidate seed")
    return seed


def _override_hill_target(monkeypatch, target: dict[str, object]) -> None:
    from evolve import workspace as workspace_module
    from evolve.composition import ResolvedRecipe

    resolve_builtin_recipe = workspace_module.resolve_builtin_recipe

    def configured(recipe: str) -> ResolvedRecipe:
        resolved = resolve_builtin_recipe(recipe)
        config = copy.deepcopy(resolved.config)
        config["target"] = target.copy()
        return ResolvedRecipe(
            resolved.name,
            resolved.directory,
            config,
            resolved.operators,
            resolved.warnings,
        )

    monkeypatch.setattr(workspace_module, "resolve_builtin_recipe", configured)


def test_init_rejects_test_only_builtin_dummy_seed_before_creating_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "hill_climb",
        "--seed",
        "builtin-dummy",
        env={"EVOLVE_HOME": str(tmp_path / "evolve-home")},
    )

    assert result.returncode == 1
    assert "builtin-dummy is test-only; pass a local seed directory instead" in result.stderr
    assert not workspace.exists()


def test_init_help_advertises_only_supported_seed_options() -> None:
    result = run_evolve("init", "--help")

    assert result.returncode == 0, result.stderr
    assert "--tasks" in result.stdout
    assert "builtin-codex" in result.stdout
    assert "local target dir" in result.stdout
    assert "git URL to vendor" in result.stdout
    assert "into target/" in result.stdout
    assert "builtin-dummy" not in result.stdout
    assert "[WORKSPACE]" in result.stdout
    assert "~/.evolve-workspace" in result.stdout


def test_init_defaults_to_home_workspace(monkeypatch, tmp_path: Path) -> None:
    from evolve import cli as cli_module

    captured: list[InitOptions] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli_module, "init_workspace", captured.append)

    result = CliRunner().invoke(cli_module.app, ["init"])

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].workspace == tmp_path / ".evolve-workspace"
    assert f"Initialized RSIHub workspace at {tmp_path / '.evolve-workspace'}" in result.output


def test_init_explicit_workspace_still_wins(monkeypatch, tmp_path: Path) -> None:
    from evolve import cli as cli_module

    captured: list[InitOptions] = []
    explicit = tmp_path / "named-experiment"
    monkeypatch.setattr(cli_module, "init_workspace", captured.append)

    result = CliRunner().invoke(cli_module.app, ["init", str(explicit)])

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].workspace == explicit


def test_git_seed_revision_freezes_exact_commit(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True, text=True)
    git(seed, "config", "user.name", "Seed Test")
    git(seed, "config", "user.email", "seed@example.invalid")
    (seed / "uv.lock").write_text("version = 1\nrevision = 3\nrequires-python = '>=3.11'\n")
    git(seed, "add", "uv.lock")
    git(seed, "commit", "-m", "locked seed")
    locked_commit = git(seed, "rev-parse", "HEAD")
    (seed / "uv.lock").unlink()
    (seed / "pyproject.toml").write_text("[project]\nname='unlocked'\nversion='0'\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "remove lock")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_target(
        workspace,
        {
            "seed": seed.as_uri(),
            "revision": locked_commit,
        },
    )

    assert (workspace / "target" / "uv.lock").is_file()
    upstream = json.loads((workspace / "target" / "UPSTREAM.json").read_text())
    assert upstream == {"commit": locked_commit, "remote": seed.as_uri()}


def test_local_seed_preserves_internal_symlink_without_dereferencing(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "source.txt").write_text("source\n")
    (seed / "alias.txt").symlink_to("source.txt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _write_target(workspace, {"seed": str(seed)})

    alias = workspace / "target" / "alias.txt"
    assert alias.is_symlink()
    assert alias.readlink() == Path("source.txt")


def test_local_seed_rejects_symlink_escaping_source_tree(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (seed / "escape.txt").symlink_to(outside)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="symlink"):
        _write_target(workspace, {"seed": str(seed)})

    assert not (workspace / "target").exists()


def test_upstream_metadata_strips_remote_credentials_and_query(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init")
    git(seed, "config", "user.name", "Seed Test")
    git(seed, "config", "user.email", "seed@example.invalid")
    (seed / "README.md").write_text("seed\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "seed")
    git(seed, "remote", "add", "origin", "https://user:secret@example.com/org/repo.git?token=hidden#frag")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    _write_target(workspace, {"seed": str(seed)})

    upstream = json.loads((workspace / "target" / "UPSTREAM.json").read_text())
    assert upstream["remote"] == "https://example.com/org/repo.git"
    assert "secret" not in json.dumps(upstream)
    assert "hidden" not in json.dumps(upstream)


def test_git_seed_can_explicitly_generate_missing_lock(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True, text=True)
    git(seed, "config", "user.name", "Seed Test")
    git(seed, "config", "user.email", "seed@example.invalid")
    (seed / "pyproject.toml").write_text(
        "[project]\nname='unlocked-seed'\nversion='0'\nrequires-python='>=3.11'\ndependencies=[]\n"
    )
    git(seed, "add", "pyproject.toml")
    git(seed, "commit", "-m", "unlocked seed")
    seed_commit = git(seed, "rev-parse", "HEAD")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_target(
        workspace,
        {
            "seed": seed.as_uri(),
            "revision": seed_commit,
            "generate_lock": True,
        },
    )

    assert (workspace / "target" / "uv.lock").is_file()


@pytest.mark.parametrize("source_kind", ["local", "git"])
def test_init_accepts_explicit_locked_candidate_target(tmp_path: Path, source_kind: str) -> None:
    seed = _versioned_candidate_seed(tmp_path / "seed", locked=True)
    seed_reference = seed.as_uri() if source_kind == "git" else str(seed)
    workspace = tmp_path / "workspace"

    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe="hill_climb",
            seed=seed_reference,
            dataset=str(_identity_dataset(tmp_path)),
        )
    )

    assert (workspace / "target" / "uv.lock").is_file()
    assert load_config(workspace / "evolve.yaml")["target"] == {"seed": seed_reference}
    assert git(workspace, "ls-files", "target/uv.lock") == "target/uv.lock"
    git(workspace, "cat-file", "-e", "gen/0:target/uv.lock")


@pytest.mark.parametrize("source_kind", ["local", "git"])
def test_init_rejects_explicit_unlocked_candidate_target_without_destination_residue(
    tmp_path: Path, source_kind: str
) -> None:
    seed = _versioned_candidate_seed(tmp_path / "seed", locked=False)
    seed_reference = seed.as_uri() if source_kind == "git" else str(seed)
    workspace = tmp_path / "workspace"

    with pytest.raises(
        ValueError,
        match=r"MiniSWE candidate.*prepared target.*uv\.lock",
    ):
        init_workspace(InitOptions(workspace=workspace, recipe="hill_climb", seed=seed_reference))

    assert not workspace.exists()


@pytest.mark.parametrize("missing", ["pyproject.toml", "src/minisweagent"])
def test_init_rejects_incomplete_miniswe_candidate_project_before_creating_workspace(
    tmp_path: Path, missing: str
) -> None:
    seed = write_locked_miniswe_seed(tmp_path / "seed")
    path = seed / missing
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match=r"MiniSWE candidate.*target/"):
        init_workspace(InitOptions(workspace=workspace, recipe="hill_climb", seed=str(seed)))

    assert not workspace.exists()


def test_init_rejects_invalid_miniswe_candidate_lock_before_creating_workspace(
    tmp_path: Path,
) -> None:
    seed = write_locked_miniswe_seed(tmp_path / "seed")
    (seed / "uv.lock").write_text("not valid TOML = [\n")
    workspace = tmp_path / "workspace"

    with pytest.raises(ValueError, match=r"MiniSWE candidate.*uv lock --check"):
        init_workspace(InitOptions(workspace=workspace, recipe="hill_climb", seed=str(seed)))

    assert not workspace.exists()


def test_init_accepts_generated_lock_for_local_candidate_project(tmp_path: Path, monkeypatch) -> None:
    seed = write_locked_miniswe_seed(tmp_path / "seed")
    (seed / "uv.lock").unlink()
    _override_hill_target(
        monkeypatch,
        {"seed": str(seed), "generate_lock": True},
    )
    workspace = tmp_path / "workspace"

    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe="hill_climb",
            dataset=str(_identity_dataset(tmp_path)),
        )
    )

    assert (workspace / "target" / "uv.lock").is_file()
    git(workspace, "cat-file", "-e", "gen/0:target/uv.lock")


def test_init_removes_egg_info_created_during_lock_generation(tmp_path: Path, monkeypatch) -> None:
    from evolve import workspace as workspace_module

    seed = write_locked_miniswe_seed(tmp_path / "seed")
    lock = (seed / "uv.lock").read_text()
    (seed / "uv.lock").unlink()
    _override_hill_target(
        monkeypatch,
        {"seed": str(seed), "generate_lock": True},
    )

    def generate_lock_with_metadata(target: Path) -> None:
        (target / "uv.lock").write_text(lock)
        egg_info = target / "src" / "mini_swe_agent.egg-info"
        egg_info.mkdir()
        (egg_info / "SOURCES.txt").write_text("src/minisweagent/__init__.py\n")

    monkeypatch.setattr(workspace_module, "_generate_target_lock", generate_lock_with_metadata)
    workspace = tmp_path / "workspace"

    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe="hill_climb",
            dataset=str(_identity_dataset(tmp_path)),
        )
    )

    assert not (workspace / "target" / "src" / "mini_swe_agent.egg-info").exists()
    assert git(workspace, "ls-files", "target/src/mini_swe_agent.egg-info") == ""


def test_init_rejects_generate_lock_for_non_project_before_creating_workspace(tmp_path: Path, monkeypatch) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("not a Python project\n")
    _override_hill_target(
        monkeypatch,
        {"seed": str(seed), "generate_lock": True},
    )
    workspace = tmp_path / "workspace"

    with pytest.raises(
        ValueError,
        match=r"target\.generate_lock.*target/pyproject\.toml",
    ):
        init_workspace(InitOptions(workspace=workspace, recipe="hill_climb"))

    assert not workspace.exists()


def test_init_rejects_builtin_candidate_even_when_generate_lock_is_requested(tmp_path: Path, monkeypatch) -> None:
    _override_hill_target(
        monkeypatch,
        {"seed": "builtin-codex", "generate_lock": True},
    )
    workspace = tmp_path / "workspace"

    with pytest.raises(
        ValueError,
        match=r"target\.generate_lock.*target/pyproject\.toml",
    ):
        init_workspace(InitOptions(workspace=workspace, recipe="hill_climb"))

    assert not workspace.exists()


def test_init_rejects_explicit_builtin_candidate_without_destination_residue(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"

    with pytest.raises(
        ValueError,
        match=r"MiniSWE candidate.*prepared target.*uv\.lock",
    ):
        init_workspace(
            InitOptions(
                workspace=workspace,
                recipe="hill_climb",
                seed="builtin-codex",
            )
        )

    assert not workspace.exists()


def test_write_target_requires_generated_lock_postcondition(tmp_path: Path, monkeypatch) -> None:
    from evolve import workspace as workspace_module

    seed = write_locked_miniswe_seed(tmp_path / "seed")
    (seed / "uv.lock").unlink()
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n")
    fake_uv.chmod(0o755)
    monkeypatch.setattr(workspace_module, "uv_executable", lambda: str(fake_uv))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(
        ValueError,
        match=r"target\.generate_lock.*target/uv\.lock",
    ):
        _write_target(
            workspace,
            {"seed": str(seed), "generate_lock": True},
        )


def test_default_hill_climb_pins_seed_and_generates_candidate_lock(tmp_path: Path, monkeypatch) -> None:
    from evolve import workspace as workspace_module

    clone_source = _versioned_candidate_seed(tmp_path / "miniswe", locked=False)
    clone_revision = git(clone_source, "rev-parse", "HEAD")
    git_clone = workspace_module._git_clone

    def clone_reviewed_miniswe(url: str, destination: Path, *, revision: str | None = None) -> None:
        assert url == "https://github.com/SWE-agent/mini-swe-agent.git"
        assert revision == MINISWE_REVISION
        git_clone(clone_source.as_uri(), destination, revision=clone_revision)

    monkeypatch.setattr(workspace_module, "_git_clone", clone_reviewed_miniswe)
    workspace = tmp_path / "workspace"
    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe="hill_climb",
            dataset=str(_identity_dataset(tmp_path)),
        )
    )

    assert (workspace / "target" / "uv.lock").is_file()
    assert load_config(workspace / "evolve.yaml")["target"] == {
        "seed": "https://github.com/SWE-agent/mini-swe-agent.git",
        "revision": MINISWE_REVISION,
        "generate_lock": True,
    }
    assert git(workspace, "ls-files", "target/uv.lock") == "target/uv.lock"
    git(workspace, "cat-file", "-e", "gen/0:target/uv.lock")


def test_init_scaffolds_hill_climb_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "experiment"

    init_fixture_workspace(workspace)
    expected_paths = [
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".evolve-components.json",
        ".evolve/evolve/integrations/harbor/codex_candidate.py",
        ".evolve/evolve/integrations/harbor/miniswe_candidate.py",
        ".evolve/evolve/integrations/harbor/miniswe_task_file.py",
        "evolve.yaml",
        ".evolve-protocol-version",
        ".evolve/evolve/harbor_local.py",
        "AGENTS.md",
        "program.md",
        "LICENSE.rsihub",
        "NOTICE.rsihub",
        "operators/select.py",
        "operators/rollout.py",
        "operators/mutate.py",
        "operators/gate.py",
        "operators/record.py",
        "operators/engines/local.sh",
        "operators/preflight.sh",
        "operators/select.md",
        "operators/rollout.md",
        "operators/gate.md",
        "operators/record.md",
        "skills/evolve-agent/SKILL.md",
        "skills/evolve-agent/references/workspace-contract.md",
        "skills/evolve-agent/references/hill-climb.md",
        "target/agent.py",
        "target/README.md",
        "target/UPSTREAM.json",
        "evaluator/eval.sh",
        "evaluator/eval.env",
        "evaluator/environment.kwargs",
        "evaluator/splits.json",
        "evaluator/dataset.pin",
        "evaluator/parse_score.py",
        "evaluator/engines/local.sh",
        "runs",
        "artifacts/user",
        "artifacts/generations",
        ".gitignore",
        "archive.jsonl",
        "best_ever.json",
    ]
    for relative_path in expected_paths:
        assert (workspace / relative_path).exists(), relative_path
    for method_card in ("a-evolve.md", "gepa.md", "ahe.md", "hyperagents.md"):
        assert (workspace / "skills/evolve-agent/references" / method_card).is_file()
    for helper in (
        "library/__init__.py",
        "library/select/_config.py",
        "library/mutate/_config.py",
        "library/_shared/runners/local.py",
        "library/mutate/_support/workspace.py",
    ):
        assert (workspace / helper).is_file(), helper
    for unselected in (
        "library/analyze/ahe.py",
        "library/analyze/gepa.py",
        "library/mutate/aevolve.py",
        "library/mutate/hyperagents.py",
        "library/rollout/harbor.py",
        "library/validate/minibatch_improvement.py",
    ):
        assert not (workspace / unselected).exists(), unselected
    assert "artifacts/" in (workspace / ".gitignore").read_text().splitlines()
    assert not (workspace / "operators" / "meta_agent.py").exists()
    assert not (workspace / "operators" / "meta_agent.md").exists()
    assert not (workspace / "operators" / "meta_agent_brief.md").exists()
    assert not (workspace / "evolve_harbor_adapter").exists()
    assert not (workspace / "evolve_harbor_agent").exists()
    assert not (workspace / "operators" / "meta_agent.md").exists()
    assert not (workspace / "operators" / "meta_agent_brief.md").exists()
    assert not (workspace / "evaluator" / "checkout_agent.py").exists()
    assert (workspace / ".python-version").read_text() == "3.12\n"
    assert "harbor==0.18.0" in (workspace / "pyproject.toml").read_text()
    assert 'packages = [".evolve/evolve", "library"]' in (workspace / "pyproject.toml").read_text()
    assert "Apache License" in (workspace / "LICENSE.rsihub").read_text()
    assert "GEPA" in (workspace / "NOTICE.rsihub").read_text()

    config = (workspace / "evolve.yaml").read_text()
    assert "children_per_gen: 1" in config
    assert "mode: driver" in config
    assert "tests/fixtures/seeds/dummy" in config
    assert "variant:" not in config
    assert "script:" not in config
    assert "mutate:\n    timeout_s: 3600.0\n    config:" in config
    assert "      runner: local" in config
    assert "meta_agent:" not in config
    assert "- target/**" in config
    assert (workspace / ".evolve-protocol-version").read_text() == "1\n"
    upstream = json.loads((workspace / "target" / "UPSTREAM.json").read_text())
    assert upstream == {"kind": "fixture", "seed": "dummy"}
    components = json.loads((workspace / ".evolve-components.json").read_text())
    assert components["recipe"] == "hill_climb-smoke"
    assert str(components["target_seed"]).endswith("tests/fixtures/seeds/dummy")

    gitignore = (workspace / ".gitignore").read_text()
    assert "runs/" in gitignore
    assert "archive.jsonl" in gitignore
    assert "best_ever.json" in gitignore
    assert json.loads((workspace / "best_ever.json").read_text()) is None
    assert ".venv/" in gitignore
    assert ".env" in gitignore.splitlines()
    assert ".env.*" in gitignore.splitlines()

    splits = json.loads((workspace / "evaluator" / "splits.json").read_text())
    assert splits["version"] == 2
    assert splits["resolved"] is False
    assert splits["ratios"] == {"train": 0.5, "gate": 0.4, "sealed": 0.1}
    assert sum(len(members) for members in splits["tasks"].values()) == 100


def test_init_creates_generation_zero_git_snapshot_and_archive_event(tmp_path: Path) -> None:
    workspace = tmp_path / "experiment"

    init_fixture_workspace(workspace)
    assert git(workspace, "tag", "--list", "gen/0") == "gen/0"
    assert git(workspace, "rev-parse", "gen/0^{commit}") == git(workspace, "rev-parse", "HEAD")
    assert "gen 0" in git(workspace, "log", "-1", "--pretty=%s").lower()

    status_lines = git(workspace, "status", "--short").splitlines()
    assert status_lines == [], status_lines

    archive_lines = (workspace / "archive.jsonl").read_text().splitlines()
    assert len(archive_lines) == 1
    row = json.loads(archive_lines[0])
    assert row["genid"] == "0"
    assert row["parent"] is None
    assert row["tag"] == "gen/0"
    assert row["status"] == "pending"
    assert row["score"] is None
    assert row["valid_parent"] is False
    assert row["verdict"] == "pending"
    assert row["reason"] == "generation zero requires real evaluation"
    assert row["mutated"] == []
    assert row["surface_violations"] == []
    assert "task_set_hash" not in row
    assert "task_vector" not in row
    assert "evaluator_tree" not in row
    assert row["cost"]["usd"] == 0


def test_init_binds_real_hyperagents_method_surface_and_operators(tmp_path: Path) -> None:
    workspace = tmp_path / "hyperagents"
    seed = _miniswe_seed(tmp_path)

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "hyperagents",
        "--seed",
        str(seed),
        "--dataset",
        str(_identity_dataset(tmp_path)),
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(tmp_path / "evolve-home")},
    )

    assert result.returncode == 0, result.stderr
    assert "score_child_prop" in (workspace / "operators/select.py").read_text()
    assert "ParentEvaluationRollout" in (workspace / "operators/rollout.py").read_text()
    assert not (workspace / "library/rollout/harbor.py").exists()
    assert "class TraceBrowser" in (workspace / "operators/analyze.py").read_text()
    assert "operator: hyperagents" in (workspace / "operators/mutate.py").read_text()
    assert "HyperAgents Self-Improvement" in (workspace / "operators/mutate.py").read_text()
    assert "HyperAgentsValidate" in (workspace / "operators/validate.py").read_text()
    assert "HyperAgentsRecord" in (workspace / "operators/record.py").read_text()
    assert surface_lists(workspace) == (["target/**", "operators/**"], [])


def test_init_tracks_vendored_files_ignored_by_seed_repository(tmp_path: Path) -> None:
    workspace = tmp_path / "ignored-lock"
    seed = _miniswe_seed(tmp_path)
    (seed / ".gitignore").write_text("uv.lock\n")

    result = run_evolve(
        "init",
        str(workspace),
        "--recipe",
        "hyperagents",
        "--seed",
        str(seed),
        "--dataset",
        str(_identity_dataset(tmp_path)),
        env={"EVAL_STUB": "1", "EVOLVE_HOME": str(tmp_path / "evolve-home")},
    )

    assert result.returncode == 0, result.stderr
    assert "target/uv.lock" in git(workspace, "ls-files", "target/uv.lock").splitlines()
