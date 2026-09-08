import json

import pytest

from evolve.operator_isolation import run_isolated_operator, session_sandbox
from evolve.runtime.process import OwnedResult
from evolve.runtime.sandbox import SandboxConfig

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "target").mkdir(parents=True)
    (workspace / "target/file.txt").write_text("old")
    (workspace / "operators").mkdir()
    (workspace / "operators/validate.py").write_text("raise RuntimeError('must not execute on host')")
    (workspace / "evaluator").mkdir()
    (workspace / "evaluator/private").write_text("private")
    (workspace / "evolve.yaml").write_text('surface:\n  include: ["target/**"]\n')
    monkeypatch.setattr("evolve.operator_isolation.resolve_image", lambda *args: IMAGE)
    return workspace


def test_operator_accepts_only_public_candidate_and_stage_outputs(fixture, monkeypatch):
    def worker(config, *, inputs, output, **kwargs):
        assert not (inputs / "workspace/evaluator").exists()
        assert not (output / "checkout/evaluator").exists()
        assert not (inputs / "workspace/.git").exists()
        (output / "checkout/target/file.txt").write_text("new")
        (output / "checkout/target/file.txt").chmod(0o755)
        (output / "artifacts/validate").mkdir()
        (output / "artifacts/validate/result.json").write_text('{"accept":true}')
        (output / "artifacts/forged-host-ledger").write_text("invalid")
        return OwnedResult(0, "", "", 0, False)

    monkeypatch.setattr("evolve.operator_isolation.run_sandbox", worker)
    run_dir = fixture / "runs/gen-1"
    run_isolated_operator(
        name="validate",
        checkout=fixture,
        source=fixture,
        workspace=fixture,
        genid="1",
        parent="0",
        run_dir=run_dir,
        config_block={},
        sandbox=SandboxConfig(IMAGE, 10),
    )
    assert (fixture / "target/file.txt").read_text() == "new"
    assert (fixture / "target/file.txt").stat().st_mode & 0o111
    assert json.loads((run_dir / "validate/result.json").read_text()) == {"accept": True}
    assert not (run_dir / "forged-host-ledger").exists()
    assert (fixture / "evaluator/private").read_text() == "private"


@pytest.mark.parametrize("attack", ["scope", "symlink"])
def test_bad_operator_return_applies_no_candidate_changes(fixture, monkeypatch, attack):
    def worker(config, *, output, **kwargs):
        (output / "checkout/target/file.txt").write_text("unaccepted")
        if attack == "scope":
            (output / "checkout/forbidden").write_text("bad")
        else:
            (output / "artifacts/link").symlink_to(fixture / "evaluator/private")
        return OwnedResult(0, "", "", 0, False)

    monkeypatch.setattr("evolve.operator_isolation.run_sandbox", worker)
    with pytest.raises(RuntimeError):
        run_isolated_operator(
            name="validate",
            checkout=fixture,
            source=fixture,
            workspace=fixture,
            genid="1",
            parent="0",
            run_dir=fixture / "runs/gen-1",
            config_block={},
            sandbox=SandboxConfig(IMAGE, 10),
        )
    assert (fixture / "target/file.txt").read_text() == "old"


def test_session_operator_boundary_comes_from_controller_manifest(fixture):
    assert session_sandbox(fixture, 20) is None
    root = fixture / "runs/agent-driven/controller"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"sandbox": {"image": IMAGE, "timeout_s": 60}}))
    assert session_sandbox(fixture, 20) == SandboxConfig(IMAGE, 20)


def test_public_archive_projection_preserves_host_certified_parent_selection(tmp_path, monkeypatch):
    from evolve.frozen.interfaces import ArchiveView

    rows = [{"genid": "0", "score": 1}, {"genid": "1", "score": 0}]
    projection = tmp_path / "archive-view.json"
    projection.write_text(json.dumps({"rows": rows, "valid_parents": rows[:1]}))
    monkeypatch.setenv("EVOLVE_PUBLIC_ARCHIVE", str(projection))
    view = ArchiveView(tmp_path)
    assert view.rows() == rows
    assert view.valid_parents() == rows[:1]
    assert view.best_ever() == rows[0]
    assert view.row("1") == rows[1]
