"""Public controller inputs and file-only returns across the execution boundary."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from .agent_driver import SESSION_DIR, _child_path, parse_action
from .agent_optimizer import verify_optimizer
from .candidate.package import export_candidate, materialize_candidate
from .runtime.files import read_regular_file, read_tree, write_tree
from .runtime.model_broker import ModelBroker, ModelBrokerConfig
from .runtime.policy import POLICY
from .runtime.process import OwnedResult
from .runtime.sandbox import SandboxConfig, boundary_receipt, resolve_image, run_sandbox
from .surface import check_paths, surface_patterns


def run_isolated_controller(
    workspace: Path,
    state: dict[str, Any],
    attempt_dir: Path,
    sandbox: SandboxConfig,
    command: list[str],
    remaining_wall: float | None,
    *,
    broker: ModelBrokerConfig | None = None,
) -> OwnedResult:
    """No host repository or ledger is mounted into the research process."""
    if state.get("mode") != "continuous":
        raise RuntimeError("isolated controllers require a continuous research session")
    inputs, output = attempt_dir / "isolated-input", attempt_dir / "isolated-output"
    inputs.mkdir()
    output.mkdir()
    root = workspace / SESSION_DIR
    optimizer = state["research"]["active_optimizer"]
    write_tree(inputs / "optimizer", read_tree(verify_optimizer(workspace, optimizer)))
    for name, source in (
        ("notes", root / "notes"),
        ("drafts", root / "optimizer/drafts"),
        ("contexts", root / "isolated-contexts"),
    ):
        write_tree(output / name, read_tree(source) if source.exists() else {})
    for name in ("session-facts.json", "controller-input.json"):
        data = json.loads((attempt_dir / name).read_text())
        if name == "controller-input.json":
            data.update(
                optimizer_path="/input/optimizer",
                research_workspace="/output/notes",
                attempt=int(attempt_dir.name.removeprefix("attempt-")),
            )
        (inputs / name).write_text(json.dumps(data, sort_keys=True) + "\n")
    correction = root / "controller/correction.json"
    if correction.is_file():
        (inputs / "correction.json").write_bytes(correction.read_bytes())
    (inputs / "session.json").write_text(json.dumps(state, sort_keys=True) + "\n")
    from .agent_driver import _ACTION_FIELDS

    shapes = {
        k: {"required": sorted(v[0]), "optional": sorted(v[1])}
        for k, v in _ACTION_FIELDS.items()
        if k != "submit_champion"
    }
    (inputs / "actions.json").write_text(json.dumps(shapes, sort_keys=True) + "\n")
    champion = state["champion"]
    package = inputs / "champion-package"
    receipt = export_candidate(workspace, champion["candidate_commit"], package)
    materialize_candidate(package, receipt["sha256"], inputs / "champion")
    # These are data copies, not Git worktrees or mounts of mutable host state.
    children = workspace / "runs/worktrees"
    if children.exists():
        for child in sorted(children.glob("gen-*")):
            if child.is_dir() and (child / "target").is_dir():
                write_tree(inputs / "children" / child.name / "target", read_tree(child / "target"))
    image_id = resolve_image(sandbox, attempt_dir)
    config = (
        replace(sandbox, timeout_s=min(sandbox.timeout_s, remaining_wall)) if remaining_wall is not None else sandbox
    )
    (attempt_dir / "isolation.json").write_text(json.dumps(boundary_receipt(image_id, config), sort_keys=True) + "\n")
    usage: dict[str, int | float | None] = {"total_tokens": 0, "cost_usd": 0.0}
    if broker is None:
        result = run_sandbox(config, image_id=image_id, command=command, inputs=inputs, output=output)
    else:
        framework = Path(__file__).parent
        write_tree(
            inputs / "framework/evolve",
            {
                "__init__.py": b"",
                "runtime/__init__.py": b"",
                "runtime/policy.py": (framework / "runtime/policy.py").read_bytes(),
                "runtime/files.py": (framework / "runtime/files.py").read_bytes(),
                "runtime/model_bridge.py": (framework / "runtime/model_bridge.py").read_bytes(),
            },
        )
        (inputs / "model-client.py").write_text(
            "import sys\nsys.path.insert(0, '/input/framework')\nfrom evolve.runtime.model_bridge import main\nmain()\n"
        )
        command = [
            "python3",
            "/input/model-client.py",
            "--timeout",
            str(broker.timeout_s + POLICY.model_transport_grace_s),
            "--",
            *command,
        ]
        (inputs / "model-broker.json").write_text(
            json.dumps({"requests": "/output/broker/requests", "responses": "/output/broker/responses"}) + "\n"
        )
        with ModelBroker(broker, output, attempt_dir / "model-requests") as service:
            try:
                result = run_sandbox(config, image_id=image_id, command=command, inputs=inputs, output=output)
            finally:
                service.stop.set()
        usage = service.usage()
        if service.failure:
            result = replace(result, returncode=1, stderr="model broker failed; inspect host request receipts")
    # Files are untrusted; never follow a container-created symlink to host data.
    for name in ("method-load.json",):
        try:
            data = read_regular_file(output, name)
        except FileNotFoundError:
            continue
        (attempt_dir / name).write_bytes(data)
    # Billing comes only from the host broker; absent a broker, no model is reachable.
    (attempt_dir / "usage.json").write_text(json.dumps(usage) + "\n")
    return result


def accept_isolated_return(workspace: Path, attempt_dir: Path) -> None:
    """Accept only declared data edits and a typed request, after launch validation."""
    output = attempt_dir / "isolated-output"
    encoded = read_regular_file(output, "action.json")
    try:
        action = parse_action(json.loads(encoded, parse_constant=_reject_constant))
    except (RuntimeError, ValueError) as exc:
        from .agent_research import record_rejection

        record_rejection(workspace, str(exc), attempt=int(attempt_dir.name.removeprefix("attempt-")))
        return
    root = workspace / SESSION_DIR
    transfers = {
        root / "notes": read_tree(output / "notes"),
        root / "optimizer/drafts": read_tree(output / "drafts"),
        root / "isolated-contexts": read_tree(output / "contexts"),
    }
    patch_file = output / "edits.json"
    patches = (
        json.loads(read_regular_file(output, "edits.json")) if patch_file.exists() or patch_file.is_symlink() else {}
    )
    if not isinstance(patches, dict):
        raise RuntimeError("isolated edits must be a file map")
    if patches:
        if action.kind not in {"checkpoint", "operator", "commit"} or "genid" not in action.arguments:
            raise RuntimeError("isolated target edits require a managed child action")
        child = _child_path(workspace, action.arguments["genid"])
        if not child.is_dir() or child.is_symlink():
            raise RuntimeError("isolated edits require an existing managed child")
        changed = {}
        for name, entry in patches.items():
            parts = PurePosixPath(name).parts
            if (
                not name.startswith("target/")
                or str(PurePosixPath(name)) != name
                or any(p in {"..", ".git"} for p in parts)
            ):
                raise RuntimeError("isolated edits must stay inside target")
            if (
                not isinstance(entry, dict)
                or set(entry) != {"data", "executable"}
                or not isinstance(entry["executable"], bool)
            ):
                raise RuntimeError("invalid isolated edit entry")
            data = entry["data"]
            changed[name] = (None if data is None else base64.b64decode(data, validate=True), entry["executable"])
        violations = check_paths(list(changed), *surface_patterns(workspace))
        if violations:
            raise RuntimeError("isolated edits exceed declared mutable surface")
        # Snapshot first. Target bytes and notes are ordinary file changes, not actions.
        target = read_tree(child / "target")
        modes = {name: (child / "target" / name).stat().st_mode & 0o777 for name in target}
        for name, (data, executable) in changed.items():
            relative = name.removeprefix("target/")
            if data is None:
                target.pop(relative, None)
                modes.pop(relative, None)
            else:
                target[relative] = data
                modes[relative] = 0o755 if executable else 0o644
        transfers[child / "target"] = target
    else:
        modes = {}
    # Materialize every return before changing any host destination.
    staged = []
    for index, (destination, files) in enumerate(transfers.items()):
        staging = attempt_dir / f"accepted-{index}"
        write_tree(staging, files)
        if destination.name == "target":
            for name, mode in modes.items():
                (staging / name).chmod(mode)
        staged.append((destination, staging))
    from .agent_handoff import commit_handoff

    commit_handoff(workspace, attempt_dir, staged, action)


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite JSON constants are not allowed")
