import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_codex_controller_wrapper_records_tokens_and_resumes_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = tmp_path / "controller"
    controller.mkdir()
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli 0.149.0')\n"
        "    raise SystemExit(0)\n"
        "open(os.environ['FAKE_ARGS'], 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'session-123'}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 3}}))\n"
    )
    fake.chmod(0o755)
    bundle = tmp_path / "method"
    bundle.mkdir()
    content = "control the experiment\n"
    (bundle / "instructions.md").write_text(content)
    method = {
        "activation_id": "initial",
        "digest": "fixture",
        "files": {"instructions.md": hashlib.sha256(content.encode()).hexdigest()},
    }
    inputs = controller / "input.json"
    inputs.write_text(
        json.dumps(
            {
                "optimizer": method,
                "optimizer_path": str(bundle),
                "objective": "learn",
                "research_workspace": str(workspace / "notes"),
            }
        )
    )

    for attempt in (1, 2):
        attempt_dir = controller / f"attempt-{attempt}"
        attempt_dir.mkdir()
        usage = attempt_dir / "usage.json"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/codex_agent_controller.py"),
                "--codex",
                str(fake),
            ],
            cwd=workspace,
            env={
                **os.environ,
                "EVOLVE_AGENT_WORKSPACE": str(workspace),
                "EVOLVE_CONTROLLER_ATTEMPT": str(attempt),
                "EVOLVE_CONTROLLER_ATTEMPT_DIR": str(attempt_dir),
                "EVOLVE_CONTROLLER_USAGE_RECEIPT": str(usage),
                "FAKE_ARGS": str(attempt_dir / "args.json"),
                "EVOLVE_CONTROLLER_INPUT": str(inputs),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(usage.read_text()) == {"total_tokens": 10, "cost_usd": None}

    assert (controller / "contexts/initial/codex-session-id").read_text() == "session-123\n"
    second_events = (controller / "attempt-2/codex-events.jsonl").read_text()
    assert "turn.completed" in second_events
    second_args = json.loads((controller / "attempt-2/args.json").read_text())
    assert second_args[:3] == ["exec", "resume", "--json"]
    assert "session-123" in second_args


def test_method_activation_uses_new_context_and_actual_prompt(tmp_path: Path) -> None:
    import hashlib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = tmp_path / "controller"
    controller.mkdir()
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "if sys.argv[1:] == ['--version']:\n print('codex-cli 0.149.0'); raise SystemExit(0)\n"
        "open(os.environ['FAKE_ARGS'],'w').write(json.dumps(sys.argv[1:]))\n"
        "open(os.environ['FAKE_PROMPT'],'w').write(sys.stdin.read())\n"
        "print(json.dumps({'type':'thread.started','thread_id':'session-'+os.environ['EVOLVE_CONTROLLER_ATTEMPT']}))\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n"
    )
    fake.chmod(0o755)
    for n, activation in [(1, "initial"), (2, "initial"), (3, "adopt-new")]:
        bundle = tmp_path / activation
        bundle.mkdir(exist_ok=True)
        content = "FIRST-METHOD" if n < 3 else "SECOND-METHOD"
        (bundle / "instructions.md").write_text(content)
        method = {
            "digest": activation,
            "activation_id": activation,
            "files": {"instructions.md": hashlib.sha256(content.encode()).hexdigest()},
        }
        attempt = controller / f"attempt-{n}"
        attempt.mkdir()
        input_path = attempt / "controller-input.json"
        input_path.write_text(
            json.dumps(
                {
                    "optimizer": method,
                    "optimizer_path": str(bundle),
                    "objective": "learn",
                    "research_workspace": str(workspace / "notes"),
                }
            )
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/codex_agent_controller.py"), "--codex", str(fake)],
            cwd=workspace,
            env={
                **os.environ,
                "EVOLVE_AGENT_WORKSPACE": str(workspace),
                "EVOLVE_CONTROLLER_ATTEMPT": str(n),
                "EVOLVE_CONTROLLER_ATTEMPT_DIR": str(attempt),
                "EVOLVE_CONTROLLER_USAGE_RECEIPT": str(attempt / "usage.json"),
                "EVOLVE_CONTROLLER_INPUT": str(input_path),
                "FAKE_ARGS": str(attempt / "args.json"),
                "FAKE_PROMPT": str(attempt / "prompt.txt"),
            },
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert content in (attempt / "prompt.txt").read_text()
        assert json.loads((attempt / "method-load.json").read_text()) == method
    assert "resume" in json.loads((controller / "attempt-2/args.json").read_text())
    assert "resume" not in json.loads((controller / "attempt-3/args.json").read_text())
    assert "FIRST-METHOD" not in (controller / "attempt-3/prompt.txt").read_text()


def test_isolated_codex_uses_file_protocol_and_activation_owned_home(tmp_path):
    import hashlib

    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    bundle = inputs / "optimizer"
    bundle.mkdir()
    (bundle / "instructions.md").write_text("Research method")
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\nimport sys,os,json\nfrom pathlib import Path\nif sys.argv[1:]==['--version']:print('codex-cli 0.149.0');raise SystemExit()\nPath('args.json').write_text(json.dumps(sys.argv[1:]))\nPath('prompt.txt').write_text(sys.stdin.read())\nPath('home.txt').write_text(os.environ['CODEX_HOME'])\nprint(json.dumps({'type':'thread.started','thread_id':'file-session'}))\n"
    )
    fake.chmod(0o755)
    for n, activation in [(1, "initial"), (2, "initial"), (3, "next")]:
        optimizer = {
            "digest": activation,
            "activation_id": activation,
            "files": {"instructions.md": hashlib.sha256(b"Research method").hexdigest()},
        }
        (inputs / "controller-input.json").write_text(
            json.dumps(
                {
                    "attempt": n,
                    "optimizer": optimizer,
                    "optimizer_path": str(bundle),
                    "objective": "learn",
                    "research_workspace": str(output / "notes"),
                }
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/codex_agent_controller.py"),
                "--codex",
                str(fake),
                "--isolated-input",
                str(inputs),
                "--isolated-output",
                str(output),
            ],
            env={**os.environ, "EVOLVE_MODEL_BASE_URL": "http://127.0.0.1:12345/v1"},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        args = json.loads((output / "args.json").read_text())
        assert ("resume" in args) == (n == 2)
        assert 'model_providers.evolve_broker.base_url="http://127.0.0.1:12345/v1"' in args
        assert str(output / "action.json") in (output / "prompt.txt").read_text()
        assert (output / "home.txt").read_text() == str(output / "contexts" / activation / "codex-home")
        assert json.loads((output / "method-load.json").read_text()) == optimizer
