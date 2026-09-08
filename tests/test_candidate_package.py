import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import git

from evolve.candidate.package import export_candidate, materialize_candidate
from evolve.cli import app


def _repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "fixture")
    for name, value in {
        "target/skills/policy.md": "general policy\n",
        "target/run.sh": "#!/bin/sh\necho safe\n",
        "evaluator/private.txt": "private evaluation sentinel",
        "runs/agent-driven/notes/experience.md": "research-only memory",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    (root / "target/run.sh").chmod(0o755)
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    git(root, "tag", "gen/0")
    return root


def test_export_is_committed_target_only_and_survives_without_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "target/skills/policy.md").write_text("uncommitted replacement")
    (repo / "target/untracked-secret").write_text("untracked")
    receipt = export_candidate(repo, "gen/0", tmp_path / "package")
    assert set(receipt["files"]) == {"skills/policy.md", "run.sh"}
    second = export_candidate(repo, "gen/0", tmp_path / "second")
    assert second["sha256"] == receipt["sha256"]
    shutil.rmtree(repo)
    materialize_candidate(tmp_path / "package", receipt["sha256"], tmp_path / "installed")
    assert (tmp_path / "installed/skills/policy.md").read_text() == "general policy\n"
    assert os.access(tmp_path / "installed/run.sh", os.X_OK)
    assert sorted(
        p.relative_to(tmp_path / "installed").as_posix() for p in (tmp_path / "installed").rglob("*") if p.is_file()
    ) == ["run.sh", "skills/policy.md"]


@pytest.mark.parametrize("change", ["content", "manifest", "extra", "file_link", "directory_link", "fifo"])
def test_package_rejects_tampering_before_materialization(tmp_path: Path, change: str) -> None:
    repo = _repo(tmp_path / "repo")
    package = tmp_path / "package"
    receipt = export_candidate(repo, "gen/0", package)
    if change == "content":
        (package / "target/run.sh").write_text("changed")
    elif change == "manifest":
        (package / "manifest.json").write_text("{}")
    elif change == "extra":
        (package / "private.txt").write_text("must not travel")
    elif change in {"file_link", "fifo"}:
        path = package / "target/run.sh"
        path.unlink()
        if change == "fifo":
            os.mkfifo(path)
        else:
            path.symlink_to(repo / "target/run.sh")
    else:
        shutil.rmtree(package / "target/skills")
        (package / "target/skills").symlink_to(repo / "target/skills", target_is_directory=True)
    with pytest.raises((RuntimeError, OSError)):
        materialize_candidate(package, receipt["sha256"], tmp_path / "installed")
    assert not (tmp_path / "installed").exists()


def test_export_rejects_git_symlink_without_creating_delivery(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "target/private").symlink_to("../evaluator/private.txt")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "unsafe")
    with pytest.raises(RuntimeError, match="symlinks"):
        export_candidate(repo, "HEAD", tmp_path / "package")
    assert not (tmp_path / "package").exists()


@pytest.mark.parametrize("name", ["../private", "/private", "skills/../../private", ".git/config", "./file"])
def test_even_pinned_manifest_cannot_escape_destination(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path / "repo")
    package = tmp_path / "package"
    receipt = export_candidate(repo, "HEAD", package)
    manifest = json.loads((package / "manifest.json").read_bytes())
    manifest["files"] = {name: receipt["files"]["run.sh"]}
    encoded = json.dumps(manifest).encode()
    (package / "manifest.json").write_bytes(encoded)
    with pytest.raises(RuntimeError, match="member path"):
        materialize_candidate(package, hashlib.sha256(encoded).hexdigest(), tmp_path / "installed")
    assert not (tmp_path / "installed").exists()


def test_candidate_export_cli(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    repo = _repo(tmp_path / "repo")
    result = CliRunner().invoke(app, ["candidate-export", str(repo), "--output", str(tmp_path / "package")])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert len(receipt["sha256"]) == 64
    assert receipt["path"] == str(tmp_path / "package")
