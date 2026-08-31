from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup_terminal_bench.sh"

FAKE_TOOL = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
log = Path(os.environ["SETUP_CALLS"])
with log.open("a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")

if name == "docker":
    state = Path(os.environ["DOCKER_STATE"])
    images = json.loads(state.read_text()) if state.exists() else {}
    if args == ["info"]:
        raise SystemExit(12 if os.environ.get("FAIL_DOCKER_INFO") == "1" else 0)
    if args[:2] == ["image", "inspect"]:
        image = args[-1]
        if image not in images:
            raise SystemExit(1)
        if "--format" in args:
            print(images[image])
        raise SystemExit(0)
    if args[:1] == ["build"]:
        image = args[args.index("-t") + 1]
        images[image] = "2.4.5" if "mutate-app" in image else "0.146.0"
        state.write_text(json.dumps(images))
        raise SystemExit(0)
    raise SystemExit(2)

if name == "uv":
    if args[:2] == ["sync", "--frozen"]:
        raise SystemExit(0)
    command = args[2:] if args[:2] == ["run", "--frozen"] else []
    if command[:2] == ["harbor", "download"]:
        output = Path(command[command.index("-o") + 1])
        (output / "terminal-bench").mkdir(parents=True)
        if os.environ.get("FAIL_DOWNLOAD") == "1":
            raise SystemExit(9)
        raise SystemExit(0)
    if command[:1] == ["python"]:
        destination = Path(command[-1])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "dataset-source.json").write_text("{}\n")
        raise SystemExit(0)
    raise SystemExit(2)

if name == "git":
    if args == ["--version"]:
        print(f"git version {os.environ.get('FAKE_GIT_VERSION', '2.40.0')}")
        raise SystemExit(0)
    raise SystemExit(0)
raise SystemExit(2)
"""


def _environment(tmp_path: Path, **values: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool = fake_bin / "tool"
    tool.write_text(FAKE_TOOL)
    tool.chmod(0o755)
    for name in ("docker", "git", "uv"):
        (fake_bin / name).symlink_to(tool)
    calls = tmp_path / "calls.jsonl"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "EVOLVE_ASSET_DIR": str(tmp_path / "assets"),
            "SETUP_CALLS": str(calls),
            "DOCKER_STATE": str(tmp_path / "docker-images"),
            **values,
        }
    )
    return environment, calls


def _calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _run(tmp_path: Path, recipe: str, **values: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    environment, calls = _environment(tmp_path, **values)
    result = subprocess.run(
        ["bash", str(SCRIPT), recipe], cwd=ROOT, env=environment, text=True, capture_output=True, check=False
    )
    return result, _calls(calls)


def test_setup_rejects_recipes_outside_the_main_terminal_bench_set(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "gepa_local")

    assert result.returncode != 0
    assert "supported recipes" in result.stderr
    assert calls == []


def test_setup_stops_before_download_when_docker_is_unavailable(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "ahe", FAIL_DOCKER_INFO="1")

    assert result.returncode != 0
    assert "Docker daemon" in result.stderr
    assert calls == [["git", "--version"], ["docker", "info"]]


def test_setup_rejects_git_too_old_for_harbor_sparse_checkout(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "gepa", FAKE_GIT_VERSION="2.20.1")

    assert result.returncode != 0
    assert "Git 2.25 or newer" in result.stderr
    assert calls == [["git", "--version"]]


def test_setup_resolves_relative_asset_root_from_the_callers_directory(tmp_path: Path) -> None:
    environment, calls_path = _environment(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    environment["EVOLVE_ASSET_DIR"] = "assets"

    result = subprocess.run(
        ["bash", str(SCRIPT), "gepa"], cwd=caller, env=environment, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert (caller / "assets" / "terminal-bench-2-30-v1" / "dataset-source.json").is_file()
    assert _calls(calls_path)


def test_setup_rejects_an_existing_incomplete_raw_directory(tmp_path: Path) -> None:
    environment, calls_path = _environment(tmp_path)
    raw = Path(environment["EVOLVE_ASSET_DIR"]) / "raw"
    raw.mkdir(parents=True)
    (raw / "partial-download").write_text("incomplete\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "gepa"], cwd=ROOT, env=environment, text=True, capture_output=True, check=False
    )

    assert result.returncode != 0
    assert "incomplete raw dataset directory" in result.stderr
    assert not any(call[0:5] == ["uv", "run", "--frozen", "harbor", "download"] for call in _calls(calls_path))


def test_setup_downloads_once_and_builds_codex_image_for_ahe(tmp_path: Path) -> None:
    environment, calls_path = _environment(tmp_path)

    first = subprocess.run(
        ["bash", str(SCRIPT), "ahe"], cwd=ROOT, env=environment, text=True, capture_output=True, check=False
    )
    second = subprocess.run(
        ["bash", str(SCRIPT), "ahe"], cwd=ROOT, env=environment, text=True, capture_output=True, check=False
    )
    calls = _calls(calls_path)

    assert first.returncode == second.returncode == 0
    assert sum(call[0:5] == ["uv", "run", "--frozen", "harbor", "download"] for call in calls) == 1
    assert any(
        call[:5]
        == [
            "uv",
            "run",
            "--frozen",
            "python",
            "scripts/examples/terminal_bench_smoke/prepare_dataset.py",
        ]
        for call in calls
    )
    builds = [call for call in calls if call[:2] == ["docker", "build"]]
    assert len(builds) == 1
    assert "evolve-mutate-codex:20260818-codex0146" in builds[0]
    assert "./scripts/run_recipe_demo.sh ahe" in second.stdout


def test_setup_builds_codex_image_for_gepa(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "gepa")

    assert result.returncode == 0, result.stderr
    build = next(call for call in calls if call[:2] == ["docker", "build"])
    assert "evolve-mutate-codex:20260818-codex0146" in build


def test_setup_rebuilds_a_stale_image_with_the_expected_tag(tmp_path: Path) -> None:
    environment, calls_path = _environment(tmp_path)
    Path(environment["DOCKER_STATE"]).write_text(json.dumps({"evolve-mutate-codex:20260818-codex0146": "stale"}))

    result = subprocess.run(
        ["bash", str(SCRIPT), "gepa"], cwd=ROOT, env=environment, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert any(call[:2] == ["docker", "build"] for call in _calls(calls_path))


def test_setup_propagates_download_failure_without_building(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "hyperagents", FAIL_DOWNLOAD="1")

    assert result.returncode == 9
    assert not any(call[:2] == ["docker", "build"] for call in calls)
    assert not (tmp_path / "assets" / "raw" / "terminal-bench").exists()


def test_setup_script_is_portable_bash() -> None:
    assert shutil.which("bash")
    result = subprocess.run(["bash", "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert sys.platform in {"darwin", "linux"}
