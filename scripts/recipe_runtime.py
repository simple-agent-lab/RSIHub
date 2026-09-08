"""Resolve the mutation image and exact CLI version from a selected recipe."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def resolve(recipe: str) -> tuple[str, str, str]:
    pins = dict(
        line.split("=", 1)
        for line in (ROOT / "containers/runtime-versions.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    selected = Path(recipe)
    if selected.is_dir():
        selected /= "evolve.yaml"
    elif not selected.is_file():
        selected = ROOT / "recipes" / recipe / "evolve.yaml"
    if not selected.is_file():
        raise ValueError("recipe file not found")
    config = yaml.safe_load(selected.read_text())["operators"]["mutate"]["config"]
    agent = config.get("agent")
    if agent == "codex":
        kind, version = "codex", config.get("agent_kwargs", {}).get("version", pins["CODEX_MUTATE_VERSION"])
    elif agent == "evolve.integrations.harbor.miniswe_task_file:InstalledMiniSweAgent":
        kind, version = "miniswe", pins["MINISWE_VERSION"]
    else:
        raise ValueError("setup supports Codex and installed MiniSWE mutation images only")
    image = config.get("image")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("operators.mutate.config.agent_kwargs.version must be an exact version string")
    if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]*", image):
        raise ValueError("operators.mutate.config.image must be a buildable image tag")
    for role in ("CODEX_MUTATE", "CODEX_RESEARCH", "MINISWE"):
        if image == pins[role + "_IMAGE"] and version != pins[role + "_VERSION"]:
            raise ValueError("version differs from the reserved image tag; choose a distinct config.image tag")
    return kind, version, image


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe")
    args = parser.parse_args()
    try:
        print("\t".join(resolve(args.recipe)))
    except (ValueError, KeyError, TypeError, AttributeError, OSError, yaml.YAMLError) as exc:
        parser.error(str(exc))
