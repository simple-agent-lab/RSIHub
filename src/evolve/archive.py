from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .evaluation import CANONICAL_OUTCOMES, EvaluationRecord, evaluation_status

STAMPED_FIELDS = {
    "experiment_id",
    "generation",
    "candidate_commit",
    "purpose",
    "attempt",
    "retry_of",
    "evaluator_fingerprint",
    "task_set_hash",
    "contract_id",
    "evaluation_contract",
    "preflight_receipt",
    "contract_certified",
    "diagnostics",
    "runtime_fingerprint",
    "expected_trials",
    "scoreable_trials",
    "outcome",
    "trials",
    "score",
    "cost_usd",
    "wall_s",
    "artifacts",
    "source_attempts",
    "repaired_tasks",
    "status",
    "selection_eligible",
    "task_set_members",
    "task_vector",
    "cost",
}
MECHANISM_EVAL_FIELD = "_evolve_mechanism_eval"
RECEIPT_CERTIFIED_FIELD = "_evolve_receipt_certified"
RECORD_ATTEMPT_FIELD = "_evolve_record_attempted"
RESERVED_AUXILIARY_FIELDS = {
    "evals",
    "kind",
    "round",
    MECHANISM_EVAL_FIELD,
    RECORD_ATTEMPT_FIELD,
}
LEGACY_WRITE_BLOCKED_FIELDS = {"predicted_fixes", "verified_fixes"}
EVALUATION_FIELDS = STAMPED_FIELDS | {
    "genid",
    "parent",
    "tag",
    "valid_parent",
    "verdict",
    "reason",
    "mutated",
    "surface_violations",
    "note",
    "kind",
    "round",
    "pending_gate_record",
    "failure_stage",
    RECEIPT_CERTIFIED_FIELD,
}
AUXILIARY_BLOCKED_FIELDS = (EVALUATION_FIELDS - {"note"}) | {"evals", MECHANISM_EVAL_FIELD}
_SAFE_EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_WORKSPACE_ID = re.compile(r"^[0-9a-f]{32}$")


def archive_path(workspace: Path) -> Path:
    return workspace / "archive.jsonl"


def mirror_path(experiment_id: str, workspace: Path | None = None) -> Path:
    experiment_dir = _experiment_mirror_dir(experiment_id)
    if workspace is None:
        return experiment_dir / "archive.jsonl"
    workspace_id = _workspace_mirror_id(workspace, create=True)
    assert workspace_id is not None
    return experiment_dir / workspace_id / "archive.jsonl"


def ensure_local_archive(workspace: Path, experiment_id: str) -> None:
    local = archive_path(workspace)
    experiment_dir = _experiment_mirror_dir(experiment_id)
    workspace_id = _workspace_mirror_id(workspace, create=False)
    if workspace_id is None and not local.exists():
        orphaned = _orphaned_mirrors(experiment_dir)
        if not orphaned:
            return
        raise RuntimeError(
            "existing mirror history cannot be safely attributed to this workspace; explicitly restore and audit "
            "both archive.jsonl and .evolve-eval-receipts.jsonl before continuing"
        )
    if workspace_id is None:
        workspace_id = _workspace_mirror_id(workspace, create=True)
        assert workspace_id is not None
    mirror = experiment_dir / workspace_id / "archive.jsonl"
    if not local.exists() and not mirror.exists():
        return
    events: list[str] = []
    seen: set[str] = set()
    for path in (local, mirror):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip() or line in seen:
                continue
            events.append(line)
            seen.add(line)
    text = "\n".join(events) + ("\n" if events else "")
    for path in (local, mirror):
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, text)
    _ensure_receipts(local, mirror)


def _atomic_write_text(target: Path, text: str) -> None:
    # A crash mid-rewrite must never truncate the archive of record; write to
    # a sibling temporary file and rename over the target instead.
    with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=f".{target.name}-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _experiment_mirror_dir(experiment_id: str) -> Path:
    evolve_home = Path(os.environ.get("EVOLVE_HOME", Path.home() / ".evolve"))
    return evolve_home / "mirrors" / _safe_experiment_dir(experiment_id)


