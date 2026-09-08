import json
import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_recipe_demo.sh"

FAKE_UV = """\
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path(os.environ["UV_CALLS"]).open("a") as stream:
    stream.write(json.dumps(arguments) + "\\n")
if arguments[:2] != ["run", "--frozen"]:
    raise SystemExit(0)
command = arguments[2:]
if command[:1] == ["--env-file"]:
    for line in Path(command[1]).read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name, value = stripped.split("=", 1)
            os.environ.setdefault(name, value.strip("'\\\""))
    command = command[2:]
if command[:2] == ["sh", "-c"]:
    os.execvpe("sh", command, os.environ)
"""


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(textwrap.dedent(FAKE_UV))
    uv.chmod(0o755)
    return fake_bin, tmp_path / "uv-calls.jsonl"


def _environment(fake_bin: Path, calls: Path, **values: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "DATASET",
        "ENV_FILE",
        "EVOLVE_ASSET_DIR",
        "GENERATIONS",
        "OPENAI_API_KEY",
        "RECIPE",
        "SEED",
        "TASKS",
        "WORKSPACE",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "UV_CALLS": str(calls),
            **values,
        }
    )
    return environment


def _calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _assets(tmp_path: Path) -> Path:
    root = tmp_path / "assets"
    (root / "raw" / "terminal-bench").mkdir(parents=True)
    prepared = root / "terminal-bench-2-30-v1"
    prepared.mkdir()
    (prepared / "dataset-source.json").write_text("{}\n")
    return root


def test_recipe_demo_routes_common_overrides_through_uv(tmp_path: Path) -> None:
    fake_bin, calls_path = _fake_uv(tmp_path)
    workspace = tmp_path / "workspace"
    assets = _assets(tmp_path)

    subprocess.run(
        ["bash", str(SCRIPT), "gepa"],
        check=True,
        cwd=tmp_path,
        env=_environment(
            fake_bin,
            calls_path,
            EVOLVE_ASSET_DIR=str(assets),
            WORKSPACE=str(workspace),
            TASKS="4",
            GENERATIONS="2",
        ),
    )

    calls = _calls(calls_path)
    assert calls[0] == ["sync", "--frozen"]
    assert calls[1][2:4] == ["python", "scripts/examples/terminal_bench_smoke/prepare_dataset.py"]
    assert calls[2] == [
        "run",
        "--frozen",
        "evolve",
        "init",
        str(workspace),
        "--recipe",
        "gepa",
        "--dataset",
        str(assets / "terminal-bench-2-30-v1"),
        "--tasks",
        "4",
    ]
    assert calls[3] == ["run", "--frozen", f"{workspace}/evolve", "preflight", str(workspace)]
    assert calls[4] == [
        "run",
        "--frozen",
        f"{workspace}/evolve",
        "run",
        str(workspace),
        "--max-generations",
        "2",
        "--children-per-gen",
        "1",
    ]
    assert all(call[:2] == ["run", "--frozen"] for call in calls[1:])


