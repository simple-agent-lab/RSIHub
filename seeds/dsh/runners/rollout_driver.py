"""Run one dsh session for one Harbor task (spawned as a subprocess by DshAgent).

Process-group isolation: on timeout the parent kills the whole group,
including dsh's Node runtime. All DSH_* variables are placed into this
process's environment by the parent; the dsh runtime subprocess inherits
them, which is how ``!!js process.env.*`` expressions in the cordis
composition resolve.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from deepseek_harness import DeepSeekHarness


def main() -> int:
    container = os.environ["DSH_CONTAINER"]
    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.Config.WorkingDir}}", container],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Container working directory: prefer the image's declared WorkingDir, then
    # a caller-provided default, finally "/" (always exists). Falling back to a
    # missing directory would make `docker exec -w` fail and the shell exit
    # immediately.
    os.environ["DSH_CONTAINER_CWD"] = inspect.stdout.strip() or os.environ.get("DSH_CONTAINER_CWD") or "/"

    # The dsh SDK is installed from source; use the dev node runtime carrier.
    os.environ.setdefault("DSH_RUNTIME_MODE", "node")

    instruction = Path(os.environ["DSH_TASK_FILE"]).read_text()
    session_root = os.environ["DSH_SESSION_ROOT"]
    Path(session_root).mkdir(parents=True, exist_ok=True)

    # `include` plugin paths cannot use !!js (evaluated before !!js), so the
    # base composition carries a literal __CANDIDATE_PROFILE__ placeholder that
    # is replaced here with the candidate's absolute profile path. The
    # candidate's files stay in the candidate directory, so its relative
    # plugins/skills paths keep working.
    candidate_profile = str(Path(os.environ["DSH_CANDIDATE_DIR"]) / "profile.cordis.yml")
    base = Path(os.environ["DSH_ROLLOUT_CORDIS"]).read_text()
    effective = base.replace("__CANDIDATE_PROFILE__", candidate_profile)
    effective_path = Path(session_root).parent / "rollout.effective.cordis.yml"
    effective_path.write_text(effective)

    with DeepSeekHarness(
        provider="deepseek-official",
        model=os.environ.get("DSH_MODEL", "deepseek-v4-flash"),
        max_tokens=int(os.environ.get("DSH_MAX_TOKENS", "49152")),
        cwd=os.environ["DSH_HOST_WORKSPACE"],
        session_root=session_root,
        cordis=str(effective_path),
    ) as harness:
        result = harness.run(instruction, session_id=os.environ.get("DSH_SESSION_ID", "task"))

    final = getattr(result, "final_response", None) or ""
    out = os.environ.get("DSH_FINAL_RESPONSE")
    if out:
        Path(out).write_text(final)
    print(final[-2000:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
