"""Old recipe generations with real operators and local service substitutes.

Run with --run-slow after recipe/operator/driver changes. Linux seccomp blocks
network in the CLI and every descendant; no Docker or model service is used.
"""

import ctypes.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import UV_SOURCE_RECIPES, generated_workspace_uv_env, write_identity_dataset, write_locked_miniswe_seed

from evolve.config import load_config
from evolve.workspace import InitOptions, init_workspace

FIXTURES = Path(__file__).parent / "fixtures/recipe_cycle"
RECIPES = (
    "aevolve",
    "ahe",
    "ahe_codex",
    "gepa",
    "gepa_local",
    "hill_climb",
    "hill_climb_codex",
    "hyperagents",
    "hyperagents_codex",
)


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "linux" or not ctypes.util.find_library("seccomp"), reason="requires Linux seccomp")
@pytest.mark.parametrize("recipe", RECIPES)
def test_old_recipe_completes_generation_without_models(
    tmp_path: Path, recipe: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-only")
    seed = None
    if recipe in UV_SOURCE_RECIPES:
        seed = write_locked_miniswe_seed(tmp_path / "seed")
        prompt = seed / "src/minisweagent/config/mini.yaml"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("agent:\n  system_template: You are a helpful agent.\n  instance_template: '{{task}}'\n")
    workspace = tmp_path / "workspace"
    init_workspace(
        InitOptions(
            workspace=workspace,
            recipe=recipe,
            seed=str(seed) if seed else None,
            dataset=str(write_identity_dataset(tmp_path / "tasks", count=100)),
        )
    )
    original_config = load_config(workspace / "evolve.yaml")
    wrapper = tmp_path / "uv-offline"
    real_uv = shutil.which("uv")
    assert real_uv
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"if 'harbor' in sys.argv:\n"
        f"    os.execv({sys.executable!r}, [{sys.executable!r}, {str(FIXTURES / 'harbor.py')!r}, *sys.argv[sys.argv.index('harbor') + 1:]])\n"
        f"os.execv({real_uv!r}, [{real_uv!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    env = generated_workspace_uv_env()
    env.update(
        {
            "UV_OFFLINE": "1",
            "EVOLVE_UV_BINARY": str(wrapper),
            "RECIPE_HARBOR_LOG": str(tmp_path / "harbor.jsonl"),
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "OPENAI_API_KEY": "offline-test-only",
            "TERM": "dumb",
        }
    )
    env.pop("EVAL_STUB", None)
    env["EVOLVE_UV_CACHE_DIR"] = str(tmp_path / "candidate-cache")
    env["EVOLVE_UV_PYTHON_INSTALL_DIR"] = env["UV_PYTHON_INSTALL_DIR"]
    env.pop("EVOLVE_AGENT_COMMAND", None)
    for key in ("GITHUB_ACTIONS", "FORCE_COLOR", "CODEX_AUTH_JSON_PATH", "CODEX_FORCE_AUTH_JSON"):
        env.pop(key, None)
    command = [sys.executable, str(FIXTURES / "offline.py"), "-m", "evolve"]
    result = subprocess.run(
        [*command, "run", str(workspace), "--max-generations", "1"],
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    (tmp_path / "driver.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(line) for line in (workspace / "archive.jsonl").read_text().splitlines()]
    child = [row for row in rows if row.get("genid") == "1" and row.get("purpose") == "candidate"]
    assert child, result.stdout + result.stderr
    assert child[-1]["status"] == "complete"
    assert child[-1]["score"] == 1.0
    assert child[-1]["selection_eligible"] is True
    baseline = [row for row in rows if row.get("purpose") == "genesis"]
    assert baseline[-1]["score"] == 0.0
    assert all(row.get("cost_usd", 0) == 0 for row in rows)
    run = workspace / "runs/gen-1"
    assert json.loads((run / "gate.json").read_text())["valid_parent"] is True
    assert json.loads((run / "record/fields.json").read_text())
    assert json.loads((workspace / "best_ever.json").read_text())["genid"] == "1"
    analysis = json.loads((run / "analyze/summary.json").read_text())
    if recipe == "aevolve":
        assert analysis["judge_verdicts"] == analysis["cases"] > 0
    if recipe in {"ahe", "ahe_codex"}:
        assert analysis["debugger_errors"] == 0
        assert json.loads((run / "mutate/change_manifest.json").read_text())["changes"]
    if recipe in {"gepa", "gepa_local"}:
        comparison = json.loads((run / "validate/comparison.json").read_text())
        assert comparison["accepted"] is True
        assert comparison["child_total"] > comparison["parent_total"]
        assert comparison["child_infra_cases"] == []
    assert load_config(workspace / "evolve.yaml") == original_config
    assert (workspace / "runs/gen-1/mutate/patch.diff").read_text()
    assert (tmp_path / "harbor.jsonl").read_text()
    verification = subprocess.run(
        [*command, "verify", str(workspace)], env=env, capture_output=True, text=True, timeout=60
    )
    assert verification.returncode == 0, verification.stdout + verification.stderr


@pytest.mark.skipif(sys.platform != "linux" or not ctypes.util.find_library("seccomp"), reason="requires Linux seccomp")
def test_offline_guard_survives_child_exec() -> None:
    probe = """
import errno, socket
for family in (socket.AF_INET, socket.AF_INET6):
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        try:
            socket.socket(family, kind)
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise RuntimeError('descendant can create a network socket')
with socket.socket(socket.AF_UNIX):
    pass
"""
    child = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {probe!r}], check=True)"
    result = subprocess.run(
        [sys.executable, str(FIXTURES / "offline.py"), "-c", child],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