def test_recipe_demo_loads_the_optional_env_file(tmp_path: Path) -> None:
    fake_bin, calls_path = _fake_uv(tmp_path)
    assets = _assets(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=file-key\n")

    subprocess.run(
        ["bash", str(SCRIPT)],
        check=True,
        cwd=tmp_path,
        env=_environment(fake_bin, calls_path, ENV_FILE=str(env_file), EVOLVE_ASSET_DIR=str(assets)),
    )

    calls = _calls(calls_path)
    assert calls[1][:4] == ["run", "--frozen", "--env-file", str(env_file)]
    assert calls[2] == [
        "run",
        "--frozen",
        "--env-file",
        str(env_file),
        "evolve",
        "init",
        str(ROOT / "runs" / "ahe-demo"),
        "--recipe",
        "ahe",
        "--dataset",
        str(assets / "terminal-bench-2-30-v1"),
    ]


def test_full_hyperagents_demo_uses_the_official_export_without_rebuilding_subset(tmp_path: Path) -> None:
    fake_bin, calls_path = _fake_uv(tmp_path)
    assets = _assets(tmp_path)
    workspace = tmp_path / "workspace"

    subprocess.run(
        ["bash", str(SCRIPT), "hyperagents_tbench_full"],
        check=True,
        cwd=tmp_path,
        env=_environment(fake_bin, calls_path, EVOLVE_ASSET_DIR=str(assets), WORKSPACE=str(workspace)),
    )

    calls = _calls(calls_path)
    assert not any("scripts/examples/terminal_bench_smoke/prepare_dataset.py" in call for call in calls)
    assert calls[1] == [
        "run",
        "--frozen",
        "evolve",
        "init",
        str(workspace),
        "--recipe",
        "hyperagents_tbench_full",
        "--dataset",
        str(assets / "raw" / "terminal-bench"),
    ]


def test_full_codex_hyperagents_demo_uses_the_official_export(tmp_path: Path) -> None:
    fake_bin, calls_path = _fake_uv(tmp_path)
    assets = _assets(tmp_path)
    workspace = tmp_path / "workspace"

    subprocess.run(
        ["bash", str(SCRIPT), "hyperagents_codex_tbench_full"],
        check=True,
        cwd=tmp_path,
        env=_environment(fake_bin, calls_path, EVOLVE_ASSET_DIR=str(assets), WORKSPACE=str(workspace)),
    )

    calls = _calls(calls_path)
    assert not any("scripts/examples/terminal_bench_smoke/prepare_dataset.py" in call for call in calls)
    assert calls[1] == [
        "run",
        "--frozen",
        "evolve",
        "init",
        str(workspace),
        "--recipe",
        "hyperagents_codex_tbench_full",
        "--dataset",
        str(assets / "raw" / "terminal-bench"),
    ]


def test_recipe_demo_resolves_relative_overrides_from_the_callers_directory(tmp_path: Path) -> None:
    fake_bin, calls_path = _fake_uv(tmp_path)
    assets = _assets(tmp_path / "caller")
    env_file = tmp_path / "caller" / "demo.env"
    env_file.write_text("OPENAI_API_KEY=file-key\n")

    subprocess.run(
        ["bash", str(SCRIPT), "gepa"],
        check=True,
        cwd=tmp_path / "caller",
        env=_environment(
            fake_bin,
            calls_path,
            ENV_FILE="demo.env",
            EVOLVE_ASSET_DIR="assets",
            WORKSPACE="workspace",
        ),
    )

    calls = _calls(calls_path)
    assert calls[1][:4] == ["run", "--frozen", "--env-file", str(env_file)]
    assert str(tmp_path / "caller" / "workspace") in calls[2]
    assert str(assets / "terminal-bench-2-30-v1") in calls[2]


def test_recipe_demo_rejects_missing_setup_assets_before_sync(tmp_path: Path) -> None:
    fake_bin, calls_path = _fake_uv(tmp_path)

    result = subprocess.run(
        ["bash", str(SCRIPT), "hill_climb"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_environment(fake_bin, calls_path, EVOLVE_ASSET_DIR=str(tmp_path / "missing-assets")),
    )

    assert result.returncode != 0
    assert "setup_terminal_bench.sh hill_climb" in result.stderr
    assert _calls(calls_path) == []


def test_recipe_demo_rejects_non_main_recipes_before_sync(tmp_path: Path) -> None:
    fake_bin, calls_path = _fake_uv(tmp_path)

    result = subprocess.run(
        ["bash", str(SCRIPT), "gepa_local"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_environment(fake_bin, calls_path, EVOLVE_ASSET_DIR=str(_assets(tmp_path))),
    )

    assert result.returncode != 0
    assert "supported recipes" in result.stderr
    assert _calls(calls_path) == []


def test_recipe_demo_remains_short_portable_bash() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    text = SCRIPT.read_text()
    for private in ("DevBox", "/data00", "proxy.env", "REPO_BUNDLE", "python -"):
        assert private not in text
    assert len(text.splitlines()) <= 35
