from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

StageState = Literal["not_applicable", "skipped", "waiting", "active", "complete", "not_passed", "failed", "unknown"]
HealthState = Literal["complete", "failed", "active", "waiting", "possibly_interrupted", "unknown"]


class ViewerWarning(BaseModel):
    code: str
    message: str
    scope: str


class ArtifactReference(BaseModel):
    id: str
    label: str
    relative_path: str
    kind: str
    size: int
    previewable: bool


class ArtifactPreviewMetadata(ArtifactReference):
    truncated: bool
    content_url: str


class StageSummary(BaseModel):
    name: str
    state: StageState
    progress_completed: int | None = None
    progress_total: int | None = None
    exact_wall_s: float | None = None
    artifact_ids: list[str] = Field(default_factory=list)


class ChangeSummary(BaseModel):
    rationale: str | None = None
    changed_paths: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    patch_artifact_id: str | None = None


class PerformanceSummary(BaseModel):
    score: float | None = None
    sealed_score: float | None = None
    parent_score: float | None = None
    delta: float | None = None
    comparable: bool = False
    task_set_hash: str | None = None
    expected_trials: int | None = None
    observed_trials: int | None = None
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    contract_certified: bool | None = None
    cost_usd: float | None = None
    wall_s: float | None = None
    train_score_before: float | None = None
    train_score_after: float | None = None
    train_delta: float | None = None


class TrialSummary(BaseModel):
    id: str
    generation: str
    purpose: str
    task: str
    repetition: int
    reward: float | None
    status: str
    owner: str | None = None
    failure_category: str | None = None
    duration_ms: float | None = None
    harbor_url: str | None = None
    warnings: list[ViewerWarning] = Field(default_factory=list)


class GenerationSummary(BaseModel):
    genid: str
    parent: str | None = None
    status: str
    current_stage: str | None = None
    score: float | None = None
    selection_eligible: bool | None = None
    change_files: int = 0
    insertions: int = 0
    deletions: int = 0
    warnings: list[ViewerWarning] = Field(default_factory=list)


class GenerationDetail(BaseModel):
    summary: GenerationSummary
    stages: list[StageSummary] = Field(default_factory=list)
    change: ChangeSummary = Field(default_factory=ChangeSummary)
    performance: PerformanceSummary = Field(default_factory=PerformanceSummary)
    artifacts: list[ArtifactReference] = Field(default_factory=list)


class ExperimentSummary(BaseModel):
    id: str
    workspace: str
    recipe: str | None = None
    health: HealthState
    focus_generation: str | None = None
    current_stage: str | None = None
    best_score: float | None = None
    updated_at: datetime
    last_activity_at: datetime | None = None
    warnings: list[ViewerWarning] = Field(default_factory=list)


class ViewerSnapshot(BaseModel):
    experiment: ExperimentSummary
    generations: list[GenerationSummary] = Field(default_factory=list)
    trial_count: int = 0


class PaginatedTrials(BaseModel):
    items: list[TrialSummary]
    total: int
    page: int
    page_size: int
    total_pages: int


@dataclass(frozen=True)
class SourceDocument:
    relative_path: str
    path: Path
    size: int
    mtime_ns: int
    value: Any | None
    error: str | None = None


@dataclass(frozen=True)
class JobRootReference:
    generation: str
    purpose: str
    path: Path


@dataclass(frozen=True)
class WorkspaceSources:
    workspace: Path
    config: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    rows: tuple[dict[str, Any], ...]
    documents: dict[str, SourceDocument]
    job_roots: tuple[JobRootReference, ...]
    warnings: tuple[ViewerWarning, ...]
    refreshed_at: datetime


@dataclass(frozen=True)
class ArtifactTarget:
    path: Path
    media_type: str
    size: int


@dataclass(frozen=True)
class HarborTrialLink:
    url: str
    reward: float | None = None
    duration_ms: float | None = None


@dataclass(frozen=True)
class SnapshotBundle:
    snapshot: ViewerSnapshot
    generation_details: dict[str, GenerationDetail]
    trials: tuple[TrialSummary, ...]
    artifact_targets: dict[str, ArtifactTarget]
    artifact_references: dict[str, ArtifactReference]
