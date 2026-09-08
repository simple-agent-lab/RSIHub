"""Independent supervisor owns container deadlines, bounded transport and cleanup."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from evolve.runtime.files import read_tree, write_tree
from evolve.runtime.sandbox_io import (
    ARCHIVE_LIMIT,
    RESPONSES,
    archive_tree,
    capture,
    is_response,
    mirror_output,
    unpack_tree,
)


def supervise(job: dict, receipt: Path) -> dict:
    docker, name = job["docker"], job["name"]
    output = Path(job["output"])
    started = time.monotonic()
    deadline = started + job["timeout_s"]
    result = {"returncode": 1, "stdout": "", "stderr": "", "wall_s": 0.0, "timed_out": False}
    previous = {name for name in read_tree(output) if not is_response(name)}
    sent: dict[str, bytes] = {}

    def save(state: str) -> None:
        receipt.write_text(json.dumps({"container": name, "owner_pid": job["owner_pid"], "state": state, **result}))

    def command(args: list[str], **kwargs):
        if os.getppid() != job["owner_pid"]:
            raise RuntimeError("sandbox owner exited; container terminated")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result["timed_out"] = True
            raise RuntimeError("sandbox deadline exceeded")
        try:
            return capture([docker, *args], timeout=min(15, remaining), **kwargs)
        except RuntimeError:
            if time.monotonic() >= deadline:
                result["timed_out"] = True
            raise

    def sync() -> None:
        nonlocal previous, sent
        # Read a bounded tmpfs snapshot. No candidate archive is extracted by Docker onto the host.
        code, data, error = command(["exec", name, "tar", "-cf", "-", "-C", "/output", "."], limit=ARCHIVE_LIMIT)
        if code:
            raise RuntimeError("cannot read sandbox output: " + error.decode(errors="replace")[:500])
        previous = mirror_output(output, unpack_tree(data), previous)
        replies = {}
        for prefix in RESPONSES:
            path = output / prefix
            if path.exists():
                replies.update({prefix + "/" + key: value for key, value in read_tree(path).items()})
        changed = {key: value for key, value in replies.items() if sent.get(key) != value}
        if changed:
            with tempfile.TemporaryDirectory(dir=receipt.parent) as temporary:
                path = Path(temporary) / "replies"
                write_tree(path, changed)
                code, _, _ = command(["exec", "-i", name, "tar", "-xf", "-", "-C", "/output"], data=archive_tree(path))
                if code:
                    raise RuntimeError("cannot return sandbox responses")
            sent = replies

    save("starting")
    try:
        code, _, error = command(job["argv"])
        if code:
            raise RuntimeError("sandbox creation failed: " + error.decode(errors="replace")[:1000])
        code, _, error = command(["exec", "-i", name, "tar", "-xf", "-", "-C", "/output"], data=archive_tree(output))
        if code:
            raise RuntimeError("cannot initialize sandbox output: " + error.decode(errors="replace")[:1000])
        code, _, _ = command(["exec", name, "sh", "-c", "touch /tmp/.evolve-start"])
        if code:
            raise RuntimeError("cannot start sandbox command")
        save("running")
        while True:
            if os.getppid() != job["owner_pid"]:
                raise RuntimeError("sandbox owner exited; container terminated")
            if time.monotonic() >= deadline:
                result["timed_out"] = True
                raise RuntimeError("sandbox deadline exceeded")
            code, state, _ = command(["inspect", "--format", "{{json .State}}", name])
            if code:
                raise RuntimeError("sandbox state unavailable")
            status = json.loads(state)
            if not status["Running"]:
                raise RuntimeError("sandbox terminated before output handoff")
            sync()
            code, finished, _ = command(["exec", name, "sh", "-c", "cat /tmp/.evolve-exit 2>/dev/null || true"])
            if code:
                raise RuntimeError("sandbox completion state unavailable")
            if finished:
                result["returncode"] = int(finished)
                # Recopy after completion: the previous snapshot may predate final writes.
                sync()
                break
            time.sleep(0.02)
        code, stdout, stderr = command(["logs", "--tail", "1000", name])
        if code:
            raise RuntimeError("sandbox logs unavailable")
        result.update(stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"))
    except Exception as exc:
        result.update(returncode=1, stderr=str(exc)[:2000])
    finally:
        try:
            code, _, error = capture([docker, "rm", "-f", name], timeout=15)
        except Exception as exc:
            code, error = 1, str(exc).encode()
        if code and b"No such container" not in error:
            result.update(returncode=1, stderr="sandbox container cleanup is unconfirmed")
        result["wall_s"] = time.monotonic() - started
        save("stopped" if code == 0 or b"No such container" in error else "cleanup_unconfirmed")
    return result


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise RuntimeError("sandbox supervisor interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    job_path = Path(sys.argv[1])
    job = json.loads(job_path.read_text())
    print(json.dumps(supervise(job, job_path.with_suffix(".receipt.json"))))
