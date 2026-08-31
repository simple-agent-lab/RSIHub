from __future__ import annotations

import os
import tarfile
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

import pytest

pytestmark = pytest.mark.skipif(
    "EVOLVE_RELEASE_DIST" not in os.environ,
    reason="release artifact checks run after the CI build step",
)


def _release_wheel() -> Path:
    dist = Path(os.environ["EVOLVE_RELEASE_DIST"])
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _release_sdist() -> Path:
    dist = Path(os.environ["EVOLVE_RELEASE_DIST"])
    sdists = sorted(dist.glob("*.tar.gz"))
    assert len(sdists) == 1, sdists
    return sdists[0]


def test_release_wheel_has_one_resource_owner_and_complete_metadata() -> None:
    with ZipFile(_release_wheel()) as archive:
        names = set(archive.namelist())
        assert not [name for name in names if name.startswith("library/")]
        assert not [name for name in names if name.startswith("evolve/datasets/")]
        assert {
            "evolve/library/PROTOCOL.md",
            "evolve/library/__init__.py",
            "evolve/frozen/config.py",
            "evolve/library/analyze/failure_patterns.py",
            "evolve/library/mutate/hyperagents.py",
            "evolve/recipes/aevolve/evolve.yaml",
            "evolve/recipes/ahe/evolve.yaml",
            "evolve/recipes/ahe_codex/evolve.yaml",
            "evolve/recipes/gepa/evolve.yaml",
            "evolve/recipes/gepa_local/evolve.yaml",
            "evolve/recipes/hill_climb/evolve.yaml",
            "evolve/recipes/hill_climb_codex/evolve.yaml",
            "evolve/recipes/hyperagents/evolve.yaml",
            "evolve/recipes/hyperagents_codex/evolve.yaml",
            "evolve/recipes/hyperagents_dsh/evolve.yaml",
            "evolve/library/validate/node_check.py",
            "evolve/seeds/dsh/agent.py",
            "evolve/seeds/dsh/runners/mutate_local.py",
            "evolve/scaffolds/workspace/README.md",
            "evolve/seeds/codex/agent.py",
            "evolve/seeds/codex/plugins/evolve-target/.codex-plugin/plugin.json",
            "evolve/seeds/codex/plugins/evolve-target/hooks/hooks.json",
            "evolve/integrations/harbor/codex_candidate.py",
            "evolve/integrations/harbor/miniswe_candidate.py",
            "evolve/operator_cli.py",
            "evolve/composition/__init__.py",
            "evolve/composition/catalog.py",
            "evolve/composition/cli.py",
            "evolve/composition/recipe.py",
            "evolve/skills/evolve-agent/SKILL.md",
            "evolve/skills/evolve-agent/references/workspace-contract.md",
            "evolve/containers/mutate/Dockerfile",
            "evolve/containers/mutate/required-tools.txt",
            "evolve/licenses/LICENSE",
            "evolve/licenses/NOTICE",
        } <= names
        assert not [name for name in names if name.startswith("evolve/library/meta_agent/")]
        assert not [name for name in names if name.startswith("evolve/library/trace_analyzer/")]

        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_path).decode())

    assert metadata["Name"] == "rsihub"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert "RSIHub" in metadata.get_payload()
    project_urls = metadata.get_all("Project-URL") or []
    assert "Repository, https://github.com/simple-agent-lab/RSIHub" in project_urls
    assert "Issues, https://github.com/simple-agent-lab/RSIHub/issues" in project_urls


def test_release_sdist_contains_legal_and_build_files() -> None:
    with tarfile.open(_release_sdist()) as archive:
        names = set(archive.getnames())

    roots = {name.partition("/")[0] for name in names}
    assert len(roots) == 1, roots
    root = roots.pop()
    assert {
        f"{root}/LICENSE",
        f"{root}/NOTICE",
        f"{root}/README.md",
        f"{root}/pyproject.toml",
    } <= names
