"""Task-environment RPC: task paths cross the boundary, host paths never do."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from harbor.environments.base import ExecResult

from ...runtime.files import read_regular_file, read_tree, write_regular_file, write_tree


def encode_tree(root: Path) -> dict[str, dict[str, Any]]:
    return {
        name: {"data": base64.b64encode(data).decode(), "executable": bool((root / name).stat().st_mode & 0o111)}
        for name, data in read_tree(root).items()
    }


def decode_tree(root: Path, value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("file transfer requires a file map")
    files = {}
    for name, entry in value.items():
        if not isinstance(entry, dict) or set(entry) != {"data", "executable"} or type(entry["executable"]) is not bool:
            raise ValueError("invalid file transfer entry")
        files[name] = base64.b64decode(entry["data"], validate=True)
    write_tree(root, files)
    for name, entry in value.items():
        (root / name).chmod(0o700 if entry["executable"] else 0o600)


class WorkerEnvironment:
    def __init__(self, output: Path, *, default_user: str | int | None = None):
        self.output = output
        self.default_user = default_user

    async def _request(self, method: str, arguments: dict[str, Any]) -> Any:
        request_id = uuid.uuid4().hex
        write_regular_file(
            self.output,
            f"rpc/requests/{request_id}.json",
            json.dumps({"method": method, "arguments": arguments}).encode(),
        )
        response = self.output / f"rpc/responses/{request_id}.json"
        while not response.exists():
            await asyncio.sleep(0.02)
        payload = json.loads(read_regular_file(self.output, f"rpc/responses/{request_id}.json"))
        if "error" in payload:
            raise RuntimeError(payload["error"])
        return payload["value"]

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        return ExecResult.model_validate(
            await self._request(
                "exec", {"command": command, "cwd": cwd, "env": env, "timeout_sec": timeout_sec, "user": user}
            )
        )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        path = Path(source_path)
        data = read_regular_file(path.parent, path.name)
        await self._request(
            "upload_file",
            {
                "target_path": target_path,
                "data": base64.b64encode(data).decode(),
                "executable": bool(path.stat().st_mode & 0o111),
            },
        )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await self._request("upload_dir", {"target_dir": target_dir, "files": encode_tree(Path(source_dir))})

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        data = await self._request("download_file", {"source_path": source_path})
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_regular_file(path.parent, path.name, base64.b64decode(data, validate=True))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        files = await self._request("download_dir", {"source_dir": source_dir})
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            decoded = Path(temporary) / "decoded"
            decode_tree(decoded, files)
            for name, data in read_tree(decoded).items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                write_regular_file(target, name, data)


async def dispatch_environment(environment: Any, payload: Any) -> Any:
    """Invoke only the supplied task environment, never arbitrary host methods."""
    shapes = {
        "exec": {"command", "cwd", "env", "timeout_sec", "user"},
        "upload_file": {"target_path", "data", "executable"},
        "upload_dir": {"target_dir", "files"},
        "download_file": {"source_path"},
        "download_dir": {"source_dir"},
    }
    if not isinstance(payload, dict) or set(payload) != {"method", "arguments"}:
        raise ValueError("invalid environment request")
    method, args = payload["method"], payload["arguments"]
    if not isinstance(method, str) or method not in shapes or not isinstance(args, dict) or set(args) != shapes[method]:
        raise ValueError("unsupported environment request")
    if method == "exec":
        if not isinstance(args["command"], str) or not args["command"]:
            raise ValueError("environment command must be a non-empty string")
        if args["cwd"] is not None and not isinstance(args["cwd"], str):
            raise ValueError("invalid task cwd")
        if args["env"] is not None and (
            not isinstance(args["env"], dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in args["env"].items())
        ):
            raise ValueError("invalid task environment")
        if args["timeout_sec"] is not None and (type(args["timeout_sec"]) is not int or args["timeout_sec"] <= 0):
            raise ValueError("invalid task command timeout")
        if args["user"] is not None and type(args["user"]) not in {str, int}:
            raise ValueError("invalid task user")
        return (await environment.exec(**args)).model_dump(mode="json")
    task_path = args.get("target_path", args.get("target_dir", args.get("source_path", args.get("source_dir"))))
    if not isinstance(task_path, str) or not task_path or "\0" in task_path:
        raise ValueError("invalid task path")
    with tempfile.TemporaryDirectory(prefix="evolve-task-transfer-") as temporary:
        local = Path(temporary) / "data"
        if method == "upload_file":
            if type(args["executable"]) is not bool:
                raise ValueError("invalid file mode")
            local.write_bytes(base64.b64decode(args["data"], validate=True))
            local.chmod(0o700 if args["executable"] else 0o600)
            await environment.upload_file(local, task_path)
        elif method == "upload_dir":
            decode_tree(local, args["files"])
            await environment.upload_dir(local, task_path)
        elif method == "download_file":
            await environment.download_file(task_path, local)
            return base64.b64encode(read_regular_file(local.parent, local.name)).decode()
        else:
            await environment.download_dir(task_path, local)
            return encode_tree(local)
    return None
