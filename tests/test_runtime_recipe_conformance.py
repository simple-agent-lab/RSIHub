from pathlib import Path

import pytest
import yaml
from conftest import allow_local_runtime, contract_for_gen0, init_recipe_with_local_inputs

from evolve.config import recipe_root
from evolve.preflight import PreflightStatus, run_preflight


@pytest.mark.parametrize(
    "recipe",
    ["aevolve", "ahe", "gepa", "hyperagents", "hyperagents_codex_tbench_full", "hyperagents_tbench_full"],
)
def test_partner_recipe_runtime_conformance(
    tmp_path: Path,
    recipe: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = f"canary-key-{recipe}"
    endpoint = f"https://{recipe}.model-canary.example/v1"
    proxy = f"http://proxy-user:proxy-password@{recipe}.proxy-canary.example:8118"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    workspace = init_recipe_with_local_inputs(tmp_path, recipe)
    allow_local_runtime(monkeypatch)

    result = run_preflight(workspace)
    contract = contract_for_gen0(workspace)

    assert result.status is PreflightStatus.PASSED, (
        result.failure_category,
        result.failure_message,
    )
    assert contract.runtime_digest == result.runtime_digest
    assert result.receipt_path is not None
    receipt = result.receipt_path.read_text()
    assert all(literal not in receipt for literal in (key, endpoint, proxy, "proxy-password"))

    for root in (workspace / "target", workspace / "evaluator"):
        for path in root.rglob("*"):
            if path.is_file():
                assert path.name != "auth.json"


def test_builtin_recipes_declare_runtime_only_for_candidate_preparation() -> None:
    expected = {"ahe", "hill_climb", "hyperagents", "hyperagents_tbench_full"}
    configured = set()
    for recipe in recipe_root().iterdir():
        path = recipe / "evolve.yaml"
        if not path.is_file():
            continue
        payload = yaml.safe_load(path.read_text())
        runtime = payload["evaluator"].get("runtime")
        assert runtime != {}
        if runtime is not None:
            assert set(runtime) == {"candidate"}
            configured.add(recipe.name)
    assert configured == expected
