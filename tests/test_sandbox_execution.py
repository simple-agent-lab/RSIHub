from pathlib import Path

import pytest

from evolve.runtime.process import OwnedResult
from evolve.runtime.sandbox import SandboxConfig, resolve_image, run_sandbox

IMAGE = "sha256:" + "a" * 64


def test_sandbox_never_forwards_host_authority_and_always_cleans_up(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("PRIVATE_TEST_TOKEN", "private")
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return OwnedResult(0, "", "", 0.1, command[1] == "run")

    monkeypatch.setattr("evolve.runtime.sandbox.run_owned", run)
    result = run_sandbox(
        SandboxConfig("fixture", 1), image_id=IMAGE, command=["sh", "-c", "true"], inputs=inputs, output=output
    )
    assert result.timed_out
    command, kwargs = calls[0]
    assert "PRIVATE_TEST_TOKEN" not in kwargs["env"]
    assert "--network=none" in command
    assert "--cap-drop=ALL" in command
    assert "--read-only" in command
    assert "--pull=never" in command
    assert command.count("--mount") == 2
    assert command[-3:] == [IMAGE, "-c", "true"]
    assert calls[1][0][:3] == ["docker", "rm", "-f"]


def test_cleanup_failure_is_not_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    monkeypatch.setattr(
        "evolve.runtime.sandbox.run_owned",
        lambda command, **kwargs: OwnedResult(1 if command[1] == "rm" else 0, "", "daemon unavailable", 0.1, False),
    )
    with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
        run_sandbox(SandboxConfig("fixture", 1), image_id=IMAGE, command=["true"], inputs=inputs, output=output)


def test_image_resolution_rejects_missing_image_without_pull(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return OwnedResult(1, "", "missing", 0, False)

    monkeypatch.setattr("evolve.runtime.sandbox.run_owned", run)
    with pytest.raises(RuntimeError, match="not available locally"):
        resolve_image(SandboxConfig("fixture", 1), tmp_path)
    assert len(calls) == 1 and calls[0][1:3] == ["image", "inspect"]


@pytest.mark.parametrize("nested", [True, False])
def test_sandbox_rejects_overlapping_mounts(tmp_path: Path, monkeypatch, nested: bool) -> None:
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="separate trees"):
        run_sandbox(
            SandboxConfig("fixture", 1),
            image_id=IMAGE,
            command=["true"],
            inputs=root if nested else child,
            output=child if nested else root,
        )
