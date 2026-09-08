import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

from evolve.agent_optimizer import freeze_optimizer, verify_optimizer
from evolve.runtime import files
from evolve.runtime.sandbox_io import archive_tree, capture, mirror_output, unpack_tree
from evolve.runtime.sandbox_supervisor import supervise


def test_large_sparse_file_is_rejected_before_read(tmp_path, monkeypatch):
    (tmp_path / "large").touch()
    with (tmp_path / "large").open("wb") as stream:
        stream.truncate(files.MAX_FILE_BYTES + 1)
    with pytest.raises(RuntimeError, match="byte limit"):
        files.read_tree(tmp_path)


def test_tree_limits_count_and_total_bytes(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"123")
    (tmp_path / "b").write_bytes(b"456")
    monkeypatch.setattr(files, "MAX_TREE_BYTES", 5)
    with pytest.raises(RuntimeError, match="byte limit"):
        files.read_tree(tmp_path)
    monkeypatch.setattr(files, "MAX_TREE_BYTES", 10)
    monkeypatch.setattr(files, "MAX_TREE_ENTRIES", 1)
    with pytest.raises(RuntimeError, match="entry or depth"):
        files.read_tree(tmp_path)


def test_capture_bounds_stdout_and_stderr():
    with pytest.raises(RuntimeError, match="byte limit"):
        capture([sys.executable, "-c", "import sys; sys.stderr.write('x'*100000)"], limit=1000)


@pytest.mark.parametrize(
    "name,kind", [("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE), ("fifo", tarfile.FIFOTYPE)]
)
def test_untrusted_archive_rejected(name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        item = tarfile.TarInfo(name)
        item.type = kind
        archive.addfile(item)
    with pytest.raises(RuntimeError):
        unpack_tree(stream.getvalue())


def test_method_permissions_survive_freeze_and_transport_and_affect_identity(tmp_path):
    source = tmp_path / "method"
    source.mkdir()
    (source / "instructions.md").write_text("Run helper")
    helper = source / "helper.sh"
    helper.write_text("#!/bin/sh\necho ready\n")
    helper.chmod(0o755)
    first = freeze_optimizer(tmp_path, source)
    frozen = verify_optimizer(tmp_path, first)
    transported = tmp_path / "transported"
    files.write_tree(transported, files.read_tree(frozen))
    assert subprocess.check_output([str(transported / "helper.sh")], text=True).strip() == "ready"
    helper.chmod(0o644)
    second = freeze_optimizer(tmp_path, source)
    assert first["digest"] != second["digest"]
    (frozen / "helper.sh").chmod(0o600)
    with pytest.raises(RuntimeError, match="permissions"):
        verify_optimizer(tmp_path, first)


def test_mirror_keeps_host_replies_and_file_modes(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (remote / "run.sh").chmod(0o700)
    (remote / "broker/responses").mkdir(parents=True)
    (remote / "broker/responses/one.json").write_text("forged")
    local = tmp_path / "local"
    (local / "broker/responses").mkdir(parents=True)
    (local / "broker/responses/one.json").write_text("trusted")
    mirror_output(local, unpack_tree(archive_tree(remote)), set())
    assert os.access(local / "run.sh", os.X_OK)
    assert (local / "broker/responses/one.json").read_text() == "trusted"


def test_supervisor_cleans_up_after_owner_exit(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    owner = [42]
    calls = []
    monkeypatch.setattr("evolve.runtime.sandbox_supervisor.os.getppid", lambda: owner[0])

    def docker(command, **kwargs):
        calls.append(command)
        if command[1] == "inspect":
            owner[0] = 1
            return 0, b'{"Running":true}', b""
        return 0, b"", b""

    monkeypatch.setattr("evolve.runtime.sandbox_supervisor.capture", docker)
    result = supervise(
        {
            "docker": "docker",
            "name": "fixture",
            "owner_pid": 42,
            "timeout_s": 10,
            "output": str(output),
            "argv": ["run"],
        },
        tmp_path / "lease.json",
    )
    assert "owner exited" in result["stderr"]
    assert calls[-1] == ["docker", "rm", "-f", "fixture"]
    assert json.loads((tmp_path / "lease.json").read_text())["state"] == "stopped"


def test_mirror_removes_churn_and_handles_directory_to_file(tmp_path):
    tree = files.FileTree()
    tree["old/deep/value"] = b"old"
    previous = mirror_output(tmp_path, tree, set())
    replacement = files.FileTree()
    replacement["old"] = b"new"
    mirror_output(tmp_path, replacement, previous)
    assert (tmp_path / "old").read_bytes() == b"new"
    mirror_output(tmp_path, files.FileTree(), {"old"})
    assert list(tmp_path.iterdir()) == []


def test_legacy_optimizer_can_be_verified_and_readopted(tmp_path):
    import hashlib

    content = b"legacy instructions"
    hashes = {"instructions.md": hashlib.sha256(content).hexdigest()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    snapshot = tmp_path / "runs/agent-driven/optimizer/versions" / digest
    snapshot.mkdir(parents=True)
    (snapshot / "instructions.md").write_bytes(content)
    metadata = {"digest": digest, "files": hashes}
    assert verify_optimizer(tmp_path, metadata) == snapshot
    assert freeze_optimizer(tmp_path, snapshot) == metadata
    (snapshot / "instructions.md").chmod(0o700)
    with pytest.raises(RuntimeError, match="permissions"):
        verify_optimizer(tmp_path, metadata)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_supervisor_deadline_and_cleanup_receipt(tmp_path, monkeypatch, cleanup_fails):
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr("evolve.runtime.sandbox_supervisor.os.getppid", lambda: 42)
    calls = []

    def docker(command, **kwargs):
        calls.append(command)
        return (1, b"", b"daemon unavailable") if cleanup_fails else (0, b"", b"")

    monkeypatch.setattr("evolve.runtime.sandbox_supervisor.capture", docker)
    receipt = tmp_path / "receipt.json"
    result = supervise(
        {
            "docker": "docker",
            "name": "fixture",
            "owner_pid": 42,
            "timeout_s": 0,
            "output": str(output),
            "argv": ["run"],
        },
        receipt,
    )
    assert result["timed_out"] and result["returncode"] != 0
    assert calls == [["docker", "rm", "-f", "fixture"]]
    assert json.loads(receipt.read_text())["state"] == ("cleanup_unconfirmed" if cleanup_fails else "stopped")
