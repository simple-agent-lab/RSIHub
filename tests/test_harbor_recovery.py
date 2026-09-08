from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolve.cli import app
from evolve.integrations.harbor._recovery import resume_job


@pytest.fixture
def recovery(tmp_path):
    job = tmp_path / "jobs" / "example"
    job.mkdir(parents=True)
    config = json.dumps({"jobs_dir": str(job.parent), "job_name": job.name}).encode()
    (job / "config.json").write_bytes(config)
    trial = job / "trial"
    trial.mkdir()
    (trial / "result.json").write_text('{"exception_info":{"exception_type":"CancelledError"}}')
    status = tmp_path / "harbor.status.json"
    status.write_text('{"status":"cancelled"}')
    script = tmp_path / "resume.py"
    script.write_text(
        "import pathlib, shutil, sys\n"
        "job = pathlib.Path(sys.argv[sys.argv.index('--job-path') + 1])\n"
        "backup = next(job.parent.glob('.*-recovery/attempt-*/before-resume'))\n"
        "assert (backup / 'trial/result.json').read_bytes() == (job / 'trial/result.json').read_bytes()\n"
        "shutil.rmtree(job / 'trial')\n"
        "(job / 'result.json').write_text('{}')\n"
    )
    return job, {
        "config_sha256": hashlib.sha256(config).hexdigest(),
        "owner_status": status,
        "harbor_command": [sys.executable, str(script)],
        "cwd": tmp_path,
        "error_types": ["CancelledError"],
        "timeout_s": 5,
    }


def test_backup_precedes_destructive_resume(recovery):
    job, kwargs = recovery
    receipt = resume_job(job, **kwargs)
    assert receipt["status"] == "completed"
    assert not (job / "trial").exists()
    assert (Path(receipt["backup"]) / "trial/result.json").is_file()
    assert Path(receipt["receipt"]).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("mismatch", ["digest", "owner", "path", "symlink"])
def test_refuses_unsafe_recovery_before_touching_evidence(recovery, mismatch):
    job, kwargs = recovery
    if mismatch == "digest":
        kwargs["config_sha256"] = "0" * 64
    elif mismatch == "owner":
        kwargs["owner_status"].write_text('{"status":"running"}')
    elif mismatch == "path":
        config = json.dumps({"jobs_dir": str(job.parent), "job_name": "other"}).encode()
        (job / "config.json").write_bytes(config)
        kwargs["config_sha256"] = hashlib.sha256(config).hexdigest()
    else:
        (job / "linked").symlink_to(kwargs["owner_status"])
    with pytest.raises(ValueError):
        resume_job(job, **kwargs)
    assert (job / "trial/result.json").is_file()
    assert not list(job.parent.glob(".*-recovery/attempt-*"))


def test_failed_process_keeps_original_backup(recovery):
    job, kwargs = recovery
    kwargs["harbor_command"] = [sys.executable, "-c", "raise SystemExit(9)"]
    result = resume_job(job, **kwargs)
    assert result["status"] == "failed"
    assert result["returncode"] == 9
    assert (Path(result["backup"]) / "trial/result.json").is_file()


def test_cli_recovery_requires_explicit_inputs():
    result = CliRunner().invoke(app, ["harbor-resume", "/tmp/example"])
    assert result.exit_code != 0
    assert "config-sha256" in result.output


def test_unresolved_recovery_blocks_duplicate_process(recovery):
    job, kwargs = recovery
    attempt = job.parent / f".{job.name}-recovery/attempt-1"
    attempt.mkdir(parents=True)
    (attempt / "receipt.json").write_text('{"status":"running"}')
    with pytest.raises(RuntimeError, match="unresolved"):
        resume_job(job, **kwargs)
    assert (job / "trial/result.json").is_file()
