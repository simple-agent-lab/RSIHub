"""Materialize a candidate cordis overlay under an isolated dsh_home.

``cordis-plugin-include`` of a profile living outside ``dsh_home`` makes Node
resolve bare ``@deepseek-ai/*`` imports from the candidate directory, which
has no runtime ``node_modules``. Writing the candidate entries as a ``--patch``
file under ``dsh_home`` keeps package resolution on the harness installation
fallback (``$DSH_HOME/profiles/node_modules``) while rewriting relative
``./plugins/*.mjs`` names to absolute paths so local plugins still load.
"""

from __future__ import annotations

import re
from pathlib import Path

# Match YAML `name: './plugins/...'` or `name: "./plugins/..."`.
_RELATIVE_PLUGIN = re.compile(
    r"""(?P<prefix>^\s*name:\s*)(?P<quote>['"])(?P<rel>\./[^'"]+)(?P=quote)""",
    re.MULTILINE,
)


def rewrite_relative_plugin_names(text: str, candidate_dir: Path) -> str:
    """Turn relative plugin names into absolute paths rooted at ``candidate_dir``."""

    def _replace(match: re.Match[str]) -> str:
        absolute = (candidate_dir / match.group("rel")).resolve()
        return f"{match.group('prefix')}{match.group('quote')}{absolute}{match.group('quote')}"

    return _RELATIVE_PLUGIN.sub(_replace, text)


def materialize_candidate_overlay(
    candidate_dir: Path,
    dsh_home: Path,
    *,
    source_name: str = "profile.cordis.yml",
    output_name: str = "candidate.overlay.cordis.yml",
) -> Path:
    """Copy the candidate profile into ``dsh_home`` with absolute plugin paths."""
    source = candidate_dir / source_name
    if not source.is_file():
        raise FileNotFoundError(f"candidate profile missing: {source}")
    dsh_home.mkdir(parents=True, exist_ok=True)
    output = dsh_home / output_name
    output.write_text(rewrite_relative_plugin_names(source.read_text(), candidate_dir))
    return output
