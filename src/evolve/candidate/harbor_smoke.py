"""Audit task-level Harbor artifacts emitted by detached candidate smoke runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarborTaskAudit:
    required: bool
    job_results: int
    task_results: int
    expected_trials: int
    errored_trials: int
    cancelled_trials: int
    active_trials: int
    invalid_results: int
    task_exceptions: tuple[str, ...]

    @property
    def observed(self) -> bool:
        return bool(self.job_results or self.task_results or self.invalid_results)

    @property
    def failures(self) -> tuple[str, ...]:
        failures: list[str] = []
        if self.required and not self.observed:
            failures.append("Harbor produced no task result artifacts")
        if self.invalid_results:
            failures.append(f"{self.invalid_results} Harbor result artifact(s) were unreadable")
        if self.task_exceptions:
            failures.append(
                f"{len(self.task_exceptions)} Harbor task trial(s) reported exceptions: "
                + "; ".join(self.task_exceptions)
            )
        if self.errored_trials > len(self.task_exceptions):
            failures.append(f"Harbor summary reported {self.errored_trials} errored trial(s)")
        if self.cancelled_trials:
            failures.append(f"Harbor summary reported {self.cancelled_trials} cancelled trial(s)")
        if self.active_trials:
            failures.append(f"Harbor summary left {self.active_trials} trial(s) pending or running")
        if self.expected_trials > self.task_results:
            failures.append(f"Harbor produced {self.task_results} task result(s), expected {self.expected_trials}")
        return tuple(failures)

    def payload(self) -> dict[str, object]:
        return {
            "status": "failed" if self.failures else "passed",
            "required": self.required,
            "job_results": self.job_results,
            "task_results": self.task_results,
            "expected_trials": self.expected_trials,
            "errored_trials": self.errored_trials,
            "cancelled_trials": self.cancelled_trials,
            "active_trials": self.active_trials,
            "invalid_results": self.invalid_results,
            "task_exception_count": len(self.task_exceptions),
        }


def audit_harbor_results(attempt: Path, *, required: bool) -> HarborTaskAudit:
    job_results = task_results = expected_trials = 0
    errored_trials = cancelled_trials = active_trials = invalid_results = 0
    task_exceptions: list[str] = []
    for path in sorted((attempt / "jobs").glob("**/result.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            invalid_results += 1
            continue
        if not isinstance(payload, dict):
            invalid_results += 1
            continue
        if "task_name" in payload and "exception_info" in payload:
            task_results += 1
            if payload["exception_info"]:
                task_exceptions.append(_exception_summary(payload["exception_info"]))
            continue
        if "n_total_trials" not in payload:
            continue
        job_results += 1
        expected_trials += _nonnegative_int(payload.get("n_total_trials"))
        stats = payload.get("stats")
        if not isinstance(stats, dict):
            continue
        errored_trials += _nonnegative_int(stats.get("n_errored_trials"))
        cancelled_trials += _nonnegative_int(stats.get("n_cancelled_trials"))
        active_trials += _nonnegative_int(stats.get("n_pending_trials"))
        active_trials += _nonnegative_int(stats.get("n_running_trials"))
    return HarborTaskAudit(
        required,
        job_results,
        task_results,
        expected_trials,
        errored_trials,
        cancelled_trials,
        active_trials,
        invalid_results,
        tuple(task_exceptions),
    )


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _exception_summary(value: object) -> str:
    if not isinstance(value, dict):
        return str(value).splitlines()[0][:500]
    exception_type = str(value.get("exception_type") or "task exception")
    message = str(value.get("exception_message") or "").splitlines()[0]
    return f"{exception_type}: {message}"[:500].rstrip(": ")
