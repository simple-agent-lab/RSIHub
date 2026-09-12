"""Run one dsh self-modification session (spawned by mutate_local.py).

cwd = the candidate profile inside the child worktree; dsh reads the
evidence it is given and edits its own persona / plugins / skills there.
Process-group isolation: on timeout the parent kills the whole group.

SDK contract: ``dsh_home`` + ``profile`` + ``patches`` only (no ``session_root``
/ ``cordis`` kwargs). ``DSH_SESSION_ROOT`` is the isolated harness home.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from deepseek_harness import DeepSeekHarness


def main() -> int:
    os.environ.setdefault("DSH_RUNTIME_MODE", "node")

    prompt = Path(os.environ["DSH_TASK_FILE"]).read_text()
    dsh_home = Path(os.environ["DSH_SESSION_ROOT"])
    dsh_home.mkdir(parents=True, exist_ok=True)

    with DeepSeekHarness(
        provider="deepseek-official",
        model=os.environ.get("DSH_MODEL", "deepseek-v4-pro"),
        max_tokens=int(os.environ.get("DSH_MAX_TOKENS", "49152")),
        cwd=os.environ["DSH_MUTATE_CWD"],
        dsh_home=str(dsh_home),
        profile=os.environ.get("DSH_PROFILE", "sdk-minimal"),
        patches=(os.environ["DSH_MUTATE_CORDIS"],),
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
