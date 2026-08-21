from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from harbor.viewer.server import create_app as create_harbor_app

from evolve.viewer.harbor_bridge import HarborBridge
from evolve.viewer.models import JobRootReference


def _harbor_jobs(root: Path, job: str, *, task: str = "task-a", trial: str = "trial-0") -> Path:
    trial_dir = root / job / trial
    trial_dir.mkdir(parents=True)
    (root / job / "config.json").write_text("{}")
    (trial_dir / "config.json").write_text(
        json.dumps(
            {
                "task": {"path": task, "source": "local/source"},
                "agent": {"name": "mini agent", "model_name": "openai/model name"},
            }
        )
    )
    return root


def test_bridge_federates_multiple_roots_without_collisions(tmp_path: Path) -> None:
    left = _harbor_jobs(tmp_path / "left", "same-job")
    right = _harbor_jobs(tmp_path / "right", "same-job")

    with HarborBridge(tmp_path / "workspace") as bridge:
        federation = bridge.refresh(
            [
                JobRootReference(generation="1", purpose="rollout", path=left),
                JobRootReference(generation="1", purpose="candidate", path=right),
            ]
        )
        links = sorted(federation.root.iterdir())
        assert len(links) == 2
        assert links[0].name != links[1].name
        assert all(path.is_dir() and not path.is_symlink() for path in links)


def test_bridge_removes_stale_links_and_cleans_up(tmp_path: Path) -> None:
    jobs = _harbor_jobs(tmp_path / "jobs", "job-a")
    bridge = HarborBridge(tmp_path / "workspace")
    root = bridge.__enter__().refresh([JobRootReference(generation="1", purpose="candidate", path=jobs)]).root

    bridge.refresh([])
    assert list(root.iterdir()) == []
    bridge.__exit__(None, None, None)
    assert not root.exists()


def test_bridge_names_are_stable_and_invalid_jobs_are_ignored(tmp_path: Path) -> None:
    jobs = _harbor_jobs(tmp_path / "jobs", "job with spaces")
    (jobs / "not-a-job").mkdir()
    reference = JobRootReference(generation="2", purpose="candidate", path=jobs)

    with HarborBridge(tmp_path / "workspace") as bridge:
        first = bridge.refresh([reference])
        first_names = sorted(path.name for path in first.root.iterdir())
        second_names = sorted(path.name for path in bridge.refresh([reference]).root.iterdir())

    assert first_names == second_names
    assert len(first_names) == 1
    assert first_names[0].startswith("job-with-spaces-")


def test_bridge_builds_full_harbor_trial_route(tmp_path: Path) -> None:
    jobs = _harbor_jobs(tmp_path / "jobs", "job-a", task="task-a", trial="trial one")

    with HarborBridge(tmp_path / "workspace") as bridge:
        federation = bridge.refresh([JobRootReference(generation="3", purpose="candidate", path=jobs)])

    link = federation.trial_links[("3", "candidate", "task-a", 0)]
    assert link.url.startswith("/jobs/job-a-")
    assert link.url.endswith("/tasks/local%2Fsource/mini%20agent/openai/model%20name/task-a/trials/trial%20one")


def test_bridge_jobs_pass_harbor_containment_checks(tmp_path: Path) -> None:
    jobs = _harbor_jobs(tmp_path / "jobs", "job-a")
    trajectory = jobs / "job-a/trial-0/agent/trajectory.json"
    trajectory.parent.mkdir()
    trajectory.write_text(json.dumps({"steps": []}))

    with HarborBridge(tmp_path / "experiment") as bridge:
        federation = bridge.refresh([JobRootReference(generation="3", purpose="candidate", path=jobs)])
        job_name = federation.job_names[(jobs.resolve(), "job-a")]
        with TestClient(create_harbor_app(federation.root)) as client:
            response = client.get(f"/api/jobs/{job_name}/trials/trial-0/trajectory")

    assert response.status_code == 200
    assert response.json() == {"steps": []}


def test_bridge_maps_only_unique_canonical_suffix(tmp_path: Path) -> None:
    jobs = _harbor_jobs(tmp_path / "jobs", "job-a", task="short")
    reference = JobRootReference(generation="4", purpose="candidate", path=jobs)

    with HarborBridge(tmp_path / "workspace") as bridge:
        unique = bridge.refresh([reference], canonical_tasks={("4", "candidate"): ("suite__short",)})
        assert ("4", "candidate", "suite__short", 0) in unique.trial_links

        ambiguous = bridge.refresh(
            [reference],
            canonical_tasks={("4", "candidate"): ("left__short", "right__short")},
        )
        assert ambiguous.trial_links == {}


