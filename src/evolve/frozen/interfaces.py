from __future__ import annotations

import json
import math
import os
import random
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Literal, cast

from ..archive import archive_path, ensure_local_archive, merged_rows
from ..population import fixed_evaluation_identity, is_parent_record

PROTOCOL_VERSION = 1
Row = dict[str, Any]


class PayloadValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


@dataclass(frozen=True)
class OperatorContext:
    workspace: Path
    checkout: Path
    run_dir: Path
    genid: str
    parent: str | None
    round: int | None
    fan_out: int
    config: dict[str, Any]
    rng: random.Random
    timeout_s: float = 600.0


@dataclass(frozen=True)
class ArchiveView:
    workspace: Path

    def _projection(self) -> dict[str, Any] | None:
        path = os.environ.get("EVOLVE_PUBLIC_ARCHIVE")
        if path and Path(path).parent == self.workspace:
            return json.loads(Path(path).read_text())
        return None

    def rows(self) -> list[Row]:
        projection = self._projection()
        if projection is not None:
            return projection["rows"]
        from ..config import experiment_id

        workspace = self.workspace.resolve()
        ensure_local_archive(workspace, experiment_id(workspace))
        return merged_rows(archive_path(workspace))

    def valid_parents(self) -> list[Row]:
        projection = self._projection()
        if projection is not None:
            return projection["valid_parents"]
        expected = fixed_evaluation_identity(self.workspace)
        if expected is None:
            return []
        return [row for row in self.rows() if is_parent_record(row, expected, self.workspace)]

    def best_ever(self) -> Row | None:
        candidates = [
            row
            for row in self.valid_parents()
            if isinstance(row.get("score"), (int, float)) and not isinstance(row.get("score"), bool)
        ]
        return max(candidates, key=lambda row: float(row["score"]), default=None)

    def row(self, genid: str) -> Row | None:
        for candidate in self.rows():
            if str(candidate.get("genid")) == str(genid):
                return candidate
        return None

    def matched_parent(self, child: Row | None, parent_id: str) -> bool:
        return child is not None and str(child.get("parent")) == str(parent_id)


class SelectOperator(ABC):
    @abstractmethod
    def pick(self, archive: ArchiveView, ctx) -> SelectResult: ...


class RolloutOperator(ABC):
    @abstractmethod
    def rollout(self, checkout: Path, ctx) -> RolloutResult: ...


class AnalyzeOperator(ABC):
    @abstractmethod
    def analyze(self, checkout: Path, ctx: OperatorContext) -> AnalyzeResult:
        raise NotImplementedError


class MutateOperator(ABC):
    @abstractmethod
    def mutate(self, checkout: Path, observation: str, ctx: OperatorContext) -> MutateResult:
        raise NotImplementedError


class ValidateOperator(ABC):
    @abstractmethod
    def validate(self, checkout: Path, ctx) -> ValidateResult: ...


class NoveltyOperator(ABC):
    @abstractmethod
    def assess(self, checkout: Path, ctx) -> NoveltyResult: ...


class GateOperator(ABC):
    @abstractmethod
    def decide(self, child: Row, parent: Row | None, ctx) -> GateResult: ...


class RecordOperator(ABC):
    @abstractmethod
    def annotate(self, child: Row, ctx) -> RecordResult: ...


class ReflectOperator(ABC):
    @abstractmethod
    def reflect(self, archive, ctx) -> ReflectResult: ...


@dataclass(frozen=True)
class SelectResult:
    parents: list[str]


@dataclass(frozen=True)
class RolloutResult:
    summary: dict[str, Any]
    artifacts: list[str]


@dataclass(frozen=True)
class AnalyzeResult:
    summary: dict[str, Any]
    artifacts: list[str]


@dataclass(frozen=True)
class MutateResult:
    changed: list[str]
    notes: list[str]
    usage: dict[str, Any]


