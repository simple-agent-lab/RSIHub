"""Materialize a candidate cordis overlay under an isolated dsh_home.

``cordis-plugin-include`` of a profile living outside ``dsh_home`` makes Node
resolve bare ``@deepseek-ai/*`` imports from the candidate directory, which
has no runtime ``node_modules``. Writing the candidate entries as a ``--patch``
file under ``dsh_home`` keeps package resolution on the harness installation
fallback (``$DSH_HOME/profiles/node_modules``) while rewriting relative
``./plugins/*.mjs`` names to absolute paths so local plugins still load.

Rewrites use a YAML compose/serialize round-trip (not regex) so quote styles,
insert blocks, and non-plugin ``name`` fields stay intact. Custom tags such as
``!!js`` are preserved by PyYAML compose/serialize.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _is_relative_plugin(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("./")


def _rewrite_mapping(node: yaml.Node, candidate_dir: Path) -> None:
    if not isinstance(node, yaml.MappingNode):
        return
    for i, (key_node, value_node) in enumerate(node.value):
        if (
            isinstance(key_node, yaml.ScalarNode)
            and key_node.value == "name"
            and isinstance(value_node, yaml.ScalarNode)
            and _is_relative_plugin(value_node.value)
        ):
            absolute = str((candidate_dir / value_node.value).resolve())
            # Prefer double quotes so Windows and POSIX paths dump stably.
            node.value[i] = (
                key_node,
                yaml.ScalarNode(
                    tag=value_node.tag,
                    value=absolute,
                    style='"',
                ),
            )
        else:
            _rewrite_node(value_node, candidate_dir)


def _rewrite_node(node: yaml.Node, candidate_dir: Path) -> None:
    if isinstance(node, yaml.MappingNode):
        _rewrite_mapping(node, candidate_dir)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _rewrite_node(child, candidate_dir)


def rewrite_relative_plugin_names(text: str, candidate_dir: Path) -> str:
    """Turn relative plugin ``name`` fields into absolute paths under ``candidate_dir``."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        # Fall back to a line-oriented rewrite only if the document is not YAML;
        # valid cordis profiles always parse.
        raise
    if root is None:
        return text
    _rewrite_node(root, candidate_dir)
    dumped = yaml.serialize(root)
    # serialize() may omit a trailing newline; keep files newline-terminated.
    return dumped if dumped.endswith("\n") else dumped + "\n"


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
