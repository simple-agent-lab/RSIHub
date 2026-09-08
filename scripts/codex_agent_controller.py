#!/usr/bin/env python3
"""Run or resume a Codex exec controller and emit an RSIHub usage receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--isolated-input", type=Path)
    parser.add_argument("--isolated-output", type=Path, default=Path("/output"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--require-version", default="0.149.0")
    parser.add_argument("--codex-arg", action="append", default=[])
    args = parser.parse_args()

    if args.isolated_input is not None:
        _prepare_isolated(args)
    workspace = _required_path("EVOLVE_AGENT_WORKSPACE")
    attempt_dir = _required_path("EVOLVE_CONTROLLER_ATTEMPT_DIR")
    usage_receipt = _required_path("EVOLVE_CONTROLLER_USAGE_RECEIPT")
    attempt = int(os.environ["EVOLVE_CONTROLLER_ATTEMPT"])
    _check_version(args.codex, args.require_version)
    method = json.loads(_required_path("EVOLVE_CONTROLLER_INPUT").read_text())
    context = workspace if args.isolated_input is not None else attempt_dir.parent
    context = context / "contexts" / method["optimizer"]["activation_id"]
    context.mkdir(parents=True, exist_ok=True)
    session_path = context / "codex-session-id"
    session_id = session_path.read_text().strip() if session_path.is_file() else None
    bundle = Path(method["optimizer_path"])
    expected = method["optimizer"]["files"]
    actual = {
        p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(bundle.rglob("*"))
        if p.is_file()
    }
    if actual != expected or any(p.is_symlink() for p in bundle.rglob("*")):
        raise RuntimeError("optimizer bundle changed before controller loading")
    prompt_path = bundle / ("resume.md" if session_id and "resume.md" in expected else "instructions.md")
    prompt = prompt_path.read_text()
    prompt += "\nResearch objective: " + method["objective"]
    prompt += "\nPersistent research files (read/write freely): " + method["research_workspace"]
    prompt += "\nActive research method and tools: " + str(bundle)
    prompt += (
        "\nTo change your method, copy it into runs/agent-driven/optimizer/drafts, edit it, then request adopt_optimizer with expected_digest="
        + method["optimizer"]["digest"]
    )
    prompt += "\nPublish intermediate results with publish_best; use pause_research or finish_research to stop.\n"
    (attempt_dir / "method-load.json").write_text(json.dumps(method["optimizer"], sort_keys=True) + "\n")
    if args.isolated_input is not None:
        prompt += _file_protocol(args.isolated_input, workspace)
    elif os.environ.get("EVOLVE_CONTROLLER_ACTION_MODE") == "deferred":
        prompt += (
            "\nThe host is running continuous deferred-action mode. Read the last action receipt, "
            "make one decision, and request exactly one typed action with `agent act --defer`. "
            "Then finish this controller turn immediately. The host executes the action after you "
            "exit and resumes you with its result. Do not poll or wait for the queued action. "
            "Direct edits are permitted only in the existing managed child worktree and declared "
            "mutable surface, before requesting its next action.\n"
        )
    facts_path = os.environ.get("EVOLVE_CONTROLLER_FACTS")
    if facts_path:
        prompt += (
            "\nHost facts and action results: "
            + facts_path
            + ". These receipts, resource limits and evaluator configuration cannot be modified.\n"
        )
    correction_path = (args.isolated_input or attempt_dir.parent) / "correction.json"
    if correction_path.is_file():
        correction = json.loads(correction_path.read_text())
        if args.isolated_input is not None or correction.get("attempt") == attempt - 1:
            prompt += "\nPrevious action was rejected before execution. Correct it: " + correction["error"]
    raw_stdout = attempt_dir / "codex-events.jsonl"
    raw_stderr = attempt_dir / "codex-stderr.log"
    command = [args.codex, "exec"]
    if session_id is not None:
        command.extend(["resume", "--json", *args.codex_arg, session_id, "-"])
    else:
        command.extend(["--json", "-C", str(workspace), *args.codex_arg, "-"])
    environment = dict(os.environ)
    if args.isolated_input is not None:
        context_home = context / "codex-home"
        context_home.mkdir(parents=True, exist_ok=True)
        environment["CODEX_HOME"] = str(context_home)
    with raw_stdout.open("w") as stdout, raw_stderr.open("w") as stderr:
        result = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            input=prompt,
            text=True,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    events = _read_events(raw_stdout)
    discovered = _session_id(events)
    if discovered is not None:
        session_path.write_text(f"{discovered}\n")
    tokens = _total_tokens(events)
    if tokens is not None:
        usage_receipt.write_text(json.dumps({"total_tokens": tokens, "cost_usd": None}) + "\n")
    print(
        json.dumps(
            {
                "attempt": attempt,
                "codex_session_id": discovered or session_id,
                "total_tokens": tokens,
                "returncode": result.returncode,
                "events": str(raw_stdout),
                "stderr": str(raw_stderr),
            },
            sort_keys=True,
        )
    )
    raise SystemExit(result.returncode)


def _prepare_isolated(args: argparse.Namespace) -> None:
    inputs, output = args.isolated_input.resolve(), args.isolated_output.resolve()
    method = json.loads((inputs / "controller-input.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    endpoint = os.environ.get("EVOLVE_MODEL_BASE_URL", "")
    if not endpoint.startswith("http://127.0.0.1:"):
        raise RuntimeError("isolated Codex requires the container-local model bridge")
    os.environ.update(
        {
            "EVOLVE_AGENT_WORKSPACE": str(output),
            "EVOLVE_CONTROLLER_ATTEMPT_DIR": str(output),
            "EVOLVE_CONTROLLER_USAGE_RECEIPT": str(output / "usage.json"),
            "EVOLVE_CONTROLLER_ATTEMPT": str(method.get("attempt", 1)),
            "EVOLVE_CONTROLLER_INPUT": str(inputs / "controller-input.json"),
            "EVOLVE_CONTROLLER_FACTS": str(inputs / "session-facts.json"),
            "EVOLVE_CONTROLLER_ACTION_MODE": "file",
        }
    )
    settings = {
        "model_provider": "evolve_broker",
        "model_providers.evolve_broker.name": "RSIHub metered broker",
        "model_providers.evolve_broker.base_url": endpoint,
        "model_providers.evolve_broker.env_key": "OPENAI_API_KEY",
        "model_providers.evolve_broker.wire_api": "responses",
        "model_providers.evolve_broker.request_max_retries": 0,
        "model_providers.evolve_broker.stream_max_retries": 0,
        "model_providers.evolve_broker.supports_websockets": False,
    }
    args.codex_arg.extend(["--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"])
    for key, value in settings.items():
        args.codex_arg.extend(["-c", key + "=" + json.dumps(value)])


def _file_protocol(inputs: Path, output: Path) -> str:
    return (
        "\nThis is an isolated file-transport session. Read "
        + str(inputs / "actions.json")
        + " for action schemas and "
        + str(inputs / "session.json")
        + " for current state."
        + " Write exactly one action JSON to "
        + str(output / "action.json")
        + " and end this turn."
        + " Do not run agent act or wait for execution; the host executes after you exit."
        + " Keep persistent notes in "
        + str(output / "notes")
        + "."
        + " Method drafts belong in "
        + str(output / "drafts")
        + "; their action draft_path is runs/agent-driven/optimizer/drafts/NAME."
        + " Read existing child targets under "
        + str(inputs / "children")
        + "."
        + " To edit a child, write edits.json in the output directory: map target/RELATIVE_PATH"
        + " to {data: BASE64_BYTES, executable: BOOLEAN}; null data deletes a file."
        + " Return edits together with checkpoint, operator, or commit naming that existing child."
        + " Input files are read-only snapshots. No original repository is present.\n"
    )


def _required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required; run this wrapper through evolve agent run-controller")
    return Path(value).resolve()


def _check_version(command: str, required: str) -> None:
    result = subprocess.run([command, "--version"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"cannot execute {command}")
    installed = result.stdout.strip().split()[-1]
    if installed != required:
        raise RuntimeError(f"Codex {required} is required, found {installed}")


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid Codex JSON event at line {line_number}") from exc
        if not isinstance(event, dict):
            raise RuntimeError(f"invalid Codex JSON event at line {line_number}")
        events.append(event)
    return events


def _session_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") != "thread.started":
            continue
        value = event.get("thread_id") or event.get("session_id")
        if isinstance(value, str) and value:
            return value
    return None


def _total_tokens(events: list[dict[str, Any]]) -> int | None:
    for event in reversed(events):
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (input_tokens, output_tokens)
        ):
            return int(input_tokens) + int(output_tokens)
    return None


if __name__ == "__main__":
    main()
