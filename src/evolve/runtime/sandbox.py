"""Offline execution with explicit data mounts and no ambient host authority."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .policy import MIB, POLICY
from .process import OwnedResult, run_owned


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    timeout_s: float
    memory_mb: int = POLICY.default_memory_mb
    pids: int = POLICY.default_pids
    docker: str = "docker"
    output_mb: int = POLICY.max_output_mb

    def validate(self) -> None:
        if not self.image or self.image.startswith("-") or any(c in self.image for c in "\0\r\n"):
            raise RuntimeError("sandbox requires an explicit Docker image")
        if isinstance(self.timeout_s, bool) or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise RuntimeError("sandbox requires a positive finite timeout")
        if any(type(n) is not int or n < 1 for n in (self.memory_mb, self.pids)):
            raise RuntimeError("sandbox memory and process limits must be positive integers")
        if type(self.output_mb) is not int or not 1 <= self.output_mb <= POLICY.max_output_mb:
            raise RuntimeError(f"sandbox output must be between 1 and {POLICY.max_output_mb} MiB")
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
        timeout_s=POLICY.docker_command_timeout_s,
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
    """Only /input is host-mounted; /output is a bounded, mirrored tmpfs.

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
        "--detach",
        "--log-driver=local",
        f"--log-opt=max-size={POLICY.log_bytes}",
        f"--log-opt=max-file={POLICY.log_files}",
        "--log-opt=compress=false",
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
        f"/tmp:rw,nosuid,nodev,size={POLICY.tmp_mb}m",
        "--workdir",
        "/output",
        "--mount",
        f"type=bind,src={inputs},dst=/input,readonly",
        "--tmpfs",
        f"/output:rw,nosuid,nodev,size={config.output_mb}m,nr_inodes={POLICY.output_inodes},uid={os.getuid()},gid={os.getgid()},mode=0700",
        "--entrypoint",
        "/bin/sh",
        image_id,
        "-c",
        f"while [ ! -f /tmp/.evolve-start ]; do sleep {POLICY.poll_interval_s}; done; "
        '"$@"; code=$?; printf "%s" "$code" > /tmp/.evolve-exit; '
        "while :; do sleep 1; done",
        "evolve-entrypoint",
        *command,
    ]
    from .files import read_tree

    read_tree(output)  # Enforce input size/type limits before any daemon-side creation.
    lease = Path(tempfile.mkdtemp(prefix="sandbox-lease-", dir=inputs.parent))
    job_path = lease / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "docker": config.docker,
                "name": name,
                "owner_pid": os.getpid(),
                "timeout_s": config.timeout_s,
                "output": str(output),
                "argv": argv[1:],
                "boundary": boundary_receipt(image_id, config),
            }
        )
    )
    result = run_owned(
        [sys.executable, "-m", "evolve.runtime.sandbox_supervisor", str(job_path)],
        cwd=lease,
        env={**_environment(), "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        timeout_s=config.timeout_s + POLICY.supervisor_grace_s,
    )
    if result.returncode or result.timed_out:
        run_owned([config.docker, "rm", "-f", name], cwd=lease, env=_environment(), timeout_s=POLICY.cleanup_timeout_s)
        raise RuntimeError("sandbox supervisor failed; inspect its lease and cleanup receipt: " + str(lease))
    payload = json.loads(result.stdout)
    if "cleanup is unconfirmed" in payload["stderr"]:
        raise RuntimeError(payload["stderr"])
    return OwnedResult(**payload)


def boundary_receipt(image_id: str, config: SandboxConfig) -> dict[str, object]:
    return {
        "image_id": image_id,
        "network": "none",
        "host_environment_forwarded": [],
        "input_mount": {"path": "/input", "read_only": True},
        "output_mount": {"path": "/output", "read_only": False, "type": "tmpfs", "max_bytes": config.output_mb * MIB},
        "supervision": "independent host process with owner-death and deadline cleanup",
        "host_pid_namespace": False,
        "docker_socket": False,
        "uid": os.getuid(),
        "resources": {"memory_mb": config.memory_mb, "pids": config.pids, "timeout_s": config.timeout_s},
        "resource_policy": POLICY.receipt(),
    }
