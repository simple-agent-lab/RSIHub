from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def benchmark_label(config: Mapping[str, Any], *, explicit: str | None = None) -> str | None:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    evaluator = config.get("evaluator")
    if not isinstance(evaluator, Mapping):
        return None
    value = evaluator.get("benchmark") or evaluator.get("dataset_name") or evaluator.get("dataset")
    return format_benchmark(value)


def format_benchmark(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    source = value.strip().rstrip("/\\")
    name = re.split(r"[/\\]", source)[-1]
    lowered = name.lower()
    if re.search(r"terminal[-_ ]bench[-_ ]?(?:2(?:\.0)?)", lowered):
        return "Terminal Bench 2"
    if re.search(r"tau(?:\^?3|[-_ ]3).*bank", lowered):
        return "Tau3 Banking"
    name = re.sub(r"[-_]\d+(?:[-_]\d+){2}$", "", name)
    words = [word for word in re.split(r"[-_ ]+", name) if word]
    return " ".join(word.upper() if word.lower() in {"api", "swe"} else word.capitalize() for word in words) or None
