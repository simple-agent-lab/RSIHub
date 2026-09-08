"""Host-only model access and billing through a file transport, never a shell RPC."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .files import read_regular_file, write_regular_file
from .policy import POLICY
from .process import run_owned


@dataclass(frozen=True)
class ModelBrokerConfig:
    command: tuple[str, ...]
    max_requests: int
    max_cost_usd: float
    timeout_s: float = POLICY.model_request_timeout_s

    def validate(self) -> None:
        if not self.command or any(not isinstance(a, str) or not a or "\0" in a for a in self.command):
            raise RuntimeError("model broker requires a trusted host command")
        if type(self.max_requests) is not int or self.max_requests < 1:
            raise RuntimeError("model broker requires a positive request limit")
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in (self.max_cost_usd, self.timeout_s)):
            raise RuntimeError("model broker cost and timeout limits must be finite and positive")


class ModelBroker:
    """The handler receives private request/response paths, not command text.

    It must constrain the provider, model and credentials itself and report usage
    from the provider. Its code/config are host-owned, never candidate-owned.
    """

    def __init__(self, config: ModelBrokerConfig, output: Path, ledger: Path):
        config.validate()
        self.config, self.output, self.ledger = config, output, ledger
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.failure: str | None = None
        self.receipts: list[dict[str, Any]] = []
        self.seen: dict[str, str] = {}

    def __enter__(self) -> ModelBroker:
        # A started but unaccounted request cannot be retried by recreating a broker.
        self.ledger.mkdir(parents=True, exist_ok=False)
        (self.output / "broker/requests").mkdir(parents=True)
        (self.output / "broker/responses").mkdir(parents=True)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join()

    def usage(self) -> dict[str, int | float | None]:
        unknown = any(r.get("cost_usd") is None for r in self.receipts)
        return {
            "total_tokens": sum(r.get("total_tokens", 0) for r in self.receipts),
            "cost_usd": None if unknown else round(sum(r["cost_usd"] for r in self.receipts), 9),
        }

    def _serve(self) -> None:
        try:
            while not self.stop.is_set():
                for path in sorted((self.output / "broker/requests").glob("*.json")):
                    request_id = path.stem
                    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                        raise RuntimeError("invalid model request ID")
                    encoded = read_regular_file(
                        self.output, f"broker/requests/{path.name}", max_bytes=POLICY.max_file_bytes
                    )
                    digest = hashlib.sha256(encoded).hexdigest()
                    if request_id in self.seen:
                        if self.seen[request_id] != digest:
                            raise RuntimeError("model request ID was reused with different content")
                        continue
                    self.seen[request_id] = digest
                    self._request(request_id, encoded, digest)
                    if self.stop.is_set():
                        break
                self.stop.wait(POLICY.poll_interval_s)
        except BaseException as exc:
            self.failure = str(exc)
            self.stop.set()

    def _reply(self, request_id: str, response: dict[str, Any]) -> None:
        write_regular_file(self.output, f"broker/responses/{request_id}.json", json.dumps(response).encode())

    def _reject(self, request_id: str, reason: str) -> None:
        self._reply(
            request_id,
            {
                "status": 400,
                "content_type": "application/json",
                "body": base64.b64encode(json.dumps({"error": reason}).encode()).decode(),
            },
        )

    def _request(self, request_id: str, encoded: bytes, digest: str) -> None:
        usage = self.usage()
        if (
            usage["cost_usd"] is None
            or len(self.receipts) >= self.config.max_requests
            or usage["cost_usd"] >= self.config.max_cost_usd
        ):
            self._reject(request_id, "model request budget exhausted or cost unknown")
            return
        try:
            request = json.loads(encoded)
            if not isinstance(request, dict) or set(request) != {"body"} or not isinstance(request["body"], str):
                raise ValueError("model request must contain only a base64 body")
            body = base64.b64decode(request["body"], validate=True)
        except (ValueError, TypeError):
            self._reject(request_id, "invalid model request")
            return
        directory = self.ledger / request_id
        directory.mkdir()
        write_regular_file(directory, "request.body", body)
        receipt: dict[str, Any] = {
            "id": request_id,
            "request_sha256": digest,
            "status": "started",
            "cost_usd": None,
            "total_tokens": 0,
        }
        self.receipts.append(receipt)
        write_regular_file(directory, "receipt.json", json.dumps(receipt).encode())
        result = run_owned(
            [*self.config.command, str(directory / "request.body"), str(directory / "response.json")],
            cwd=directory,
            env=dict(os.environ),
            timeout_s=self.config.timeout_s,
        )
        try:
            if result.returncode or result.timed_out:
                raise ValueError("model handler failed or timed out")
            response = json.loads(read_regular_file(directory, "response.json", max_bytes=POLICY.max_file_bytes))
            self._validate_response(response)
        except (OSError, RuntimeError, ValueError, TypeError):
            receipt.update(status="unaccounted", returncode=result.returncode, timed_out=result.timed_out)
            write_regular_file(directory, "receipt.json", json.dumps(receipt).encode())
            self._reject(request_id, "model response or usage unavailable; accounting required")
            return
        receipt.update(status="completed", **response.pop("usage"))
        write_regular_file(directory, "receipt.json", json.dumps(receipt, allow_nan=False).encode())
        self._reply(request_id, response)

    @staticmethod
    def _validate_response(response: Any) -> None:
        if not isinstance(response, dict) or set(response) != {"status", "content_type", "body", "usage"}:
            raise ValueError("invalid model handler response")
        if type(response["status"]) is not int or not 100 <= response["status"] <= 599:
            raise ValueError("invalid model response status")
        if not isinstance(response["content_type"], str) or any(c in response["content_type"] for c in "\r\n"):
            raise ValueError("invalid model response content type")
        base64.b64decode(response["body"], validate=True)
        usage = response["usage"]
        if not isinstance(usage, dict) or set(usage) != {"total_tokens", "cost_usd"}:
            raise ValueError("invalid model usage")
        tokens, cost = usage["total_tokens"], usage["cost_usd"]
        if type(tokens) is not int or tokens < 0:
            raise ValueError("invalid model token count")
        if cost is not None and (type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0):
            raise ValueError("invalid model cost")
