from __future__ import annotations

from pathlib import Path

import pytest

from evolve.viewer.catalog import load_catalog


def workspace(root: Path, name: str) -> Path:
    target = root / name
    target.mkdir()
    (target / "evolve.yaml").write_text("experiment:\n  id: catalog-test\n")
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
