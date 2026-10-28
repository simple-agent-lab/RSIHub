import os
import subprocess
import sys
from pathlib import Path

import pytest

from evolve.candidate.snapshot import (
    CandidateSnapshotError,
    build_candidate_snapshot,
    commit_candidate_snapshot,
    materialize_snapshot,
)
from evolve.git import changed_paths, dirty_paths, working_tree_changed_paths


def git_stdout(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def git_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git_stdout(checkout, "init", "-q")
    git_stdout(checkout, "config", "user.name", "test")
    git_stdout(checkout, "config", "user.email", "test@example.invalid")
    (checkout / "target").mkdir()
    (checkout / "target" / "agent.py").write_text("VALUE = 1\n")
    git_stdout(checkout, "add", ".")
    git_stdout(checkout, "commit", "-qm", "parent")
    return checkout


def test_snapshot_excludes_ignored_untracked_file(tmp_path: Path) -> None:
    checkout = git_checkout(tmp_path)
    (checkout / ".gitignore").write_text("ignored.lock\n")
    (checkout / "visible.txt").write_text("visible\n")
    (checkout / "ignored.lock").write_text("ignored\n")
    snapshot = build_candidate_snapshot(checkout, "HEAD", include=["**"], exclude=[])
    with materialize_snapshot(checkout, snapshot) as materialized:
        assert (materialized / "visible.txt").is_file()
        assert not (materialized / "ignored.lock").exists()


def test_commit_tree_equals_snapshot_tree(tmp_path: Path) -> None:
    checkout = git_checkout(tmp_path)
    (checkout / "target" / "agent.py").write_text("VALUE = 2\n")
    snapshot = build_candidate_snapshot(checkout, "HEAD", include=["target/**"], exclude=[])
    commit = commit_candidate_snapshot(checkout, snapshot, "candidate")
    assert git_stdout(checkout, "rev-parse", f"{commit}^{{tree}}") == snapshot.tree


def test_snapshot_rejects_already_staged_path(tmp_path: Path) -> None:
    checkout = git_checkout(tmp_path)
    (checkout / "target" / "agent.py").write_text("VALUE = 2\n")
    git_stdout(checkout, "add", "target/agent.py")

    with pytest.raises(CandidateSnapshotError, match="^candidate index is not clean$"):
        build_candidate_snapshot(checkout, "HEAD", include=["target/**"], exclude=[])


@pytest.mark.parametrize(
    "name",
    [
        "中文.py",
        "a -> b.py",
        'quote"name.py',
        "tab\tname.py",
        "line\nname.py",
        "carriage\rreturn.py",
        " spaced.py ",
        pytest.param(
            os.fsdecode(b"non-utf8-\xff.py"),
            marks=pytest.mark.skipif(sys.platform != "linux", reason="non-UTF-8 filesystem names require Linux"),
        ),
    ],
)
@pytest.mark.parametrize("tracked", [False, True])
def test_snapshot_preserves_unusual_filenames(tmp_path: Path, name: str, tracked: bool) -> None:
    """Preserve each filename through discovery, materialization, and commit."""
    checkout = git_checkout(tmp_path)
    relative = f"target/{name}"
    path = checkout / relative
    if tracked:
        path.write_text("original\n")
        git_stdout(checkout, "add", "--", relative)
        git_stdout(checkout, "commit", "-qm", "track unusual filename")
    path.write_text("candidate\n")

    assert dirty_paths(checkout) == [relative]
    snapshot = build_candidate_snapshot(checkout, "HEAD", include=["target/**"], exclude=[])

    assert snapshot.changed_paths == (relative,)
    with materialize_snapshot(checkout, snapshot) as materialized:
        assert (materialized / relative).read_text() == "candidate\n"
    commit_candidate_snapshot(checkout, snapshot, "candidate")
    assert changed_paths(checkout, "HEAD^", "HEAD") == [relative]


def test_dirty_paths_skips_rename_source_record(tmp_path: Path) -> None:
    """Report rename destinations without consuming the next independent entry."""
    checkout = git_checkout(tmp_path)
    destination = "target/中文 -> renamed.py"
    git_stdout(checkout, "mv", "target/agent.py", destination)
    (checkout / "target/extra.py").write_text("untracked\n")

    assert set(dirty_paths(checkout)) == {destination, "target/extra.py"}


@pytest.mark.parametrize("helper", ["dirty", "working_tree", "committed"])
@pytest.mark.parametrize("path_bytes", [b"target/carriage\rreturn.py", b"target/non-utf8-\xff.py"])
def test_git_path_output_preserves_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str, path_bytes: bytes
) -> None:
    """Exercise actual subprocess decoding even on filesystems rejecting byte names."""
    script = tmp_path / "git"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.buffer.write((b'?? ' if 'status' in sys.argv else b'') + {path_bytes!r} + bytes([0]))\n"
    )
    script.chmod(0o700)
    monkeypatch.setattr("evolve.git.shutil.which", lambda _name: str(script))

    calls = {
        "dirty": lambda: dirty_paths(tmp_path),
        "working_tree": lambda: working_tree_changed_paths(tmp_path, "HEAD"),
        "committed": lambda: changed_paths(tmp_path, "HEAD^", "HEAD"),
    }
    assert calls[helper]() == [os.fsdecode(path_bytes)]
