"""Isolated lifecycle of a candidate adapter; run only inside the worker image."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

from harbor.models.agent.context import AgentContext

from ...runtime.files import read_regular_file, write_regular_file
from ._worker_environment import WorkerEnvironment, decode_tree


async def main() -> None:
    inputs, output = Path("/input"), Path("/output")
    config = json.loads((inputs / "agent.json").read_text())
    module, name = config["import_path"].split(":", 1)
    cls = getattr(importlib.import_module(module), name)
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    agent = cls(logs_dir=logs, model_name=config.get("model_name"), **config["kwargs"])
    environment = WorkerEnvironment(output)
    context = AgentContext()
    number = 0
    while True:
        number += 1
        control = inputs / "control" / f"{number}.json"
        while not control.exists():
            await asyncio.sleep(0.02)
        request = json.loads(control.read_text())
        phase = request["phase"]
        if phase == "stop":
            return
        environment.default_user = request.get("default_user")
        try:
            if phase == "setup":
                await agent.setup(environment=environment)
            elif phase == "run":
                context = AgentContext.model_validate(request["context"])
                await agent.run(instruction=request["instruction"], environment=environment, context=context)
            elif phase == "post_run":
                files = request["logs"]
                staging = output / f"post-run-{number}"
                decode_tree(staging, files)
                for path in staging.rglob("*"):
                    if path.is_file():
                        relative = path.relative_to(staging)
                        (logs / relative).parent.mkdir(parents=True, exist_ok=True)
                        write_regular_file(logs, relative.as_posix(), read_regular_file(staging, relative.as_posix()))
                agent.populate_context_post_run(context)
            else:
                raise ValueError("unsupported worker phase")
            response = {"context": context.model_dump(mode="json"), "version": agent.version()}
        except Exception as exc:
            response = {"error": type(exc).__name__, "message": str(exc), "context": context.model_dump(mode="json")}
        write_regular_file(output, f"control/{number}.json", json.dumps(response).encode())


if __name__ == "__main__":
    asyncio.run(main())
