import asyncio
import base64

import pytest
from harbor.environments.base import ExecResult

from evolve.integrations.harbor._worker_environment import dispatch_environment


class Environment:
    def __init__(self):
        self.commands = []
        self.uploaded = None

    async def exec(self, **kwargs):
        self.commands.append(kwargs)
        return ExecResult(return_code=0, stdout="task output", stderr="")

    async def upload_file(self, source, destination):
        self.uploaded = (source.read_bytes(), destination)

    async def upload_dir(self, source, destination):
        self.uploaded = (
            {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()},
            destination,
        )

    async def download_file(self, source, destination):
        destination.write_bytes(b"task bytes")


def test_exec_is_dispatched_only_to_supplied_task_environment():
    environment = Environment()
    result = asyncio.run(
        dispatch_environment(
            environment,
            {
                "method": "exec",
                "arguments": {"command": "echo task", "cwd": None, "env": {}, "timeout_sec": 10, "user": None},
            },
        )
    )
    assert result["stdout"] == "task output"
    assert environment.commands[0]["command"] == "echo task"


def test_uploads_take_bytes_not_candidate_selected_host_paths():
    environment = Environment()
    asyncio.run(
        dispatch_environment(
            environment,
            {
                "method": "upload_file",
                "arguments": {
                    "target_path": "/task/file",
                    "data": base64.b64encode(b"hello").decode(),
                    "executable": False,
                },
            },
        )
    )
    assert environment.uploaded == (b"hello", "/task/file")
    with pytest.raises(ValueError):
        asyncio.run(
            dispatch_environment(
                environment,
                {"method": "upload_file", "arguments": {"source_path": "/host/private", "target_path": "/task/file"}},
            )
        )


@pytest.mark.parametrize("method", ["start", "stop", "__getattribute__", "get_host_path"])
def test_host_and_environment_management_are_not_rpc_capabilities(method):
    with pytest.raises(ValueError):
        asyncio.run(dispatch_environment(Environment(), {"method": method, "arguments": {}}))


@pytest.mark.parametrize("name", ["../private", "/private", "x/../../private"])
def test_upload_tree_cannot_write_outside_host_transfer_directory(tmp_path, name):
    with pytest.raises(RuntimeError):
        asyncio.run(
            dispatch_environment(
                Environment(),
                {
                    "method": "upload_dir",
                    "arguments": {"target_dir": "/task", "files": {name: {"data": "eA==", "executable": False}}},
                },
            )
        )


def test_download_symlink_does_not_disclose_host_file(tmp_path):
    private = tmp_path / "private"
    private.write_text("host secret")

    class MaliciousDownload(Environment):
        async def download_file(self, source, destination):
            destination.symlink_to(private)

    with pytest.raises(OSError):
        asyncio.run(
            dispatch_environment(
                MaliciousDownload(), {"method": "download_file", "arguments": {"source_path": "/task/file"}}
            )
        )
