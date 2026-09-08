"""Execute a verified delivery package without exposing its research repository."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..runtime.sandbox import SandboxConfig, boundary_receipt, resolve_image, run_sandbox
from .package import materialize_candidate


def run_candidate_package(
    package: Path,
    *,
    digest: str,
    config: SandboxConfig,
    command: list[str],
    run_dir: Path,
) -> dict[str, Any]:
    """The host owns the receipt; candidate output is a separate untrusted tree."""
    config.validate()
    if run_dir.exists() or run_dir.is_symlink():
        raise RuntimeError("candidate execution requires a fresh run directory")
    run_dir.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="evolve-candidate-input-") as temporary:
        inputs = Path(temporary) / "target"
        manifest = materialize_candidate(package, digest, inputs)
        image_id = resolve_image(config, run_dir)
        output = run_dir / "output"
        output.mkdir()
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "status": "started",
            "package_sha256": digest,
            "candidate_commit": manifest["candidate_commit"],
            "target_tree": manifest["target_tree"],
            "command": command,
            "boundary": boundary_receipt(image_id),
        }
        receipt_path = run_dir / "execution.json"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        try:
            result = run_sandbox(config, image_id=image_id, command=command, inputs=inputs, output=output)
        except BaseException:
            receipt["status"] = "interrupted_or_cleanup_unconfirmed"
            receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
            raise
        (run_dir / "stdout.log").write_text(result.stdout)
        (run_dir / "stderr.log").write_text(result.stderr)
        receipt.update(
            status="completed" if result.returncode == 0 and not result.timed_out else "failed",
            result={k: v for k, v in asdict(result).items() if k not in {"stdout", "stderr"}},
        )
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        return receipt
