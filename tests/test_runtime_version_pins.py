"""Keep standalone runtime defaults aligned with the repository pin manifest."""

import ast
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PINS = dict(
    line.split("=", 1)
    for line in (ROOT / "containers/runtime-versions.env").read_text().splitlines()
    if line and not line.startswith("#")
)


def test_codex_standalone_defaults_match_manifest():
    docker = (ROOT / "containers/mutate-codex/Dockerfile").read_text()
    assert re.findall(r"^ARG CODEX_VERSION=(.+)$", docker, re.M) == [PINS["CODEX_MUTATE_VERSION"]] * 2
    seed = tomllib.loads((ROOT / "seeds/codex/codex.toml").read_text())
    assert seed["codex"]["version"] == PINS["CODEX_SEED_VERSION"]
    tree = ast.parse((ROOT / "scripts/codex_agent_controller.py").read_text())
    defaults = [
        ast.literal_eval(keyword.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "--require-version"
        for keyword in node.keywords
        if keyword.arg == "default"
    ]
    assert defaults == [PINS["CODEX_CONTROLLER_VERSION"]]


def test_harbor_and_miniswe_pins_match_build_and_lock():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "harbor==" + PINS["HARBOR_VERSION"] in project["project"]["dependencies"]
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    assert [p["version"] for p in locked["package"] if p["name"] == "harbor"] == [PINS["HARBOR_VERSION"]]
    worker = (ROOT / "containers/candidate-worker/Dockerfile").read_text()
    assert "version('harbor') == '" + PINS["HARBOR_VERSION"] + "'" in worker
    miniswe = (ROOT / "containers/mutate/Dockerfile").read_text()
    assert re.findall(r"^ARG MINISWE_VERSION=(.+)$", miniswe, re.M) == [PINS["MINISWE_VERSION"]]


def test_shipped_mutation_image_tags_match_their_role():
    for path in (ROOT / "recipes").glob("*/evolve.yaml"):
        recipe = yaml.safe_load(path.read_text())
        mutate = recipe["operators"].get("mutate", {}).get("config", {})
        image = mutate.get("image", "")
        if image.startswith("evolve-mutate-codex:"):
            role = "CODEX_RESEARCH" if path.parent.name == "hyperagents_codex_tbench_full" else "CODEX_MUTATE"
            assert image == PINS[role + "_IMAGE"]
            if role == "CODEX_RESEARCH":
                assert mutate["agent_kwargs"]["version"] == PINS[role + "_VERSION"]
                assert recipe["evaluator"]["agent_kwargs"]["version"] == PINS[role + "_VERSION"]
        if image.startswith("evolve-mutate-app:"):
            assert image == PINS["MINISWE_IMAGE"]
