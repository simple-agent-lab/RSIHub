"""Loopback HTTP to file transport inside an isolated research container."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .files import read_regular_file, write_regular_file

_MAX_BYTES = 16 * 1024 * 1024


class ModelBridge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, output: Path, timeout_s: float = 125):
        self.output = output
        self.timeout_s = timeout_s
        super().__init__(("127.0.0.1", 0), _Request)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


class _Request(BaseHTTPRequestHandler):
    server: ModelBridge

    def log_message(self, format: str, *args: object) -> None:
        # Bodies and headers may contain private research material.
        pass

    def do_POST(self) -> None:
        if self.path not in {"/responses", "/v1/responses", "/chat/completions", "/v1/chat/completions"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= _MAX_BYTES or self.headers.get("Transfer-Encoding"):
                raise ValueError("invalid body length")
            self.connection.settimeout(self.server.timeout_s)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete body")
            identifier = uuid.uuid4().hex
            write_regular_file(
                self.server.output,
                f"broker/requests/{identifier}.json",
                json.dumps({"body": base64.b64encode(body).decode()}).encode(),
            )
            deadline = time.monotonic() + self.server.timeout_s
            while True:
                try:
                    encoded = read_regular_file(
                        self.server.output, f"broker/responses/{identifier}.json", max_bytes=32 * 1024 * 1024
                    )
                    break
                except FileNotFoundError:
                    if time.monotonic() >= deadline:
                        self.send_error(504, "model response timed out; request was not retried")
                        return
                    time.sleep(0.02)
            response = json.loads(encoded)
            status, content_type = response["status"], response["content_type"]
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("invalid response status")
            if not isinstance(content_type, str) or any(c in content_type for c in "\r\n"):
                raise ValueError("invalid response content type")
            data = base64.b64decode(response["body"], validate=True)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ValueError, KeyError, TypeError, OSError):
            self.send_error(502, "invalid model transport response")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=125)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a client command is required")
    with ModelBridge(Path("/output"), args.timeout) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {
                **os.environ,
                "OPENAI_BASE_URL": server.base_url,
                "EVOLVE_MODEL_BASE_URL": server.base_url,
                "OPENAI_API_KEY": "isolated-transport-placeholder",
            }
            result = subprocess.run(command, env=environment, check=False)
        finally:
            server.shutdown()
            thread.join()
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
