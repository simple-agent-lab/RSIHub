"""Select and preflight the dsh SDK runtime carrier (bundled exe vs node).

Editable ``uv add`` of ``sdk-runtime`` does **not** ship a ready carrier by
itself. After installing from a deepseek-harness checkout, run::

    pnpm exec tsx scripts/build-exe-for-python-sdk.ts

from the harness repo root so ``runtime/`` contains the platform exe and/or
the ``runtime/node/`` closure.

Mode selection (``DSH_RUNTIME_MODE``):

- ``exe`` — bundled platform executable (preferred when present; no system Node)
- ``node`` — dev-only node carrier on system Node >= 22.19 (must opt in)

When ``DSH_RUNTIME_MODE`` is unset, prefer the bundled exe if resolution
succeeds; otherwise fail with an actionable message rather than silently
defaulting to a missing node carrier.
"""

from __future__ import annotations

import os
from typing import Literal

RuntimeMode = Literal["exe", "node"]

_BUILD_HINT = (
    "build the SDK runtime carrier from the deepseek-harness checkout with "
    "`pnpm exec tsx scripts/build-exe-for-python-sdk.ts` "
    "(or install a wheel that already embeds the platform exe)"
)


def _try_resolve(mode: str | None) -> tuple[str, ...] | None:
    try:
        from deepseek_harness_runtime import resolve_bundled_launch_args
    except ImportError:
        return None
    for kwargs in (
        lambda: resolve_bundled_launch_args(mode),
        lambda: resolve_bundled_launch_args(mode=mode),
    ):
        try:
            return tuple(kwargs())
        except TypeError:
            continue
        except (FileNotFoundError, ValueError, OSError, AttributeError):
            return None
    return None


def detect_available_mode() -> RuntimeMode | None:
    """Return ``exe`` or ``node`` when that carrier resolves; else ``None``."""
    if _try_resolve("exe") is not None:
        return "exe"
    if _try_resolve("node") is not None:
        return "node"
    return None


def _runtime_package_importable() -> bool:
    try:
        import deepseek_harness_runtime  # noqa: F401
    except ImportError:
        return False
    return True


def ensure_runtime_mode(*, prefer_exe: bool = True) -> RuntimeMode:
    """Ensure ``DSH_RUNTIME_MODE`` is set to a resolvable carrier; return it.

    Raises ``RuntimeError`` with a clear fix-it message when the selected
    (or auto-detected) carrier is missing. If the ``deepseek_harness_runtime``
    package is not importable yet (incomplete editable install / unit stubs),
    fall back to recording ``node`` without resolving so drivers can still be
    exercised; an explicit ``DSH_RUNTIME_MODE`` still fails closed when the
    package is present or resolution is attempted.
    """
    explicit = os.environ.get("DSH_RUNTIME_MODE")
    package_ok = _runtime_package_importable()

    if explicit is not None and explicit != "":
        if explicit not in ("exe", "node"):
            raise RuntimeError(f"unsupported DSH_RUNTIME_MODE={explicit!r}; expected 'exe' or 'node'")
        if not package_ok:
            # Package missing: cannot validate carrier; honor the explicit mode.
            return explicit  # type: ignore[return-value]
        if _try_resolve(explicit) is None:
            if explicit == "node":
                raise RuntimeError("DSH_RUNTIME_MODE=node but the node runtime carrier is missing; " + _BUILD_HINT)
            raise RuntimeError("DSH_RUNTIME_MODE=exe but the bundled platform exe is missing; " + _BUILD_HINT)
        return explicit  # type: ignore[return-value]

    if not package_ok:
        os.environ["DSH_RUNTIME_MODE"] = "node"
        return "node"

    if prefer_exe:
        if _try_resolve("exe") is not None:
            os.environ["DSH_RUNTIME_MODE"] = "exe"
            return "exe"
        if _try_resolve("node") is not None:
            os.environ["DSH_RUNTIME_MODE"] = "node"
            return "node"
    else:
        found = detect_available_mode()
        if found is not None:
            os.environ["DSH_RUNTIME_MODE"] = found
            return found

    raise RuntimeError("no dsh runtime carrier found (bundled exe or runtime/node); " + _BUILD_HINT)
