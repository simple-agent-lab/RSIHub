from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SAFE_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ViewerWorkspace:
    slug: str
    workspace: Path
    label: str
    method: str | None = None
    agent: str | None = None
    selection_metric: str | None = None

    def public_record(self) -> dict[str, str | None]:
        return {
            "slug": self.slug,
            "label": self.label,
            "method": self.method,
            "agent": self.agent,
            "selection_metric": self.selection_metric,
            "workspace": str(self.workspace),
            "url": f"/experiments/{self.slug}/",
            "snapshot_url": f"/experiments/{self.slug}/api/evolve/snapshot",
        }


def load_catalog(path: Path) -> tuple[ViewerWorkspace, ...]:
    source = path.expanduser().resolve()
    try:
        document = yaml.safe_load(source.read_text())
    except OSError as exc:
        raise ValueError(f"cannot read viewer catalog {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid viewer catalog YAML {source}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("experiments"), list):
        raise ValueError("viewer catalog must contain an experiments list")

    entries: list[ViewerWorkspace] = []
    slugs: set[str] = set()
    for index, raw in enumerate(document["experiments"], start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"viewer catalog experiment {index} must be an object")
        workspace = _workspace_path(raw, source.parent, index)
        slug = _slug(raw.get("slug") or raw.get("label") or workspace.name)
        if not slug:
            raise ValueError(f"viewer catalog experiment {index} has no usable slug")
        if slug in slugs:
            raise ValueError(f"duplicate viewer catalog slug: {slug}")
        slugs.add(slug)
        entries.append(
            ViewerWorkspace(
                slug=slug,
                workspace=workspace,
                label=str(raw.get("label") or workspace.name),
                method=_optional_text(raw.get("method")),
                agent=_optional_text(raw.get("agent")),
                selection_metric=_optional_text(raw.get("selection_metric")),
            )
        )
    if not entries:
        raise ValueError("viewer catalog must contain at least one experiment")
    return tuple(entries)


def _workspace_path(raw: dict[str, Any], base: Path, index: int) -> Path:
    value = raw.get("workspace")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"viewer catalog experiment {index} is missing workspace")
    workspace = Path(value).expanduser()
    if not workspace.is_absolute():
        workspace = base / workspace
    workspace = workspace.resolve()
    required = [name for name in ("evolve.yaml", "archive.jsonl") if not (workspace / name).is_file()]
    if required:
        raise ValueError(f"viewer workspace {workspace} is missing {', '.join(required)}")
    return workspace


def _slug(value: Any) -> str:
    return _SAFE_SLUG.sub("-", str(value).lower()).strip("-")


def _optional_text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
