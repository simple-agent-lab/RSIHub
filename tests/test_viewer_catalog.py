from __future__ import annotations

from pathlib import Path

import pytest

from evolve.viewer.catalog import load_catalog


def workspace(root: Path, name: str, *, dataset: str | None = None) -> Path:
    target = root / name
    target.mkdir()
    evaluator = f"evaluator:\n  dataset: {dataset}\n" if dataset else ""
    (target / "evolve.yaml").write_text(f"experiment:\n  id: catalog-test\n{evaluator}")
    (target / "archive.jsonl").write_text("")
    return target


def test_load_catalog_resolves_relative_workspaces(tmp_path: Path) -> None:
    experiment = workspace(tmp_path, "experiment-a")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
experiments:
  - slug: MiniSWE A-Evolve
    label: A-Evolve · MiniSWE
    method: A-Evolve
    agent: MiniSWE
    selection_metric: Gate
    workspace: experiment-a
""".lstrip()
    )

    entries = load_catalog(catalog)

    assert len(entries) == 1
    assert entries[0].slug == "miniswe-a-evolve"
    assert entries[0].workspace == experiment
    assert entries[0].public_record()["snapshot_url"].endswith("/api/evolve/snapshot")
    assert entries[0].benchmark is None


@pytest.mark.parametrize(
    ("dataset", "expected"),
    [
        ("/datasets/terminal-bench-2-50-19-20", "Terminal Bench 2"),
        ("sierra-research/tau3-bench__tau3-banking", "Tau3 Banking"),
        ("/datasets/custom-code-benchmark-50-20-27", "Custom Code Benchmark"),
    ],
)
def test_load_catalog_derives_benchmark_from_workspace_dataset(tmp_path: Path, dataset: str, expected: str) -> None:
    workspace(tmp_path, "experiment", dataset=dataset)
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("experiments:\n  - workspace: experiment\n")

    entry = load_catalog(catalog)[0]

    assert entry.benchmark == expected
    assert entry.public_record()["benchmark"] == expected


def test_catalog_benchmark_override_takes_precedence(tmp_path: Path) -> None:
    workspace(tmp_path, "experiment", dataset="/datasets/terminal-bench-2-50-19-20")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("experiments:\n  - workspace: experiment\n    benchmark: Terminal Bench 2.0 (custom split)\n")

    assert load_catalog(catalog)[0].benchmark == "Terminal Bench 2.0 (custom split)"


def test_load_catalog_rejects_duplicate_slugs(tmp_path: Path) -> None:
    workspace(tmp_path, "one")
    workspace(tmp_path, "two")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
experiments:
  - slug: duplicate
    workspace: one
  - slug: Duplicate
    workspace: two
""".lstrip()
    )

    with pytest.raises(ValueError, match="duplicate viewer catalog slug"):
        load_catalog(catalog)


def test_load_catalog_requires_frozen_workspace_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(f"experiments:\n  - workspace: {missing}\n")

    with pytest.raises(ValueError, match="evolve.yaml, archive.jsonl"):
        load_catalog(catalog)