@dataclass(frozen=True)
class ValidateResult:
    accept: bool
    reason: str
    artifacts: list[str]


@dataclass(frozen=True)
class NoveltyResult:
    novelty: float  # 1.0 = wholly novel, 0.0 = an exact duplicate of a prior diff
    accept: bool  # reject near-duplicate candidate edits before they are evaluated


@dataclass(frozen=True)
class GateResult:
    decision: Literal["accept", "reject"]
    reason: str


@dataclass(frozen=True)
class RecordResult:
    fields: dict[str, Any]


@dataclass(frozen=True)
class ReflectResult:
    ops: list  # playbook delta operations (full-state entries), never a rewrite


def validate_select_payload(payload: SelectResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    parents = data.get("parents")
    if not isinstance(parents, list) or not parents or not all(str(parent) for parent in parents):
        raise PayloadValidationError("parents", "parents must be a non-empty list")
    return {"parents": [str(parent) for parent in parents]}


def validate_rollout_payload(payload: RolloutResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    _require_type(data, "summary", dict, "summary must be a dict")
    _require_type(data, "artifacts", list, "artifacts must be a list")
    return {"summary": data["summary"], "artifacts": [str(artifact) for artifact in data["artifacts"]]}


def validate_analyze_payload(payload: AnalyzeResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    _require_type(data, "summary", dict, "summary must be a dict")
    _require_type(data, "artifacts", list, "artifacts must be a list")
    return {"summary": data["summary"], "artifacts": [str(artifact) for artifact in data["artifacts"]]}


def validate_mutate_payload(payload: MutateResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    _require_type(data, "changed", list, "changed must be a list")
    _require_type(data, "notes", list, "notes must be a list")
    _require_type(data, "usage", dict, "usage must be a dict")
    usage = validate_mutate_usage_payload(data["usage"])
    return {
        "changed": [str(path) for path in data["changed"]],
        "notes": [str(note) for note in data["notes"]],
        "usage": usage,
    }


def validate_validate_payload(payload: ValidateResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    _require_type(data, "accept", bool, "accept must be a boolean")
    _require_type(data, "reason", str, "reason must be a string")
    _require_type(data, "artifacts", list, "artifacts must be a list")
    return {"accept": data["accept"], "reason": data["reason"], "artifacts": [str(p) for p in data["artifacts"]]}


def validate_gate_payload(payload: GateResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    if data.get("decision") not in {"accept", "reject"}:
        raise PayloadValidationError("decision", "decision must be accept or reject")
    _require_type(data, "reason", str, "reason must be a string")
    return {"decision": data["decision"], "reason": data["reason"]}


def validate_novelty_payload(payload: NoveltyResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    novelty = data.get("novelty")
    if not isinstance(novelty, (int, float)) or isinstance(novelty, bool) or not math.isfinite(float(novelty)):
        raise PayloadValidationError("novelty", "novelty must be a finite number")
    _require_type(data, "accept", bool, "accept must be a boolean")
    return {"novelty": float(novelty), "accept": data["accept"]}


def validate_record_payload(payload: RecordResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    _require_type(data, "fields", dict, "fields must be a dict")
    return {"fields": data["fields"]}


def validate_reflect_payload(payload: ReflectResult | dict[str, Any]) -> dict[str, Any]:
    data = _payload_dict(payload)
    ops = data.get("ops")
    if not isinstance(ops, list) or not all(isinstance(op, dict) and op.get("id") for op in ops):
        raise PayloadValidationError("ops", "ops must be a list of dicts each with an id")
    return {"ops": ops}


def validate_rollout_summary_payload(payload: object) -> dict[str, Any]:
    return dict(_json_object(payload, "summary", "summary must be a JSON object"))


def validate_rollout_artifacts_payload(payload: object) -> list[str]:
    if not isinstance(payload, list):
        raise PayloadValidationError("artifacts", "artifacts must be a list")
    return [str(item) for item in payload]


def validate_mutate_usage_payload(payload: object) -> dict[str, Any]:
    data = _json_object(payload, "usage", "usage must be a JSON object")
    usd = data.get("usd")
    if usd is not None and (
        not isinstance(usd, (int, float)) or isinstance(usd, bool) or not math.isfinite(float(usd))
    ):
        raise PayloadValidationError("usd", "usd must be a finite number")
    return dict(data)


def validate_gate_file_payload(payload: object) -> dict[str, Any]:
    data = _json_object(payload, "valid_parent", "gate payload must be a JSON object")
    _require_type(data, "valid_parent", bool, "valid_parent must be a boolean")
    if data.get("verdict") not in {"keep", "discard"}:
        raise PayloadValidationError("verdict", "verdict must be keep or discard")
    if data["valid_parent"] is not (data["verdict"] == "keep"):
        raise PayloadValidationError("valid_parent", "valid_parent must agree with verdict")
    _require_type(data, "reason", str, "reason must be a string")
    return {
        "valid_parent": data["valid_parent"],
        "verdict": data["verdict"],
        "reason": data["reason"],
    }


def validate_novelty_file_payload(payload: object) -> dict[str, Any]:
    return validate_novelty_payload(_json_object(payload, "novelty", "novelty payload must be a JSON object"))


def validate_validate_file_payload(payload: object) -> dict[str, Any]:
    return validate_validate_payload(_json_object(payload, "accept", "validate payload must be a JSON object"))


def validate_record_fields_payload(payload: object) -> dict[str, Any]:
    return dict(_json_object(payload, "fields", "fields must be a JSON object"))


def _payload_dict(payload: object) -> dict[str, Any]:
    if is_dataclass(payload) and not isinstance(payload, type):
        return asdict(payload)
    if isinstance(payload, dict):
        return dict(cast("dict[str, Any]", payload))
    raise PayloadValidationError("payload", "payload must be a dataclass result or dict")


def _require_type(data: dict[str, Any], field: str, expected: type, message: str) -> None:
    if not isinstance(data.get(field), expected):
        raise PayloadValidationError(field, message)


def _json_object(payload: object, field: str, message: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PayloadValidationError(field, message)
    return cast("dict[str, Any]", payload)


# ---------------------------------------------------------------------------
# The operator registry — the single source of truth for the operator set.
# Adding an operator is ONE entry here; the kind lists in config.py and the
# contract tests derive from it, so they can't drift (mechanism 6, DESIGN §6).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorSpec:
    kind: str
    abc: type
    result: type
    method: str
    required: bool  # required: always in the loop; optional: opt-in per recipe


OPERATORS: tuple[OperatorSpec, ...] = (
    OperatorSpec("select", SelectOperator, SelectResult, "pick", True),
    OperatorSpec("rollout", RolloutOperator, RolloutResult, "rollout", True),
    OperatorSpec("analyze", AnalyzeOperator, AnalyzeResult, "analyze", False),
    OperatorSpec("mutate", MutateOperator, MutateResult, "mutate", True),
    OperatorSpec("validate", ValidateOperator, ValidateResult, "validate", False),
    OperatorSpec("novelty", NoveltyOperator, NoveltyResult, "assess", False),
    OperatorSpec("gate", GateOperator, GateResult, "decide", True),
    OperatorSpec("record", RecordOperator, RecordResult, "annotate", True),
    OperatorSpec("reflect", ReflectOperator, ReflectResult, "reflect", False),
)
OPERATOR_BY_KIND: dict[str, OperatorSpec] = {spec.kind: spec for spec in OPERATORS}
REQUIRED_OPERATOR_KINDS: tuple[str, ...] = tuple(s.kind for s in OPERATORS if s.required)
OPTIONAL_OPERATOR_KINDS: tuple[str, ...] = tuple(s.kind for s in OPERATORS if not s.required)
