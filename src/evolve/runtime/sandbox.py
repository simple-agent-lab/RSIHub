"""Offline execution with explicit data mounts and no ambient host authority."""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .process import OwnedResult, run_owned


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    timeout_s: float
    memory_mb: int = 1024
    pids: int = 128
    docker: str = "docker"

    def validate(self) -> None:
        if not self.image or self.image.startswith("-") or any(c in self.image for c in "\0\r\n"):
            raise RuntimeError("sandbox requires an explicit Docker image")
        if isinstance(self.timeout_s, bool) or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise RuntimeError("sandbox requires a positive finite timeout")
        if any(type(n) is not int or n < 1 for n in (self.memory_mb, self.pids)):
            raise RuntimeError("sandbox memory and process limits must be positive integers")
        if os.getuid() == 0:
            raise RuntimeError("sandbox launcher must run as a non-root user")


def _environment() -> dict[str, str]:
    # This environment belongs to the Docker client, never to the container.
    return {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR") if k in os.environ}


def resolve_image(config: SandboxConfig, directory: Path) -> str:
    """Resolve once and execute by immutable image ID; never implicitly pull."""
    config.validate()
    result = run_owned(
        [config.docker, "image", "inspect", "--format", "{{.Id}}", config.image],
        cwd=directory,
        env=_environment(),
        timeout_s=15,
    )
    identity = result.stdout.strip()
    if result.returncode or result.timed_out or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise RuntimeError("sandbox image is not available locally as an immutable image ID")
    return identity


def run_sandbox(
    config: SandboxConfig,
    *,
    image_id: str,
    command: list[str],
    inputs: Path,
    output: Path,
    container_name: str | None = None,
) -> OwnedResult:
    """Only /input (read-only) and /output (writable) are host-mounted.

    The caller must prepare public inputs and treat every output as untrusted.
    No evaluator, repository, Docker socket, host PID namespace, network or
    environment credential is granted. Model access requires a separate broker.
    """
    config.validate()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("sandbox requires a resolved immutable image ID")
    if not command or any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise RuntimeError("sandbox requires a valid command")
    for path in (inputs, output):
        if path.is_symlink() or not path.is_dir() or any(c in str(path.resolve()) for c in ",\n\r\0"):
            raise RuntimeError("sandbox mounts must be real directories with unambiguous paths")
    inputs, output = inputs.resolve(), output.resolve()
    if inputs.is_relative_to(output) or output.is_relative_to(inputs):
        raise RuntimeError("sandbox input and output must be separate trees")
    name = container_name or "evolve-sandbox-" + uuid.uuid4().hex
    if not re.fullmatch(r"evolve-sandbox-[a-f0-9]{32}", name):
        raise RuntimeError("invalid sandbox container name")
    argv = [
        config.docker,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        str(config.pids),
        "--memory",
        f"{config.memory_mb}m",
        "--memory-swap",
        f"{config.memory_mb}m",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        "--workdir",
        "/output",
        "--mount",
        f"type=bind,src={inputs},dst=/input,readonly",
        "--mount",
        f"type=bind,src={output},dst=/output",
        "--entrypoint",
        command[0],
        image_id,
        *command[1:],
    ]
    try:
        return run_owned(argv, cwd=output, env=_environment(), timeout_s=config.timeout_s)
    finally:
        # A killed Docker CLI can leave the daemon-owned workload alive.
        cleanup = run_owned([config.docker, "rm", "-f", name], cwd=output, env=_environment(), timeout_s=15)
        if cleanup.timed_out or (cleanup.returncode and "No such container" not in cleanup.stderr):
            raise RuntimeError("sandbox container cleanup is unconfirmed; inspect the Docker daemon")


def boundary_receipt(image_id: str) -> dict[str, object]:
    return {
        "image_id": image_id,
        "network": "none",
        "host_environment_forwarded": [],
        "input_mount": {"path": "/input", "read_only": True},
        "output_mount": {"path": "/output", "read_only": False},
        "host_pid_namespace": False,
        "docker_socket": False,
        "uid": os.getuid(),
    }
