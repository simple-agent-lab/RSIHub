import hashlib
from pathlib import Path

from evolve.composition import ResolvedOperator, resolve_builtin_recipe
from evolve.composition.materialize import materialize_operators

ROOT = Path(__file__).resolve().parents[1]


def test_real_recipe_binds_harbor_rollout_analyze_and_hyperagents_mutate() -> None:
    bindings = resolve_builtin_recipe("hill_climb").operators
    rollout = bindings["rollout"]
    analyze = bindings["analyze"]
    mutate = bindings["mutate"]

    assert rollout.name == "harbor"
    assert analyze.name == "failure_patterns"
    expected_source = (ROOT / "library" / "mutate" / "hyperagents.py").read_text()
    assert mutate.name == "hyperagents"
    assert mutate.source.read_text() == expected_source

    materialized = materialize_operators(bindings)
    harbor_runtime = str(materialized.files["library/_shared/harbor/rollout.py"])
    assert "from evolve.integrations.harbor._runtime_plan import " in harbor_runtime
    assert "library/_shared/harbor/__init__.py" in materialized.files
    assert "library/_shared/harbor/config.py" in materialized.files
    assert "library/_shared/harbor/evidence.py" in materialized.files
    assert "library/_shared/harbor/execution.py" in materialized.files
    assert "library/_shared/harbor.py" not in materialized.files
    assert "library/mutate/_config.py" in materialized.files
    assert "library/_shared/runners/local.py" in materialized.files
    assert "library/_shared/runners/harbor.py" in materialized.files
    assert "library/mutate/_support/workspace.py" in materialized.files
    assert "library/mutate/aevolve.py" not in materialized.files
    assert "library/mutate/ahe.py" not in materialized.files
    assert "library/mutate/gepa.py" not in materialized.files
    assert "library/mutate/hyperagents.py" not in materialized.files


def test_mutate_runners_are_not_operator_variants(tmp_path: Path) -> None:
    from evolve.composition.catalog import discover_operators

    variants = sorted(name for stage, name in discover_operators() if stage == "mutate")
    assert variants == ["aevolve", "ahe", "gepa", "hyperagents"]
    assert "local" not in variants
    assert "harbor" not in variants


def test_materialization_preserves_binary_stage_helper_assets(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "__init__.py").write_text('"""Closed library."""\n')
    (library / "mutate" / "prompts").mkdir(parents=True)
    source = library / "mutate" / "hyperagents.py"
    source.write_text('"""Selected mutate."""\n')
    (library / "mutate" / "prompts" / "strategy.md").write_text("Strategy prompt\n")
    (library / "mutate" / "_support").mkdir()
    (library / "mutate" / "_support" / "backend.py").write_text("SUPPORT = True\n")
    (library / "mutate" / "_support" / "strategy.bin").write_bytes(b"\x86\x00")
    (library / "mutate" / "__pycache__").mkdir()
    (library / "mutate" / "__pycache__" / "strategy.cpython-314.pyc").write_bytes(b"\x86\x00")
    binding = ResolvedOperator(
        stage="mutate",
        source_kind="library",
        source=source,
        name="hyperagents",
        timeout_s=10.0,
        config={},
        portable=True,
        digest=hashlib.sha256(source.read_bytes()).hexdigest(),
    )

    materialized = materialize_operators({"mutate": binding}, library=library)

    assert materialized.files["library/mutate/_support/backend.py"] == "SUPPORT = True\n"
    assert materialized.files["library/mutate/_support/strategy.bin"] == b"\x86\x00"
    assert "library/mutate/prompts/strategy.md" not in materialized.files
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in materialized.files)


def test_materialization_reads_only_discovery_permitted_root_helpers(tmp_path: Path, monkeypatch) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "__init__.py").write_text('"""Closed library."""\n')
    helper = library / "_shared_support.py"
    helper.write_text("ROOT_HELPER = True\n")
    source = library / "select" / "greedy.py"
    source.parent.mkdir()
    source.write_text('"""Selected parent."""\n')
    nested = library / "internal" / "credential_loader.py"
    nested.parent.mkdir()
    nested.write_text("MUST_NOT_BE_READ = True\n")
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path, *args, **kwargs):
        if path == nested:
            raise AssertionError("nested root helper candidate was read")
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    binding = ResolvedOperator(
        stage="select",
        source_kind="library",
        source=source,
        name="greedy",
        timeout_s=10.0,
        config={},
        portable=True,
        digest=hashlib.sha256(original_read_bytes(source)).hexdigest(),
    )

    materialized = materialize_operators({"select": binding}, library=library)

    assert materialized.files["library/_shared_support.py"] == "ROOT_HELPER = True\n"
    assert "library/internal/credential_loader.py" not in materialized.files


def test_recipe_evaluator_assets_copy_training_but_not_sealed_files(tmp_path: Path, monkeypatch) -> None:
    from evolve import workspace as workspace_module

    recipes = tmp_path / "recipes"
    (recipes / "custom" / "evaluator" / "tasks").mkdir(parents=True)
    (recipes / "custom" / "evaluator" / "tasks" / "train.txt").write_text("task-a\n")
    (recipes / "custom" / "sealed").mkdir()
    (recipes / "custom" / "sealed" / "heldout.txt").write_text("secret-task\n")
    assert workspace_module._recipe_evaluator_assets(recipes / "custom") == {"evaluator/tasks/train.txt": "task-a\n"}
