"""Deterministic Harbor transport for real recipe operators, never a model client."""

import json
import os
import shutil
import sys
from pathlib import Path

MARKER = "Offline recipe regression mutation"


def option(*names, default=None):
    for name in names:
        if name in sys.argv:
            return sys.argv[sys.argv.index(name) + 1]
    if default is not None:
        return default
    raise ValueError(f"missing option {names}")


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def trial(root, name, reward, message):
    directory = root / (name + "__offline")
    write(
        directory / "result.json",
        {
            "trial_name": directory.name,
            "task_name": name,
            "agent_info": {"name": "codex", "version": "offline", "model_info": {"name": "offline", "provider": None}},
            "exception_info": None,
            "agent_result": {"n_input_tokens": 0, "n_cache_tokens": 0, "n_output_tokens": 0, "cost_usd": 0},
            "verifier_result": {"rewards": {"reward": reward}},
        },
    )
    write(directory / "agent/trajectory.json", {"steps": [{"source": "agent", "message": message}]})
    return directory


def main():
    with Path(os.environ["RECIPE_HARBOR_LOG"]).open("a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\n")
    root = Path(option("--jobs-dir")) / option("--job-name", default="offline-job")
    if sys.argv[1] == "exec":
        source = Path(option("--path", "-p"))
        directory = trial(root, "task-0001", 1, "Completed deterministic mutation.")
        if (source / "workspace").exists():
            artifact = directory / "artifacts/app/task/workspace"
            shutil.copytree(source / "workspace", artifact)
            prompt = artifact / "target/prompt.md"
            if not prompt.exists():
                prompt = artifact / "target/knowledge.md"
            if not prompt.exists():
                prompt = artifact / "target/src/minisweagent/config/mini.yaml"
            prompt.write_text(prompt.read_text() + f"\n# {MARKER}\n")
            config = (artifact / "evolve.yaml").read_text()
            if "operator: ahe\n" in config:
                write(
                    artifact / "target/.ahe-change-manifest.json",
                    {
                        "iteration": "1",
                        "changes": [
                            {
                                "id": "chg-1",
                                "type": "improvement",
                                "description": MARKER,
                                "files": [str(prompt.relative_to(artifact))],
                                "failure_pattern": "offline fixture",
                                "predicted_fixes": [],
                                "risk_tasks": [],
                                "constraint_level": "prompt",
                                "why_this_component": "exercise prompt mutation",
                            }
                        ],
                    },
                )
            exported = "/app/task/workspace"
        else:
            exported = "/logs/artifacts"
            report = directory / "artifacts/logs/artifacts/ahe-debugger-response.md"
            report.parent.mkdir(parents=True)
            response = (
                json.dumps({"score": 2, "category": "missing_output", "outcome": "failed", "failure_reason": "fixture"})
                if root.name.startswith("trajectory-judge-")
                else "ROOT CAUSE: deterministic fixture; inspect candidate instructions."
            )
            report.write_text(response + "\n")
            write(directory / "agent/trajectory.json", {"steps": [{"source": "agent", "message": response}]})
        write(
            directory / "artifacts/manifest.json",
            [
                {
                    "source": exported,
                    "destination": "artifacts" + exported,
                    "type": "directory",
                    "status": "ok",
                    "service": None,
                }
            ],
        )
        count = 1
    elif sys.argv[1] == "run":
        names = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == "--include-task-name"]
        assert names, "rollout must specify its actual task selection"
        candidates = [value.split("=", 1)[1] for value in sys.argv if value.startswith("EVOLVE_CANDIDATE_SOURCE=")]
        assert candidates
        candidate = Path(candidates[0])
        reward = int(
            any(
                MARKER in path.read_text()
                for path in candidate.rglob("*")
                if path.is_file() and path.suffix in {".md", ".yaml"}
            )
        )
        for name in names:
            trial(root, name, reward, "Fixture feedback: verify the requested output.")
        count = len(names)
    else:
        raise ValueError("only Harbor exec and run are allowed")
    write(root / "result.json", {"stats": {"n_completed_trials": count}})


if __name__ == "__main__":
    main()
