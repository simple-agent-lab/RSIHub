"""Run one dsh self-modification session (spawned by mutate_local.py).

cwd = the candidate profile inside the child worktree; dsh reads the
evidence it is given and edits its own persona / plugins / skills there.
Process-group isolation: on timeout the parent kills the whole group.

SDK contract: ``dsh_home`` + ``profile`` + ``patches`` only (no ``session_root``
/ ``cordis`` kwargs). ``DSH_SESSION_ROOT`` is the isolated harness home.

The mutate cordis file is copied under ``dsh_home`` before launch so package
resolution uses the harness installation fallback, matching rollout.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

from deepseek_harness import DeepSeekHarness


def _ensure_runtime_mode() -> None:
    """Delegate to runtime_mode.ensure_runtime_mode (exe prefer when unset; prepare pins node for Harbor)."""
    path = Path(__file__).resolve().parent / "runtime_mode.py"
    spec = importlib.util.spec_from_file_location("dsh_runtime_mode", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load runtime mode helper from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ensure_runtime_mode()


def main() -> int:
    _ensure_runtime_mode()

    prompt = Path(os.environ["DSH_TASK_FILE"]).read_text()
    dsh_home = Path(os.environ["DSH_SESSION_ROOT"])
    dsh_home.mkdir(parents=True, exist_ok=True)

    # Copy the frozen mutate overlay into dsh_home so @deepseek-ai/* resolves
    # via $DSH_HOME/profiles/node_modules (same as rollout candidate overlays).
    mutate_src = Path(os.environ["DSH_MUTATE_CORDIS"])
    mutate_patch = dsh_home / "mutate.overlay.cordis.yml"
    shutil.copyfile(mutate_src, mutate_patch)

    with DeepSeekHarness(
        provider="deepseek-official",
        model=os.environ.get("DSH_MODEL", "deepseek-v4-pro"),
        max_tokens=int(os.environ.get("DSH_MAX_TOKENS", "49152")),
        cwd=os.environ["DSH_MUTATE_CWD"],
        dsh_home=str(dsh_home),
        profile=os.environ.get("DSH_PROFILE", "sdk-minimal"),
        patches=(str(mutate_patch),),
    ) as harness:
        result = harness.run(prompt, session_id=os.environ.get("DSH_SESSION_ID", "mutate"))

    final = getattr(result, "final_response", None) or ""
    out = os.environ.get("DSH_FINAL_RESPONSE")
    if out:
        Path(out).write_text(final)
    print(final[-3000:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
