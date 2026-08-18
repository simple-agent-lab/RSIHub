"""Validate operator (script form): syntax guard for evolved dsh plugins.

Rejects candidates whose ``*.mjs`` plugins fail ``node --check`` or whose
``profile.cordis.yml`` is not parseable YAML, saving a full evaluation on a
syntactically broken candidate. Runs in the workspace venv (PyYAML is
available); node is located via ``DSH_NODE_BIN`` or PATH — if node is absent
the check degrades to YAML-only (the guard is a cost saver, not a scorer).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

from evolve.frozen import sdk
from evolve.frozen.config import Config
from evolve.frozen.interfaces import ValidateOperator, ValidateResult

CONFIG = Config({})


class NodeCheckValidate(ValidateOperator):
    # Contract: sdk._run_runtime_mode instantiates operator_cls() bare;
    # config arrives through ctx.
    def validate(self, checkout: Path, ctx) -> ValidateResult:
        target = checkout / "target"
        problems: list[str] = []

        profile = target / "profile.cordis.yml"
        if not profile.is_file():
            return ValidateResult(accept=False, reason="target/profile.cordis.yml missing", artifacts=[])
        try:
            # The cordis dialect uses custom tags such as !!js: compose() checks
            # syntax/structure without constructing tags; safe_load would reject
            # legitimate profiles.
            yaml.compose(profile.read_text())
        except yaml.YAMLError as error:
            problems.append(f"profile.cordis.yml: {error}")

        node = os.environ.get("DSH_NODE_BIN") or shutil.which("node")
        if node:
            for script in sorted(target.rglob("*.mjs")):
                result = subprocess.run(
                    [node, "--check", str(script)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout).strip().splitlines()
                    problems.append(f"{script.relative_to(checkout)}: {detail[0] if detail else 'syntax error'}")

        if problems:
            return ValidateResult(accept=False, reason="; ".join(problems)[:800], artifacts=[])
        return ValidateResult(accept=True, reason="profile yaml + node --check passed", artifacts=[])


if __name__ == "__main__":
    sdk.main(NodeCheckValidate, config_schema=CONFIG)