def test_bridge_maps_dataset_qualified_harbor_task_name(tmp_path: Path) -> None:
    jobs = _harbor_jobs(
        tmp_path / "jobs",
        "job-a",
        task="sierra-research/tau3-bench__tau3-banking_knowledge-task-001",
    )
    reference = JobRootReference(generation="4", purpose="candidate", path=jobs)

    with HarborBridge(tmp_path / "workspace") as bridge:
        federation = bridge.refresh(
            [reference],
            canonical_tasks={("4", "candidate"): ("tau3-banking_knowledge-task-001",)},
        )

    assert ("4", "candidate", "tau3-banking_knowledge-task-001", 0) in federation.trial_links


def test_bridge_links_legacy_trials_rejected_by_current_harbor_models(tmp_path: Path) -> None:
    """Old writable-mount configs remain inspectable even when Harbor 0.18 rejects them."""
    jobs = _harbor_jobs(tmp_path / "jobs", "legacy-job", task="suite__task-a")
    trial = jobs / "legacy-job/trial-0"
    legacy_config = json.loads((trial / "config.json").read_text())
    legacy_config["environment"] = {"mounts": [{"read_only": False}]}
    (trial / "config.json").write_text(json.dumps(legacy_config))
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "suite__task-a",
                "source": "legacy-source",
                "agent_info": {
                    "name": "legacy-agent",
                    "model_info": {"provider": "openai", "name": "legacy-model"},
                },
                "verifier_result": {"rewards": {"reward": 0.75}},
                "started_at": "2026-07-30T10:00:00Z",
                "finished_at": "2026-07-30T10:00:02.500000Z",
                "config": {"environment": {"mounts": [{"read_only": False}]}},
            }
        )
    )

    with HarborBridge(tmp_path / "workspace") as bridge:
        federation = bridge.refresh(
            [JobRootReference(generation="5", purpose="candidate", path=jobs)],
            canonical_tasks={("5", "candidate"): ("suite__task-a",)},
        )
        copied_job = next(federation.root.iterdir())
        copied_result = json.loads((copied_job / "trial-0/result.json").read_text())
        copied_config = json.loads((copied_job / "trial-0/config.json").read_text())

    link = federation.trial_links[("5", "candidate", "suite__task-a", 0)]
    assert link.reward == 0.75
    assert link.duration_ms == 2500
    assert "/legacy-source/legacy-agent/openai/legacy-model/" in link.url
    assert copied_result["config"]["environment"]["mounts"][0]["read_only"] is True
    assert copied_config["environment"]["mounts"][0]["read_only"] is True
    source_result = json.loads((trial / "result.json").read_text())
    source_config = json.loads((trial / "config.json").read_text())
    assert source_result["config"]["environment"]["mounts"][0]["read_only"] is False
    assert source_config["environment"]["mounts"][0]["read_only"] is False


def test_bridge_counts_repetitions_per_task_not_per_job(tmp_path: Path) -> None:
    jobs = _harbor_jobs(tmp_path / "jobs", "job-a", task="task-a", trial="task-a-trial")
    _harbor_jobs(jobs, "job-a", task="task-b", trial="task-b-trial")

    with HarborBridge(tmp_path / "workspace") as bridge:
        federation = bridge.refresh([JobRootReference(generation="6", purpose="candidate", path=jobs)])

    assert ("6", "candidate", "task-a", 0) in federation.trial_links
    assert ("6", "candidate", "task-b", 0) in federation.trial_links


def test_bridge_preserves_multiple_logical_references_to_one_job_root(tmp_path: Path) -> None:
    jobs = _harbor_jobs(tmp_path / "jobs", "shared-job", task="task-a")

    with HarborBridge(tmp_path / "workspace") as bridge:
        federation = bridge.refresh(
            [
                JobRootReference(generation="9", purpose="candidate", path=jobs),
                JobRootReference(generation="10", purpose="rollout", path=jobs),
            ]
        )
        assert len(list(federation.root.iterdir())) == 1

    assert ("9", "candidate", "task-a", 0) in federation.trial_links
    assert ("10", "rollout", "task-a", 0) in federation.trial_links
