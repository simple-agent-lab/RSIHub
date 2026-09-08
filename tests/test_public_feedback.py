import json

from evolve.public_feedback import archive_row, operator_feedback


def test_archive_projection_excludes_private_diagnostics_and_sealed_vectors():
    row = {
        "genid": "1",
        "score": 0.5,
        "task_set_hash": "abc",
        "valid_parent": True,
        "reason": "PRIVATE",
        "task_vector": {"secret-task": 1},
        "diagnostics": {"stderr": "PRIVATE"},
        "evals": [{"purpose": "anchor", "score": 0.1}, {"purpose": "candidate", "score": 0.5, "reason": "PRIVATE"}],
    }
    projected = archive_row(row)
    assert projected["score"] == 0.5
    assert projected["evals"] == [{"purpose": "candidate", "score": 0.5}]
    assert "PRIVATE" not in json.dumps(projected)
    assert "secret-task" not in json.dumps(projected)


def test_operator_observation_does_not_copy_nested_jobs_or_raw_text(tmp_path):
    (tmp_path / "rollout/job/verifier").mkdir(parents=True)
    (tmp_path / "rollout/job/verifier/stdout.txt").write_text("PRIVATE")
    (tmp_path / "rollout/summary.json").write_text(
        json.dumps({"score": 0.5, "agent_errors": 1, "error": "PRIVATE", "score_explanation": "PRIVATE"})
    )
    (tmp_path / "gate.json").write_text(json.dumps({"valid_parent": True, "verdict": "accept", "reason": "PRIVATE"}))
    output = operator_feedback(tmp_path)
    assert set(output) == {"rollout/summary.json", "gate.json"}
    assert json.loads(output["rollout/summary.json"]) == {"score": 0.5, "agent_errors": 1}
    assert all(b"PRIVATE" not in body for body in output.values())
