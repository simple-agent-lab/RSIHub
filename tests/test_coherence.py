"""Enforces docs/ARCHITECTURE.md and coding-style constraints.

When a rot pattern is caught in review, add an assertion here so the
suite accumulates immune memory.
"""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path

from evolve.frozen import interfaces

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "evolve"
ARCHITECTURE_ROW = re.compile(r"^\| `([^`]+\.py)` \| (\d+) \|", re.MULTILINE)
TOTAL_BUDGET = re.compile(r"^Total `src/evolve/` budget: \*\*(\d+) lines\*\*\.", re.MULTILINE)


def _module_paths() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _module_relpaths() -> set[str]:
    return {path.relative_to(SRC).as_posix() for path in _module_paths()}


def _architecture_budgets() -> tuple[dict[str, int], int]:
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    rows = ARCHITECTURE_ROW.findall(architecture)
    budgets = {name: int(limit) for name, limit in rows}
    total = TOTAL_BUDGET.search(architecture)
    assert len(rows) == len(budgets), "docs/ARCHITECTURE.md must not list a module more than once"
    assert total is not None, "docs/ARCHITECTURE.md must declare the total src/evolve budget"
    return budgets, int(total.group(1))


def test_architecture_table_is_the_module_and_budget_authority() -> None:
    budgets, documented_total = _architecture_budgets()
    actual = _module_relpaths()

    assert set(budgets) == actual
    over_budget = {
        path.relative_to(SRC).as_posix(): (
            len(path.read_text().splitlines()),
            budgets[path.relative_to(SRC).as_posix()],
        )
        for path in _module_paths()
        if len(path.read_text().splitlines()) > budgets[path.relative_to(SRC).as_posix()]
    }
    assert over_budget == {}
    assert sum(budgets.values()) == documented_total
    assert sum(len(path.read_text().splitlines()) for path in _module_paths()) <= documented_total


def test_population_delegates_evaluation_identity() -> None:
    source = (SRC / "population.py").read_text()
    assert "hashlib" not in source
    assert "json.dumps" not in source


def test_no_test_hooks_in_mechanism() -> None:
    for path in _module_paths():
        text = path.read_text()
        for pattern in ("EVOLVE_FAKE", "MUTATE_FAKE"):
            assert pattern not in text, f"test hook {pattern!r} in {path}"


def test_local_superpowers_artifacts_are_not_tracked() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--", ".superpowers", "docs/superpowers"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_artifacts = result.stdout.splitlines()
    assert not tracked_artifacts, f"transient Superpowers artifacts must not be tracked: {tracked_artifacts}"


def test_generated_python_caches_are_not_tracked() -> None:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked = result.stdout.splitlines()
    assert not [path for path in tracked if "__pycache__/" in path or path.endswith(".pyc")]


def test_stamped_fields_defined_once() -> None:
    defining = [
        path.relative_to(SRC).as_posix()
        for path in _module_paths()
        if re.search(r"^STAMPED_FIELDS\s*=", path.read_text(), re.M)
    ]
    assert defining == ["archive.py"], (
        f"STAMPED_FIELDS defined in {defining}; single source of truth is archive.py - import it"
    )


def test_operator_registry_is_the_single_source() -> None:
    # Every *Operator ABC is registered exactly once, and sdk.py dispatches each —
    # so adding an operator is one registry entry that everything else derives from.
    defined = {
        obj.__name__
        for _name, obj in vars(interfaces).items()
        if inspect.isclass(obj) and _name.endswith("Operator") and obj is not object
    }
    registered = {spec.abc.__name__ for spec in interfaces.OPERATORS}
    assert defined == registered, f"unregistered operator ABCs: {defined ^ registered}"
    assert len(registered) == len(interfaces.OPERATORS), "duplicate operator in the registry"

    sdk_source = (SRC / "frozen" / "sdk.py").read_text()
    for spec in interfaces.OPERATORS:
        assert spec.abc.__name__ in sdk_source, f"sdk.py must dispatch {spec.abc.__name__}"
    # config's kind lists are derived, not hand-kept
    from evolve import config

    assert set(config.OPERATOR_KINDS) | set(config.OPTIONAL_OPERATOR_KINDS) == {s.kind for s in interfaces.OPERATORS}