def _workspace_mirror_id(workspace: Path, *, create: bool) -> str | None:
    marker = _workspace_id_path(workspace)
    if not marker.exists():
        if not create:
            return None
        marker.parent.mkdir(parents=True, exist_ok=True)
        candidate = uuid.uuid4().hex
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(f"{candidate}\n")
    try:
        workspace_id = marker.read_text().strip()
    except OSError as error:
        raise RuntimeError(f"cannot read persistent workspace mirror identity: {marker}") from error
    if _WORKSPACE_ID.fullmatch(workspace_id) is None:
        raise RuntimeError(f"invalid persistent workspace mirror identity: {marker}")
    return workspace_id


def _workspace_id_path(workspace: Path) -> Path:
    executable = shutil.which("git")
    if executable is not None:
        result = subprocess.run(
            [executable, "-C", str(workspace), "rev-parse", "--git-common-dir"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            common = Path(result.stdout.strip())
            if not common.is_absolute():
                common = workspace / common
            return common.resolve() / "evolve-workspace-id"
    return workspace.resolve() / ".evolve-workspace-id"


def _orphaned_mirrors(experiment_dir: Path) -> list[Path]:
    if not experiment_dir.is_dir():
        return []
    candidates = [experiment_dir / "archive.jsonl", *experiment_dir.glob("*/archive.jsonl")]
    return sorted(path for path in candidates if path.is_file())


def append_event(workspace: Path, experiment_id: str, event: dict[str, Any]) -> None:
    line = json.dumps(event, sort_keys=True) + "\n"
    targets = (archive_path(workspace), mirror_path(experiment_id, workspace))
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as archive:
            archive.write(line)
    if event.get(MECHANISM_EVAL_FIELD) is True:
        for target in targets:
            _append_eval_receipt(target, event)


def append_evaluation_record(
    workspace: Path, record: EvaluationRecord, *, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    pending_gate = (metadata or {}).get("pending_gate_record") is True
    valid_parent = record.selection_eligible and not pending_gate
    tasks: dict[str, dict[str, list[dict[str, object]]]] = {}
    for trial in record.trials:
        raw = asdict(trial)
        task_id = str(raw.pop("task_id"))
        raw["status"] = raw.pop("outcome").value
        for field in ("source_attempt", "repaired_from_attempt", "repair_reason", "failure_category"):
            if raw[field] is None:
                raw.pop(field)
        tasks.setdefault(task_id, {"trials": []})["trials"].append(raw)
    event = {
        **(metadata or {}),
        **record.to_dict(),
        "event_type": "evaluation",
        "genid": record.generation,
        "tag": f"gen/{record.generation}",
        "status": record.status,
        "selection_eligible": record.selection_eligible,
        "pending_gate_record": pending_gate,
        "task_set_members": sorted(tasks),
        "task_vector": {"schema_version": 1, "tasks": tasks},
        "valid_parent": valid_parent,
        "verdict": "keep" if valid_parent else "discard",
        "cost": {"usd": record.cost_usd, "wall_s": record.wall_s},
        MECHANISM_EVAL_FIELD: True,
    }
    append_event(workspace, record.experiment_id, event)
    return event


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def eval_receipt_path(archive: Path) -> Path:
    return archive.with_name(".evolve-eval-receipts.jsonl")


def merged_rows(path: Path) -> list[dict[str, Any]]:
    return merge_events(read_events(path), receipts=_eval_receipts(path))


def merge_events(events: Iterable[dict[str, Any]], *, receipts: set[str] | None = None) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    evals_by_genid: dict[str, dict[str, dict[str, Any]]] = {}
    top_eval_hash: dict[str, str] = {}
    receipts = receipts or set()
    order: list[str] = []
    for raw_event in events:
        event = {key: value for key, value in raw_event.items() if key != RECEIPT_CERTIFIED_FIELD}
        genid = str(event["genid"])
        if genid not in rows:
            rows[genid] = {}
            evals_by_genid[genid] = {}
            order.append(genid)
        if _is_keyed_evaluation(event):
            certified = _has_evaluation_provenance(raw_event, genid, receipts)
            if certified:
                event[RECEIPT_CERTIFIED_FIELD] = True
            if event.get("kind") == "anchor":
                _merge_auxiliary_evaluation(rows[genid], evals_by_genid[genid], event, prefix="anchor")
                continue
            if event.get("kind") == "genesis_eval" and genid != "0":
                _merge_auxiliary_evaluation(rows[genid], evals_by_genid[genid], event, prefix="genesis")
                continue
            if genid in top_eval_hash and not certified:
                _merge_auxiliary_non_stamped_fields(rows[genid], event)
                continue
            _merge_keyed_evaluation(rows[genid], evals_by_genid[genid], top_eval_hash, genid, event)
            continue
        _merge_event_fields(rows[genid], rows[genid], event)
    return [rows[genid] for genid in order]


def rows_by_genid(workspace: Path) -> dict[str, dict[str, Any]]:
    return {str(row["genid"]): row for row in merged_rows(archive_path(workspace))}


def _safe_experiment_dir(experiment_id: str) -> str:
    if _SAFE_EXPERIMENT_ID.fullmatch(experiment_id) and experiment_id not in {".", ".."} and ".." not in experiment_id:
        return experiment_id
    return f"unsafe-{hashlib.sha256(experiment_id.encode('utf-8')).hexdigest()[:16]}"


def _is_keyed_evaluation(event: dict[str, Any]) -> bool:
    return event.get("task_set_hash") is not None and bool(STAMPED_FIELDS & set(event))


def _has_evaluation_provenance(event: dict[str, Any], genid: str, receipts: set[str]) -> bool:
    return (
        event.get(MECHANISM_EVAL_FIELD) is True
        and _eval_receipt(event) in receipts
        and event.get("tag") == f"gen/{genid}"
        and isinstance(event.get("valid_parent"), bool)
        and event.get("verdict") in {"keep", "discard"}
        and isinstance(event.get("reason"), str)
        and isinstance(event.get("cost"), dict)
    )


def _eval_receipt(event: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()


def _eval_receipts(archive: Path) -> set[str]:
    path = eval_receipt_path(archive)
    return {line.strip() for line in path.read_text().splitlines() if line.strip()} if path.exists() else set()


def verify_integrity(workspace: Path) -> list[str]:
    """Report frozen mechanism-eval events without matching receipts."""
    path = archive_path(workspace)
    receipts = _eval_receipts(path)
    findings: list[str] = []
    for event in read_events(path):
        if event.get(MECHANISM_EVAL_FIELD) is True and _eval_receipt(event) not in receipts:
            findings.append(
                f"gen {event.get('genid')} round {event.get('round')}: mechanism-eval "
                "carries no matching receipt — the archive was hand-edited"
            )
    return findings


def _append_eval_receipt(archive: Path, event: dict[str, Any]) -> None:
    path = eval_receipt_path(archive)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as receipts:
        receipts.write(_eval_receipt(event) + "\n")


def _ensure_receipts(local: Path, mirror: Path) -> None:
    receipts = sorted(_eval_receipts(local) | _eval_receipts(mirror))
    if not receipts:
        return
    for path in (eval_receipt_path(local), eval_receipt_path(mirror)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(receipts) + ("\n" if receipts else ""))


def _merge_keyed_evaluation(
    row: dict[str, Any],
    evals: dict[str, dict[str, Any]],
    top_eval_hash: dict[str, str],
    genid: str,
    event: dict[str, Any],
) -> None:
    task_hash = str(event["task_set_hash"])
    if _is_genesis_replacement(row, genid, event):
        top_eval_hash[genid] = task_hash
        _replace_top_evaluation(row, event)
        return
    if genid not in top_eval_hash:
        top_eval_hash[genid] = task_hash
        _merge_event_fields(row, row, event)
        return

    if task_hash == top_eval_hash[genid]:
        if event.get(RECEIPT_CERTIFIED_FIELD) is True and row.get(RECEIPT_CERTIFIED_FIELD) is not True:
            retained = {key: row[key] for key in ("evals", "note") if key in row and key not in event}
            row.clear()
            row.update(retained)
            _replace_top_evaluation(row, event)
        else:
            _merge_event_fields(row, row, event)
        return

    current = evals.get(task_hash)
    if current is None:
        evals[task_hash] = _evaluation_entry(event)
        row["evals"] = list(evals.values())
        _merge_auxiliary_non_stamped_fields(row, event)
        return

    replace_stamped = _can_replace_stamped(current, event)
    for key, value in _evaluation_entry(event).items():
        if key in STAMPED_FIELDS and key in current and not replace_stamped:
            continue
        current[key] = value
    _merge_auxiliary_non_stamped_fields(row, event)
    row["evals"] = list(evals.values())


def _merge_auxiliary_evaluation(
    row: dict[str, Any], evals: dict[str, dict[str, Any]], event: dict[str, Any], *, prefix: str
) -> None:
    if "genid" not in row:
        row["genid"] = event["genid"]
    key = f"{prefix}:{event['task_set_hash']}"
    evals[key] = _evaluation_entry(event)
    row["evals"] = list(evals.values())


def _evaluation_entry(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key in EVALUATION_FIELDS}


def _is_genesis_replacement(row: dict[str, Any], genid: str, event: dict[str, Any]) -> bool:
    return (
        genid == "0"
        and evaluation_status(row) == "pending"
        and row.get("score") is None
        and row.get("valid_parent") is False
        and event.get("kind") == "genesis_eval"
        and event.get(MECHANISM_EVAL_FIELD) is True
    )


def _replace_top_evaluation(row: dict[str, Any], event: dict[str, Any]) -> None:
    for key, value in event.items():
        if key not in RESERVED_AUXILIARY_FIELDS:
            row[key] = value


def _merge_event_fields(row: dict[str, Any], current: dict[str, Any], event: dict[str, Any]) -> None:
    replace_stamped = _can_replace_stamped(current, event)
    terminal_override = current.get("pending_gate_record") is True and event.get("status") == "operator_failed"
    for key, value in event.items():
        if (
            key == "valid_parent"
            and value is True
            and "selection_eligible" in row
            and row.get("selection_eligible") is not True
        ):
            continue
        protected = key in STAMPED_FIELDS and key in row and not replace_stamped
        if terminal_override and key in {"status", "score", "cost"}:
            protected = False
        if key not in RESERVED_AUXILIARY_FIELDS and not protected:
            row[key] = value


def _merge_auxiliary_non_stamped_fields(row: dict[str, Any], event: dict[str, Any]) -> None:
    for key, value in event.items():
        if key not in STAMPED_FIELDS and key not in AUXILIARY_BLOCKED_FIELDS:
            row[key] = value


def _can_replace_stamped(current: dict[str, Any], event: dict[str, Any]) -> bool:
    if event.get("kind") == "baseline" and event.get(MECHANISM_EVAL_FIELD) is True:
        return True
    if (
        (
            current.get("note") in {"initial scaffold", "mechanism evaluation recorded before gate/record"}
            or current.get("pending_gate_record") is True
        )
        and event.get(MECHANISM_EVAL_FIELD) is True
        and event.get("genid") == current.get("genid")
        and event.get("tag") == current.get("tag")
        and event.get("outcome") == "benchmark_complete"
        and event.get("selection_eligible") is True
        and event.get("score") is not None
        and event.get("valid_parent") is True
    ):
        return True
    return (
        evaluation_status(current) in {"infra_failed", "infrastructure_failed"}
        and event.get(MECHANISM_EVAL_FIELD) is True
        and event.get("outcome") in CANONICAL_OUTCOMES
        and all(event.get(key) == current.get(key) for key in ("generation", "candidate_commit", "purpose"))
        and all(isinstance(values.get("attempt"), int) for values in (current, event))
        and event["attempt"] > current["attempt"]
    )
