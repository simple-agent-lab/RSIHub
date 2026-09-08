"""Execute mutable operators against explicit public data, never host worktrees."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Resource
from .frozen.public_artifacts import OUTPUTS, SCHEMA_VERSION
from .public_feedback import archive_row, operator_feedback
from .runtime.files import make_directories, read_regular_file, read_tree, write_regular_file, write_tree
from .runtime.process import OwnedResult
from .runtime.sandbox import SandboxConfig, resolve_image, run_sandbox
from .surface import check_paths, surface_patterns


def session_sandbox(workspace: Path, timeout_s: float) -> SandboxConfig | None:
    manifest = workspace / "runs/agent-driven/controller/manifest.json"
    if not manifest.exists():
        return None
    config = json.loads(manifest.read_text()).get("sandbox")
    if config is None:
        return None
    return replace(SandboxConfig(**config), timeout_s=min(config["timeout_s"], timeout_s))


def _tree(path: Path) -> dict[str, bytes]:
    return read_tree(path) if path.exists() else {}


def _python_files(root: Resource, prefix: str = "") -> dict[str, bytes]:
    files = {}
    for child in root.iterdir():
        name = prefix + child.name
        if child.is_dir():
            files.update(_python_files(child, name + "/"))
        elif child.name.endswith(".py"):
            files[name] = child.read_bytes()
    return files


def _selected(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(root + "/") for root in roots)


def run_isolated_operator(
    *,
    name: str,
    checkout: Path,
    source: Path,
    workspace: Path,
    genid: str,
    parent: str | None,
    run_dir: Path,
    config_block: dict[str, Any],
    sandbox: SandboxConfig,
) -> OwnedResult:
    if name not in OUTPUTS:
        raise RuntimeError("unknown isolated operator stage")
    run_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"isolated-{name}-", dir=run_dir))
    inputs, output = root / "input", root / "output"
    inputs.mkdir()
    output.mkdir()
    before = {
        f"{part}/{key}": value for part in ("target", "operators") for key, value in _tree(checkout / part).items()
    }
    modes = {key: bool((checkout / key).stat().st_mode & 0o111) for key in before}
    write_tree(output / "checkout", before)
    for key, executable in modes.items():
        (output / "checkout" / key).chmod(0o755 if executable else 0o644)
    write_tree(inputs / "operators", _tree(source / "operators"))
    framework = Path(__file__).parent
    write_tree(
        inputs / "framework/evolve",
        {p.relative_to(framework).as_posix(): p.read_bytes() for p in framework.rglob("*.py")},
    )
    from .config import library_root
    from .frozen.interfaces import ArchiveView

    library = library_root()
    write_tree(inputs / "framework/library", _python_files(library))
    view = ArchiveView(workspace)
    public = {}
    if (workspace / "archive.jsonl").is_file():
        public["archive-view.json"] = json.dumps(
            {
                "rows": [archive_row(row) for row in view.rows()],
                "valid_parents": [archive_row(row) for row in view.valid_parents()],
            }
        ).encode()
    else:
        public["archive-view.json"] = b'{"rows":[],"valid_parents":[]}'
    for filename in (".evolve-protocol-version",):
        if (workspace / filename).is_file():
            public[filename] = read_regular_file(workspace, filename)
    include, exclude = surface_patterns(workspace)
    public["evolve.yaml"] = json.dumps({"surface": {"include": include, "exclude": exclude}}).encode()
    insight = workspace / "insights/playbook.jsonl"
    prior_insights = read_regular_file(workspace, "insights/playbook.jsonl") if insight.exists() else b""
    if prior_insights:
        public["insights/playbook.jsonl"] = prior_insights
    workspace_root = output if name == "reflect" else inputs
    write_tree(workspace_root / "workspace", public)
    # Only previous public operator products are observation inputs. Never copy
    # the enclosing run directory: it also contains evaluator and host receipts.
    observations = operator_feedback(run_dir)
    (root / "feedback-exposure.json").write_text(
        json.dumps(
            {
                "role": "development",
                "schema_version": SCHEMA_VERSION,
                "files": sorted(observations),
                "archive": "aggregate scores and candidate identities; no task vectors or private logs",
                "independent_holdout": False,
            }
        )
    )
    write_tree(output / "artifacts", observations)
    environment = {
        "EVOLVE_PUBLIC_ARCHIVE": "/output/workspace/archive-view.json"
        if name == "reflect"
        else "/input/workspace/archive-view.json",
        "EVOLVE_GENID": genid,
        "EVOLVE_PARENT": parent or "",
        "EVOLVE_STAGE_TIMEOUT_S": str(sandbox.timeout_s),
        "EVOLVE_OPERATOR_TIMEOUT_S": str(sandbox.timeout_s),
        "EVOLVE_WORKSPACE": "/output/workspace" if name == "reflect" else "/input/workspace",
        "EVOLVE_CHECKOUT": "/output/checkout",
        "EVOLVE_RUN_DIR": "/output/artifacts",
        "EVOLVE_HOME": "/output/home",
    }
    (inputs / "launch.py").write_text(
        "import os,sys,runpy\n"
        "sys.path[:0] = ['/input/framework', '/output/checkout']\n"
        f"os.environ.update({environment!r})\n"
        "os.chdir('/output/checkout')\n"
        f"sys.argv=['/input/operators/{name}.py','--config',{json.dumps(config_block, allow_nan=False)!r}]\n"
        f"runpy.run_path('/input/operators/{name}.py',run_name='__main__')\n"
    )
    result = run_sandbox(
        sandbox,
        image_id=resolve_image(sandbox, root),
        command=["python3", "/input/launch.py"],
        inputs=inputs,
        output=output,
    )
    (root / "result.json").write_text(json.dumps({"returncode": result.returncode, "timed_out": result.timed_out}))
    (root / "stdout").write_text(result.stdout)
    (root / "stderr").write_text(result.stderr)
    after = read_tree(output / "checkout")
    after_modes = {key: bool((output / "checkout" / key).stat().st_mode & 0o111) for key in after}
    changes = sorted(
        key
        for key in before.keys() | after.keys()
        if before.get(key) != after.get(key) or modes.get(key) != after_modes.get(key)
    )
    violations = check_paths(changes, include, exclude)
    if violations:
        raise RuntimeError("isolated operator changed forbidden paths: " + ", ".join(violations))
    artifacts = {key: data for key, data in read_tree(output / "artifacts").items() if _selected(key, OUTPUTS[name])}
    appended = None
    if name == "reflect":
        from .frozen.interfaces import validate_reflect_payload

        updated = read_regular_file(output, "workspace/insights/playbook.jsonl")
        if not updated.startswith(prior_insights):
            raise RuntimeError("reflection must append to existing insights")
        appended = updated[len(prior_insights) :]
        validate_reflect_payload({"ops": [json.loads(line) for line in appended.splitlines() if line]})
    # Validate every untrusted tree before writing any candidate or result data.
    for key in changes:
        if key not in after:
            (checkout / key).unlink()
        else:
            make_directories(checkout, str(Path(key).parent))
            write_regular_file(checkout, key, after[key])
            (checkout / key).chmod(0o755 if after_modes[key] else 0o644)
    for key, data in artifacts.items():
        make_directories(run_dir, str(Path(key).parent))
        write_regular_file(run_dir, key, data)
    if appended is not None:
        make_directories(workspace, "insights")
        write_regular_file(workspace, "insights/playbook.jsonl", prior_insights + appended)
    if name == "mutate":
        # No model or network is reachable in this operator boundary yet.
        make_directories(run_dir, "mutate")
        write_regular_file(run_dir, "mutate/usage.json", b'{"usd":0}')
    return result
