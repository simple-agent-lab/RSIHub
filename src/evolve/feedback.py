"""The feedback bundle the mutation operator reads — mechanism-owned assembly.

Folded out of the retired `observe` operator (DESIGN §7: the canonical verb set
is select/rollout/analyze/mutate/…/gate/record). The bundle is derived
from the archive + workspace, plus bounded evidence emitted by analyze. The driver calls
`write_feedback_bundle` after analyze and before mutate, which reads
`runs/gen-<id>/feedback/`. It therefore exists even when rollout is a no-op
operator. This is the one home for the logic — `library/observe/*` is deleted.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .archive import RECEIPT_CERTIFIED_FIELD, archive_path, merged_rows
from .evaluation.diagnostics import DiagnosticsValidationError, validate_evaluation_diagnostics_payload
from .git import git_stdout, tag_exists
from .surface import surface_patterns

Row = dict[str, Any]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _lineage(rows: list[Row]) -> list[Row]:
    return [
        {
            "genid": row.get("genid"),
            "parent": row.get("parent"),
            "score": row.get("score"),
            "status": row.get("status"),
            "valid_parent": row.get("valid_parent"),
        }
        for row in rows
    ]


def _latest_accepted_diff(workspace: Path, rows: list[Row]) -> str:
    for candidate in reversed(rows):
        parent = candidate.get("parent")
        tag = candidate.get("tag")
        if candidate.get("valid_parent") is not True or not parent or not tag:
            continue
        if not (tag_exists(workspace, str(tag)) and tag_exists(workspace, f"gen/{parent}")):
            continue
        diff = git_stdout(workspace, "diff", f"gen/{parent}", str(tag))
        return diff + ("" if not diff or diff.endswith("\n") else "\n")
    return ""


def _surface_rule_lists(workspace: Path) -> tuple[list[str], list[str]]:
    try:
        return surface_patterns(workspace)
    except Exception:
        return ["target/**"], []


def _copy_trace_feedback(run_dir: Path, failures: Path) -> str | None:
    for source in (run_dir / "analyze" / "feedback.md", run_dir / "rollout" / "feedback.md"):
        if source.is_file():
            destination = failures / "analyze.md"
            destination.write_text(source.read_text())
            return "feedback/failures/analyze.md"
    return None


def _copy_trace_evidence(run_dir: Path, destination: Path) -> list[str]:
    source = run_dir / "analyze" / "evidence"
    if not source.is_dir():
        source = run_dir / "rollout" / "evidence"
    if not source.is_dir():
        return []
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in (
        "manifest.json",
        "selected.md",
        "trajectory_only.json",
        "metrics.json",
        "failure_patterns.json",
        "passing_behaviors.json",
        "diagnosis.json",
        "reflective_dataset.json",
        "artifact_manifest.json",
        "rubric_failures.json",
    ):
        path = source / name
        if not path.is_file():
            continue
        target = destination / name
        shutil.copyfile(path, target)
        copied.append(f"feedback/evidence/{name}")
    return copied


def _copy_harbor_state(run_dir: Path, destination: Path) -> str | None:
    source = run_dir / "rollout" / "harbor-state.json"
    try:
        payload = json.loads(source.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    provenance = payload.get("source") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(provenance, dict)
        or provenance.get("kind") != "harbor_rollout"
        or provenance.get("role") != "train"
    ):
        return None
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination / "harbor-state.json")
    return "feedback/evidence/harbor-state.json"


def _rollout_history(workspace: Path, rows: list[Row], history_k: int) -> list[Row]:
    history: list[Row] = []
    for row in rows[-int(history_k) :]:
        genid = str(row.get("genid") or "")
        evidence_root = workspace / "runs" / f"gen-{genid}" / "analyze" / "evidence"
        if not evidence_root.is_dir():
            evidence_root = workspace / "runs" / f"gen-{genid}" / "rollout" / "evidence"
        manifest: dict[str, Any] = {}
        metrics: dict[str, Any] = {}
        for path, target in ((evidence_root / "manifest.json", manifest), (evidence_root / "metrics.json", metrics)):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                target.update(payload)
        history.append(
            {
                "genid": row.get("genid"),
                "parent": row.get("parent"),
                "score": row.get("score"),
                "status": row.get("status"),
                "valid_parent": row.get("valid_parent"),
                "verdict": row.get("verdict"),
                "reason": row.get("reason"),
                "mutated": row.get("mutated"),
                "analyze_operator": manifest.get("analyze_operator"),
                "rollout_metrics": metrics,
                "raw_evidence_dir": evidence_root.relative_to(workspace).as_posix() if evidence_root.is_dir() else None,
                "source_tag": row.get("tag"),
            }
        )
    return history


def _evaluation_diagnostics(rows: list[Row], history_k: int) -> list[Row]:
    diagnostics = []
    for row in rows[-int(history_k) :]:
        if row.get("diagnostics") is None:
            continue
        try:
            payload = validate_evaluation_diagnostics_payload(row["diagnostics"])
        except DiagnosticsValidationError:
            continue
        diagnostics.append(
            {
                "genid": str(row.get("genid")),
                "diagnostics": payload,
                "receipt_certified": row.get(RECEIPT_CERTIFIED_FIELD) is True,
            }
        )
    return diagnostics


def write_feedback_bundle(*, workspace: Path, run_dir: Path, history_k: int = 8) -> list[str]:
    """Write the feedback bundle under run_dir/feedback/ and return its manifest.

    Rows come from the archive (the bundle is archive-derived); the driver only
    passes the workspace and the persistent run_dir.
    """
    rows = merged_rows(archive_path(workspace))
    feedback = run_dir / "feedback"
    feedback.mkdir(parents=True, exist_ok=True)
    _write_json(feedback / "lineage.json", _lineage(rows))

    attempts = ["# Attempts", ""]
    attempts += [
        "- gen %s: status=%s score=%s valid_parent=%s reason=%s"
        % (row.get("genid"), row.get("status"), row.get("score"), row.get("valid_parent"), row.get("reason"))
        for row in rows[-int(history_k) :]
    ]
    attempts.append("")
    (feedback / "attempts.md").write_text("\n".join(attempts))

    failures = feedback / "failures"
    failures.mkdir(exist_ok=True)
    (failures / "README.md").write_text("The feedback bundle writes a minimal failure summary.\n")
    trace_feedback = _copy_trace_feedback(run_dir, failures)
    evidence_files = _copy_trace_evidence(run_dir, feedback / "evidence")
    if harbor_state := _copy_harbor_state(run_dir, feedback / "evidence"):
        evidence_files.append(harbor_state)
    _write_json(feedback / "evidence" / "history.json", _rollout_history(workspace, rows, history_k))
    evidence_files.append("feedback/evidence/history.json")
    _write_json(feedback / "evaluation_diagnostics.json", _evaluation_diagnostics(rows, history_k))
    (feedback / "last_accepted.diff").write_text(_latest_accepted_diff(workspace, rows))

    include, exclude = _surface_rule_lists(workspace)
    (feedback / "rules.md").write_text(
        "# Rules\n\n- Surface include: %s\n- Surface exclude: %s\n- Self-check: `evolve surface-check`\n"
        % (include, exclude)
    )
    has_selected_evidence = "feedback/evidence/selected.md" in evidence_files
    trace_link = (
        "- [current trace analysis](failures/analyze.md)\n" if trace_feedback and not has_selected_evidence else ""
    )
    evidence_link = "- [selected trace evidence](evidence/selected.md)\n" if has_selected_evidence else ""
    harbor_state_link = (
        "- [raw Harbor runtime state](evidence/harbor-state.json)\n"
        if "feedback/evidence/harbor-state.json" in evidence_files
        else ""
    )
    history_link = "- [rollout and edit history](evidence/history.json)\n"
    (feedback / "index.md").write_text(
        "# Feedback Bundle\n\n"
        "- [lineage](lineage.json)\n"
        "- [attempts](attempts.md)\n"
        "- [failures](failures/)\n"
        f"{trace_link}"
        f"{evidence_link}"
        f"{harbor_state_link}"
        f"{history_link}"
        "- [evaluation diagnostics](evaluation_diagnostics.json)\n"
        "- [last accepted diff](last_accepted.diff)\n"
        "- [rules](rules.md)\n"
    )
    manifest = [
        "feedback/lineage.json",
        "feedback/index.md",
        "feedback/attempts.md",
        "feedback/failures/README.md",
        "feedback/evaluation_diagnostics.json",
        "feedback/last_accepted.diff",
        "feedback/rules.md",
    ]
    if trace_feedback:
        manifest.append(trace_feedback)
    manifest.extend(evidence_files)
    _write_json(run_dir / "feedback" / "manifest.json", manifest)
    return manifest
