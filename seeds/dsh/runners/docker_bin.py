"""Resolve the host docker CLI for Harbor docker-exec bash.

Cordis ``terminal-bash`` uses ``shellPath: process.env.DSH_DOCKER_BIN``. Never
assume ``/usr/bin/docker``: Homebrew Mac installs docker under
``/opt/homebrew/bin`` and ``/usr/bin/docker`` does not exist. Doctor and the
agent must resolve the same way so a PATH hit cannot false-green against a
hard-coded missing fallback.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping


def resolve_docker_bin(*, env: Mapping[str, str] | None = None) -> str:
    """Return an executable docker path.

    Prefer ``DSH_DOCKER_BIN`` when set and executable; otherwise ``shutil.which``.
    Raise ``RuntimeError`` with a clear message when neither works.
    """
    source = os.environ if env is None else env
    explicit = (source.get("DSH_DOCKER_BIN") or "").strip()
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise RuntimeError(f"DSH_DOCKER_BIN={explicit!r} is not an executable docker binary")
    found = shutil.which("docker", path=source.get("PATH"))
    if found:
        return found
    raise RuntimeError(
        "docker not found on PATH; install Docker or set DSH_DOCKER_BIN "
        "(Homebrew Mac: typically /opt/homebrew/bin/docker — do not assume /usr/bin/docker)"
    )
