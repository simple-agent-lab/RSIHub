import json
from pathlib import Path

import pytest

from evolve.integrations.harbor._runtime_plan import resolve_mounts, write_mount_plan


def test_explicit_common_mounts_survive_with_offline_tools(tmp_path: Path) -> None:
    ca = {"type": "bind", "source": str(tmp_path / "ca.crt"), "target": "/etc/ssl/certs/ca.crt", "read_only": True}
    binary = tmp_path / "codex"
    binary.touch()
    env = {"EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": json.dumps([ca]), "EVOLVE_CODEX_BINARY_PATH": str(binary)}
    mounts = resolve_mounts(env, tmp_path / "cache")
    assert mounts == [ca, {"type": "bind", "source": str(binary), "target": "/usr/local/bin/codex", "read_only": True}]
    write_mount_plan(tmp_path, mounts)
    assert json.loads((tmp_path / "candidate-runtime.mounts.json").read_text()) == mounts
    assert json.loads((tmp_path / "candidate-runtime.plan.json").read_text())["mounts"] == mounts


@pytest.mark.parametrize("value", ["{}", "[null]", '[{"type":"volume"}]', "bad"])
def test_invalid_mount_plan_fails_before_launch(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        resolve_mounts({"EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": value}, tmp_path / "cache")


def test_conflicting_target_fails_before_launch(tmp_path: Path) -> None:
    mount = {"type": "bind", "source": str(tmp_path), "target": "/cache"}
    with pytest.raises(ValueError, match="duplicate"):
        resolve_mounts({"EVOLVE_CANDIDATE_RUNTIME_MOUNTS_JSON": json.dumps([mount, mount])}, tmp_path)


def test_default_cache_and_python_are_shared(tmp_path: Path) -> None:
    mounts = resolve_mounts({"EVOLVE_UV_PYTHON_INSTALL_DIR": str(tmp_path / "python")}, tmp_path / "cache")
    assert [m["target"] for m in mounts] == ["/opt/evolve/uv/cache", "/installed-agent/uv-python"]


def test_compose_overlays_preserve_order_spaces_and_content_identity(tmp_path: Path) -> None:
    from evolve.integrations.harbor._runtime_plan import write_compose_plan

    overlay = tmp_path / "network configuration.yaml"
    overlay.write_text("networks: {}\n")
    env = {"EVOLVE_HARBOR_EXTRA_DOCKER_COMPOSE_JSON": json.dumps([str(overlay)])}
    assert write_compose_plan(tmp_path / "train", env) == [str(overlay)]
    write_compose_plan(tmp_path / "eval", env)
    train = tmp_path / "train/candidate-runtime.compose.json"
    assert train.read_bytes() == (tmp_path / "eval/candidate-runtime.compose.json").read_bytes()
    before = train.read_bytes()
    overlay.write_text("networks: {default: {enable_ipv6: true}}\n")
    write_compose_plan(tmp_path / "train", env)
    assert train.read_bytes() != before


@pytest.mark.parametrize("raw", ["{}", "[null]", '["relative.yaml"]', '["/missing/overlay.yaml"]', "bad"])
def test_invalid_compose_inputs_fail_before_launch(tmp_path: Path, raw: str) -> None:
    from evolve.integrations.harbor._runtime_plan import write_compose_plan

    with pytest.raises(ValueError):
        write_compose_plan(tmp_path, {"EVOLVE_HARBOR_EXTRA_DOCKER_COMPOSE_JSON": raw})
