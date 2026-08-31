from pathlib import Path

from evolve.config import RECIPE_NAMES

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED = {
    "aevolve",
    "ahe",
    "ahe_codex",
    "gepa",
    "gepa_local",
    "hill_climb",
    "hill_climb_codex",
    "hyperagents",
    "hyperagents_codex",
    "hyperagents_dsh",
}


def _directories(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")}


def test_repository_exposes_only_supported_recipes() -> None:
    assert _directories(ROOT / "recipes") == SUPPORTED
    assert set(RECIPE_NAMES) == SUPPORTED


def test_recipe_fixtures_are_classified() -> None:
    assert _directories(ROOT / "tests/fixtures/recipes") == {
        "hill_climb-smoke",
        "hyperagents-smoke",
    }
