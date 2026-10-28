from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def git(
    workspace: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    path_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run Git, optionally preserving verbatim paths with filesystem decoding."""
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required")
    process = subprocess.run(
        [executable, "-C", str(workspace), *args],
        text=not path_output,
        capture_output=True,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )
    result = subprocess.CompletedProcess(
        process.args, process.returncode, os.fsdecode(process.stdout), os.fsdecode(process.stderr)
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(message)
    return result


def git_stdout(workspace: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return git(workspace, *args, env=env).stdout.strip()


def git_common_dir(workspace: Path) -> str:
    common = Path(git_stdout(workspace, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = workspace / common
    return str(common.resolve())


def head_commit(workspace: Path) -> str:
    return git_stdout(workspace, "rev-parse", "HEAD")


def head_tag(workspace: Path) -> str | None:
    output = git(workspace, "describe", "--tags", "--exact-match", "HEAD", check=False)
    if output.returncode != 0:
        return None
    return output.stdout.strip() or None


def tag_exists(workspace: Path, tag: str) -> bool:
    return git(workspace, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0


def changed_paths(workspace: Path, parent_tag: str, child_tag: str) -> list[str]:
    """Return committed paths without quoting, newline translation, or byte loss."""
    output = git(workspace, "diff", "--name-only", "-z", parent_tag, child_tag, path_output=True).stdout
    return [path for path in output.split("\0") if path]


def working_tree_changed_paths(worktree: Path, parent_tag: str) -> list[str]:
    """Combine tracked and dirty paths while preserving filesystem names."""
    tracked = git(worktree, "diff", "--name-only", "-z", parent_tag, "--", path_output=True).stdout
    paths = [path for path in tracked.split("\0") if path]
    for path in dirty_paths(worktree):
        if path not in paths:
            paths.append(path)
    return paths


def evaluator_tree(workspace: Path, tag: str) -> str:
    return git_stdout(workspace, "rev-parse", f"{tag}:evaluator")


def commit_paths(workspace: Path, paths: list[str], message: str) -> str:
    git(workspace, "add", "--", *paths)
    git(workspace, "commit", "-m", message)
    return git_stdout(workspace, "rev-parse", "HEAD")


def create_tag(workspace: Path, tag: str) -> None:
    git(workspace, "tag", tag)


def dirty_paths(workspace: Path) -> list[str]:
    """Return dirty destination paths, skipping separate rename/copy sources."""
    output = git(workspace, "status", "--porcelain", "-z", path_output=True).stdout
    records = iter(output.split("\0"))
    paths: list[str] = []
    for record in records:
        if not record:
            continue
        paths.append(record[3:])
        if any(status in "RC" for status in record[:2]):
            next(records)  # NUL-delimited renames/copies put the source in a separate record.
    return paths


def add_worktree(repo: Path, path: Path, ref: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(path), ref)


def worktree_paths(repo: Path) -> list[Path]:
    """Return every registered worktree path from Git's stable porcelain form."""

    return [
        Path(line.removeprefix("worktree ")).resolve()
        for line in git_stdout(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path)
