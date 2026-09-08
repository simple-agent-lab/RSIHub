"""Harbor proxy that never imports candidate-owned adapter code on the host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.agent.context import AgentContext

from ...candidate.package import export_candidate, materialize_candidate
from ...runtime.files import make_directories, read_regular_file, read_tree, write_regular_file, write_tree
from ...runtime.policy import POLICY
from ...runtime.process import run_owned
from ...runtime.sandbox import SandboxConfig, resolve_image, run_sandbox
from ._time_budget import read_agent_time_budget
from ._worker_environment import dispatch_environment, encode_tree


class IsolatedCandidateAgent(BaseAgent):
    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        candidate_import_path: str,
        worker_image: str,
        worker_timeout_s: float = 3600,
        logger=None,
        extra_env=None,
        **kwargs,
    ):
        super().__init__(logs_dir, model_name=model_name, logger=logger, extra_env=extra_env)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", worker_image):
            raise ValueError("candidate worker requires an immutable image ID")
        self._config = SandboxConfig(worker_image, worker_timeout_s)
        self._config.validate()
        self._import_path = candidate_import_path
        self._kwargs = kwargs
        self._root = logs_dir.parent / "candidate-isolation"
        self._number = 0
        self._seen: dict[str, str] = {}
        self._version: str | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._future = None
        self._name = "evolve-sandbox-" + uuid.uuid4().hex
        self._closed = False

    @staticmethod
    def name() -> str:
        return "isolated-candidate"

    def version(self) -> str | None:
        return self._version

    def _start(self) -> None:
        self._root.mkdir(parents=True, exist_ok=False)
        inputs, output = self._root / "input", self._root / "output"
        inputs.mkdir()
        output.mkdir()
        (inputs / "control").mkdir()
        for name in ("control", "rpc/requests", "rpc/responses"):
            (output / name).mkdir(parents=True)
        (self._root / "rpc-ledger").mkdir()
        source = Path(self._extra_env.get("EVOLVE_CANDIDATE_SOURCE", ""))
        if not source.is_absolute() or source.name != "target":
            raise ValueError("candidate worker requires an explicit target source")
        receipt = export_candidate(source.parent, "HEAD", self._root / "package")
        materialize_candidate(self._root / "package", receipt["sha256"], inputs / "target")
        framework = Path(__file__).resolve().parents[2]
        files = {
            p.relative_to(framework).as_posix(): p.read_bytes()
            for p in framework.rglob("*.py")
            if "__pycache__" not in p.parts
        }
        write_tree(inputs / "framework/evolve", files)
        config = {
            "import_path": self._import_path,
            "model_name": self.model_name,
            "kwargs": {**self._kwargs, "extra_env": {**self._extra_env, "EVOLVE_CANDIDATE_SOURCE": "/input/target"}},
        }
        (inputs / "agent.json").write_text(json.dumps(config, default=lambda x: x.model_dump(mode="json")))
        budget = read_agent_time_budget(self.logs_dir)
        if budget["status"] == "available":
            (output / "config.json").write_text(json.dumps({"agent": {"override_timeout_sec": budget["limit_s"]}}))
        identity = resolve_image(self._config, self._root)
        (self._root / "receipt.json").write_text(
            json.dumps(
                {
                    "package_sha256": receipt["sha256"],
                    "candidate_commit": receipt["candidate_commit"],
                    "image_id": identity,
                    "import_path": self._import_path,
                    "status": "started",
                    "usage_status": "unknown",
                }
            )
        )
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._future = self._executor.submit(
            run_sandbox,
            self._config,
            image_id=identity,
            command=[
                "env",
                "PYTHONPATH=/input:/input/framework",
                "python3",
                "-m",
                "evolve.integrations.harbor._candidate_worker",
            ],
            inputs=inputs,
            output=output,
            container_name=self._name,
        )

    def _send_phase(self, phase: str, **data: Any) -> Path:
        self._number += 1
        write_regular_file(
            self._root / "input", f"control/{self._number}.json", json.dumps({"phase": phase, **data}).encode()
        )
        return self._root / "output/control" / f"{self._number}.json"

    def _response(self, response: Path, context: AgentContext | None = None) -> dict[str, Any]:
        payload = json.loads(read_regular_file(response.parent, response.name, max_bytes=POLICY.max_file_bytes))
        if not isinstance(payload, dict):
            raise RuntimeError("invalid candidate worker response")
        self._version = payload.get("version", self._version)
        if context is not None and "context" in payload:
            result = AgentContext.model_validate(payload["context"])
            for key, value in result.model_dump().items():
                setattr(context, key, value)
        receipt_path = self._root / "receipt.json"
        if "context" in payload and receipt_path.is_file():
            value = AgentContext.model_validate(payload["context"]).cost_usd
            receipt = json.loads(receipt_path.read_text())
            known = value is not None and math.isfinite(value) and value >= 0
            receipt.update(usage_status="reported" if known else "unknown", reported_cost_usd=value if known else None)
            receipt_path.write_text(json.dumps(receipt))
        self._sync_logs()
        if "error" in payload:
            raise NonZeroAgentExitCodeError(f"candidate worker {payload['error']}: {payload.get('message', '')}")
        return payload

    def _check_alive(self) -> None:
        if self._future is None or self._future.done():
            if self._future is not None:
                result = self._future.result()
                (self._root / "worker.stdout").write_text(result.stdout)
                (self._root / "worker.stderr").write_text(result.stderr)
            raise RuntimeError("candidate worker stopped before returning its phase result")

    async def _rpc(self, environment: Any) -> None:
        output = self._root / "output"
        for path in sorted((output / "rpc/requests").glob("*.json")):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                raise RuntimeError("invalid candidate environment request ID")
            encoded = read_regular_file(output, f"rpc/requests/{path.name}", max_bytes=POLICY.max_file_bytes)
            digest = hashlib.sha256(encoded).hexdigest()
            if path.stem in self._seen:
                if self._seen[path.stem] != digest:
                    raise RuntimeError("candidate environment request ID changed")
                continue
            self._seen[path.stem] = digest
            ledger = self._root / "rpc-ledger"
            write_regular_file(ledger, path.name, json.dumps({"status": "started", "sha256": digest}).encode())
            try:
                value = await dispatch_environment(environment, json.loads(encoded))
                answer = {"value": value}
            except Exception as exc:
                # Host exception details may contain private host paths or credentials.
                answer = {"error": type(exc).__name__ + ": task environment request failed"}
            write_regular_file(
                ledger,
                path.name,
                json.dumps({"status": "completed", "sha256": digest, "error": answer.get("error")}).encode(),
            )
            write_regular_file(output, f"rpc/responses/{path.name}", json.dumps(answer).encode())

    async def _phase(
        self, phase: str, environment: Any, *, result_context: AgentContext | None = None, **data: Any
    ) -> dict[str, Any]:
        response = self._send_phase(phase, default_user=environment.default_user, **data)
        while not response.exists():
            self._check_alive()
            await self._rpc(environment)
            await asyncio.sleep(POLICY.poll_interval_s)
        return self._response(response, result_context)

    async def setup(self, environment) -> None:
        try:
            self._start()
            await self._phase("setup", environment)
        except BaseException:
            await asyncio.to_thread(self._shutdown)
            raise

    async def run(self, instruction, environment, context: AgentContext) -> None:
        try:
            await self._phase(
                "run",
                environment,
                result_context=context,
                instruction=instruction,
                context=context.model_dump(mode="json"),
            )
            if not context.is_empty():
                await asyncio.to_thread(self._shutdown)
            # Empty contexts are parsed after Harbor synchronizes task logs. No
            # further environment requests are serviced while waiting for that hook.
        except NonZeroAgentExitCodeError:
            # A reported candidate failure leaves the worker alive. Preserve its
            # partial usage and, when empty, the same-instance post-run parser.
            if not context.is_empty():
                await asyncio.to_thread(self._shutdown)
            raise
        except BaseException:
            await asyncio.to_thread(self._shutdown)
            raise

    def populate_context_post_run(self, context: AgentContext) -> None:
        if self._closed:
            return
        try:
            response = self._send_phase("post_run", logs=encode_tree(self.logs_dir))
            deadline = time.monotonic() + 30
            while not response.exists():
                self._check_alive()
                if time.monotonic() >= deadline:
                    raise RuntimeError("candidate post-run parsing timed out")
                time.sleep(POLICY.poll_interval_s)
            result = AgentContext.model_validate(self._response(response)["context"])
            for key, value in result.model_dump().items():
                setattr(context, key, value)
        finally:
            self._shutdown()

    def _sync_logs(self) -> None:
        source = self._root / "output/logs"
        if not source.is_dir():
            return
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        for name, data in read_tree(source).items():
            make_directories(self.logs_dir, str(Path(name).parent))
            write_regular_file(self.logs_dir, name, data)

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._future is None:
            return
        self._send_phase("stop")
        try:
            try:
                result = self._future.result(timeout=2)
            except TimeoutError:
                run_owned(
                    [self._config.docker, "rm", "-f", self._name],
                    cwd=self._root,
                    env=dict(os.environ),
                    timeout_s=POLICY.cleanup_timeout_s,
                )
                result = self._future.result(timeout=20)
            (self._root / "worker.stdout").write_text(result.stdout)
            (self._root / "worker.stderr").write_text(result.stderr)
            receipt = json.loads((self._root / "receipt.json").read_text())
            receipt.update(status="stopped", returncode=result.returncode, timed_out=result.timed_out)
            (self._root / "receipt.json").write_text(json.dumps(receipt))
        finally:
            if self._executor is not None:
                self._executor.shutdown(wait=False)
