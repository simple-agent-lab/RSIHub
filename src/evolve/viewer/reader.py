from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..archive import eval_receipt_path, merge_events
from ..config import load_config
from .models import JobRootReference, SourceDocument, ViewerWarning, WorkspaceSources

_LEGACY_ANALYZE = "trace" + "_analyzer"
_LEGACY_MUTATE = "meta" + "_agent"
_GENERATION_DOCUMENTS = (
    "select/parents.json",
    "rollout/summary.json",
    "rollout/cases.json",
    "rollout/artifacts.json",
    f"{_LEGACY_ANALYZE}/summary.json",
    f"{_LEGACY_ANALYZE}/artifacts.json",
    f"{_LEGACY_ANALYZE}/analysis/overview.md",
    f"{_LEGACY_ANALYZE}/evidence/overview.json",
    "analyze/summary.json",
    "analyze/artifacts.json",
    "analyze/analysis/overview.md",
    "analyze/evidence/overview.json",
    f"{_LEGACY_MUTATE}/change_manifest.json",
    f"{_LEGACY_MUTATE}/changed.json",
    f"{_LEGACY_MUTATE}/patch.diff",
    f"{_LEGACY_MUTATE}/model_patch.diff",
    f"{_LEGACY_MUTATE}/rationale.md",
    f"{_LEGACY_MUTATE}/usage.json",
    "mutate/change_manifest.json",
    "mutate/changed.json",
    "mutate/patch.diff",
    "mutate/model_patch.diff",
    "mutate/rationale.md",
    "mutate/usage.json",
    "validate/result.json",
    "novelty.json",
    "gate.json",
    "record/fields.json",
)
_EVALUATION_DOCUMENTS = (
    "evaluation-contract.json",
    "task_vector.json",
    "evaluation_artifacts.json",
    "diagnostics.json",
    "cost.json",
    "status",
    "score",
    "attempt-summary.json",
)


class WorkspaceReader:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self._cache: dict[Path, tuple[tuple[int, int, int], SourceDocument]] = {}

    def refresh(self) -> WorkspaceSources:
        self._validate_workspace()
        events, warnings = self._read_archive()
        documents = self._read_known_documents()
        warnings.extend(self._document_warnings(documents))
        receipts_path = eval_receipt_path(self.workspace / "archive.jsonl")
        receipts = set(receipts_path.read_text().splitlines()) if receipts_path.exists() else set()
        return WorkspaceSources(
            workspace=self.workspace,
            config=load_config(self.workspace / "evolve.yaml"),
            events=tuple(events),
            rows=tuple(merge_events(events, receipts=receipts)),
            documents=documents,
            job_roots=self._discover_job_roots(documents),
            warnings=tuple(warnings),
            refreshed_at=datetime.now(UTC),
        )

    def resolve_workspace_path(self, relative_path: str) -> Path:
        candidate = (self.workspace / relative_path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise ValueError("artifact path escapes workspace")
        return candidate

    def _validate_workspace(self) -> None:
        if not (self.workspace / "evolve.yaml").is_file():
            raise ValueError(f"not an RSIHub workspace: missing evolve.yaml in {self.workspace}")
        if not (self.workspace / "archive.jsonl").is_file():
            raise ValueError(f"not an RSIHub workspace: missing archive.jsonl in {self.workspace}")

    def _read_archive(self) -> tuple[list[dict[str, Any]], list[ViewerWarning]]:
        path = self.workspace / "archive.jsonl"
        text = path.read_text()
        lines = text.splitlines(keepends=True)
        events: list[dict[str, Any]] = []
        warnings: list[ViewerWarning] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) and not line.endswith("\n"):
                    warnings.append(
                        ViewerWarning(
                            code="archive_partial_tail",
                            message="archive append is incomplete; retrying on the next refresh",
                            scope="archive",
                        )
                    )
                    break
                raise ValueError(f"archive.jsonl line {index} is invalid: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"archive.jsonl line {index} must be an object")
            events.append(value)
        return events, warnings

    def _read_known_documents(self) -> dict[str, SourceDocument]:
        paths: set[Path] = {self.workspace / "runs/run-summary.json"}
        runs = self.workspace / "runs"
        if runs.is_dir():
            for generation_dir in runs.glob("gen-*"):
                if generation_dir.is_dir():
                    paths.update(generation_dir / relative for relative in _GENERATION_DOCUMENTS)
            for attempt_dir in runs.glob("evaluations/*/gen-*/*/attempt-*"):
                if attempt_dir.is_dir():
                    paths.update(attempt_dir / relative for relative in _EVALUATION_DOCUMENTS)
        documents: dict[str, SourceDocument] = {}
        for path in sorted(paths):
            if not path.is_file():
                continue
            document = self._read_document(path)
            documents[document.relative_path] = document
        return documents

    def _read_document(self, path: Path) -> SourceDocument:
        stat = path.stat()
        identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == identity:
            return cached[1]
        relative = path.relative_to(self.workspace).as_posix()
        try:
            text = path.read_text()
            value: Any = json.loads(text) if path.suffix == ".json" else text
            error = None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            value = None
            error = str(exc)
        document = SourceDocument(
            relative_path=relative,
            path=path,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            value=value,
            error=error,
        )
        self._cache[path] = (identity, document)
        return document

    def _document_warnings(self, documents: dict[str, SourceDocument]) -> list[ViewerWarning]:
        return [
            ViewerWarning(code="artifact_parse_failed", message=document.error or "parse failed", scope=relative)
            for relative, document in documents.items()
            if document.error is not None
        ]

    def _discover_job_roots(self, documents: dict[str, SourceDocument]) -> tuple[JobRootReference, ...]:
        references: set[JobRootReference] = set()
        runs = self.workspace / "runs"
        for jobs in runs.glob("evaluations/*/gen-*/*/attempt-*/jobs") if runs.is_dir() else ():
            parts = jobs.relative_to(runs).parts
            if jobs.is_dir() and len(parts) >= 3:
                references.add(JobRootReference(generation=parts[2].removeprefix("gen-"), purpose=parts[1], path=jobs))
        for relative, document in documents.items():
            if not relative.endswith("/rollout/summary.json") or not isinstance(document.value, dict):
                continue
            generation = Path(relative).parts[1].removeprefix("gen-")
            values = document.value.get("jobs_dirs") or [document.value.get("jobs_dir")]
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str) and (path := self._relocated_job_path(value)) is not None:
                    references.add(JobRootReference(generation=generation, purpose="rollout", path=path.resolve()))
        return tuple(sorted(references, key=lambda item: (item.generation, item.purpose, str(item.path))))

    def _relocated_job_path(self, value: str) -> Path | None:
        recorded = Path(value).expanduser()
        if recorded.is_dir():
            return recorded
        parts = recorded.parts
        if "runs" not in parts:
            return None
        runs_index = len(parts) - 1 - tuple(reversed(parts)).index("runs")
        relocated = self.workspace.joinpath(*parts[runs_index:])
        return relocated if relocated.is_dir() else None
