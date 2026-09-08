import json
from dataclasses import asdict
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
        return OwnedResult(0, json.dumps(asdict(OwnedResult(1, "", "", 0.1, True))), "", 0.1, False)

    monkeypatch.setattr("evolve.runtime.sandbox.run_owned", run)
    result = run_sandbox(
        SandboxConfig("fixture", 1), image_id=IMAGE, command=["sh", "-c", "true"], inputs=inputs, output=output
    )
    assert result.timed_out
    supervisor, kwargs = calls[0]
    job = json.loads(Path(supervisor[-1]).read_text())
    command = job["argv"]
    assert "PRIVATE_TEST_TOKEN" not in kwargs["env"]
    assert "--network=none" in command
    assert "--cap-drop=ALL" in command
    assert "--read-only" in command
    assert "--pull=never" in command
    assert command.count("--mount") == 1
    assert any("/output:rw,nosuid,nodev,size=64m" in arg for arg in command)
    assert command[-3:] == ["sh", "-c", "true"]
    assert job["owner_pid"] > 0
    assert supervisor[1:3] == ["-m", "evolve.runtime.sandbox_supervisor"]


def test_cleanup_failure_is_not_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    monkeypatch.setattr(
        "evolve.runtime.sandbox.run_owned",
        lambda command, **kwargs: OwnedResult(
            0,
            json.dumps(asdict(OwnedResult(1, "", "sandbox container cleanup is unconfirmed", 0.1, False))),
            "",
            0.1,
            False,
        ),
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


def test_nondefault_limits_are_identical_in_launch_and_receipt(tmp_path, monkeypatch):
    from evolve.runtime.policy import POLICY

    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    job = {}

    def run(command, **kwargs):
        job.update(json.loads(Path(command[-1]).read_text()))
        return OwnedResult(0, json.dumps(asdict(OwnedResult(0, "", "", 0.1, False))), "", 0.1, False)

    monkeypatch.setattr("evolve.runtime.sandbox.run_owned", run)
    run_sandbox(
        SandboxConfig("fixture", 2, memory_mb=256, pids=32, output_mb=1),
        image_id=IMAGE,
        command=["true"],
        inputs=inputs,
        output=output,
    )
    boundary = job["boundary"]
    assert boundary["output_mount"]["max_bytes"] == 1024 * 1024
    assert any("/output:rw,nosuid,nodev,size=1m," in arg for arg in job["argv"])
    assert boundary["resources"] == {"memory_mb": 256, "pids": 32, "timeout_s": 2}
    assert job["argv"][job["argv"].index("--memory") + 1] == "256m"
    assert job["argv"][job["argv"].index("--pids-limit") + 1] == "32"
    assert boundary["resource_policy"] == POLICY.receipt()


@pytest.mark.parametrize("limit", [0, 65, True, 1.5])
def test_output_cannot_exceed_host_policy(limit, monkeypatch):
    monkeypatch.setattr("evolve.runtime.sandbox.os.getuid", lambda: 1000)
    with pytest.raises(RuntimeError, match="sandbox output"):
        SandboxConfig("fixture", 2, output_mb=limit).validate()
