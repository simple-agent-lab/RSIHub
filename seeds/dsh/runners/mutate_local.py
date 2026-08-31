"""Host command for the mutate stage (``runner: local``) — the meta agent.

Launched by the local mutation runner with cwd = the child worktree (checkout
root, containing ``target/``) and the framework-assembled feedback prompt at
``$EVOLVE_PROMPT_FILE``. This script anchors a dsh self-modification session
at ``<checkout>/target`` (the only editable root) using
``compositions/mutate.cordis.yml``.

The meta model is decoupled from ``evaluator.model``: set ``DSH_META_MODEL``
(default ``deepseek-v4-pro``). The endpoint follows the workspace's frozen
identity: ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` are mapped onto dsh's
``DEEPSEEK_*``.

Robustness: upstream model content filters can stochastically kill a session
whose evidence contains sensitive-looking payloads (e.g. raw DNA sequences in
a failed-task trajectory). The session is retried up to ``DSH_META_ATTEMPTS``
times, judged by whether ``target/`` actually changed; retries append a
warning to the prompt asking the agent to summarize rather than dump such
evidence.

This script is harness-side (outside the workspace); candidates cannot reach
it, and out-of-surface edits are rejected by the surface check regardless.

The mutate driver and cordis composition are resolved relative to this file
(``runners/`` ships inside the seed, protected by ``surface.exclude``); the
recipe invokes it as ``python3 target/runners/mutate_local.py`` with the child
checkout as cwd. Optional: ``DSH_HARNESS_REPO`` (dsh clone; enables the
official cordis composition-authoring skills inside the meta session).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def main() -> int:
    checkout = Path.cwd()
    target = checkout / "target"
    if not (target / "profile.cordis.yml").is_file():
        print(f"mutate_local: no target/profile.cordis.yml under {checkout}", file=sys.stderr)
        return 2

    prompt_file = os.environ.get("EVOLVE_PROMPT_FILE")
    if not prompt_file or not Path(prompt_file).is_file():
        print("mutate_local: EVOLVE_PROMPT_FILE missing", file=sys.stderr)
        return 2

    runners = Path(__file__).resolve().parent
    run_dir = Path(os.environ.get("EVOLVE_RUN_DIR", str(checkout / "runs" / "mutate-local")))

    env = os.environ.copy()
    base_url = env.get("OPENAI_BASE_URL")
    if base_url:
        env["DEEPSEEK_BASE_URL"] = base_url
    api_key = env.get("OPENAI_API_KEY")
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    skills_dir = env.get("DSH_MUTATE_SKILLS_DIR", "")
    harness_repo = env.get("DSH_HARNESS_REPO")
    if not skills_dir and harness_repo:
        skills_dir = str(Path(harness_repo) / "apps" / "cli" / "config" / "agent-presets" / "cordis" / "skills")
    env.update(
        {
            "DSH_TASK_FILE": prompt_file,
            "DSH_MUTATE_CWD": str(target),
            "DSH_MUTATE_CORDIS": env.get("DSH_MUTATE_CORDIS", str(runners / "compositions" / "mutate.cordis.yml")),
            "DSH_MODEL": env.get("DSH_META_MODEL", "deepseek-v4-pro"),
            "DSH_MUTATE_SKILLS_DIR": skills_dir,
            "DSH_SESSION_ROOT": str(run_dir / "mutate-sessions"),
            "DSH_FINAL_RESPONSE": str(run_dir / "mutate_final_response.txt"),
        }
    )

    timeout = float(env.get("DSH_META_TIMEOUT_SEC", "5400"))
    driver = runners / "mutate_driver.py"
    # mutate_driver imports deepseek_harness → it must run under the workspace
    # venv interpreter (this script itself is stdlib-only).
    workspace = os.environ.get("EVOLVE_WORKSPACE", "")
    venv_python = Path(workspace) / ".venv" / "bin" / "python" if workspace else None
    interpreter = str(venv_python) if venv_python and venv_python.is_file() else sys.executable
    run_dir.mkdir(parents=True, exist_ok=True)

    attempts = int(env.get("DSH_META_ATTEMPTS", "3"))
    base_session = env.get("DSH_SESSION_ID", f"mutate-{os.environ.get('EVOLVE_GENID', 'gen')}")
    for attempt in range(1, attempts + 1):
        env["DSH_SESSION_ID"] = f"{base_session}-a{attempt}"
        if attempt > 1:
            augmented = run_dir / f"mutation_prompt_attempt{attempt}.txt"
            augmented.write_text(
                Path(prompt_file).read_text()
                + "\n\nNOTE: A previous attempt was cut off by an upstream model content filter. "
                "Avoid pasting raw sensitive-looking payloads (e.g. DNA/protein sequences, credential "
                "dumps) into your context; summarize such evidence files instead of dumping them.\n"
            )
            env["DSH_TASK_FILE"] = str(augmented)
        rc = _run_driver(interpreter, driver, env, run_dir, timeout)
        changed = subprocess.run(
            ["git", "status", "--porcelain", "--", "target/"],
            cwd=checkout,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if changed:
            return 0
        print(f"mutate_local: attempt {attempt}/{attempts} produced no target/ changes (rc={rc})", file=sys.stderr)
    return 0  # let the framework record a clean no_proposal


def _run_driver(interpreter: str, driver: Path, env: dict, run_dir: Path, timeout: float) -> int:
    with open(run_dir / "mutate_local.log", "ab") as log:
        proc = subprocess.Popen(
            [interpreter, str(driver)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
            print(f"mutate_local: dsh meta session timed out after {timeout}s", file=sys.stderr)
            return 3


if __name__ == "__main__":
    sys.exit(main())
