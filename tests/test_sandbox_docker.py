"""Opt-in real-daemon regressions: EVOLVE_TEST_DOCKER_IMAGE=alpine:3.20 pytest --run-slow."""

import json
import os
import sys

import pytest

from evolve.runtime.model_broker import ModelBroker, ModelBrokerConfig
from evolve.runtime.sandbox import SandboxConfig, resolve_image, run_sandbox

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get("EVOLVE_TEST_DOCKER_IMAGE"), reason="explicit local Docker test image required"
    ),
]


@pytest.mark.parametrize("case", ["quota", "timeout", "broker"])
def test_bounded_tmpfs_transport_with_real_docker(tmp_path, case):
    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    config = SandboxConfig(os.environ["EVOLVE_TEST_DOCKER_IMAGE"], 15, output_mb=1)
    identity = resolve_image(config, tmp_path)
    if case == "quota":
        result = run_sandbox(
            config,
            image_id=identity,
            inputs=inputs,
            output=output,
            command=["sh", "-c", "dd if=/dev/zero of=/output/large bs=1024 count=2048"],
        )
        assert result.returncode != 0 and "No space left" in result.stderr
        assert (output / "large").stat().st_size <= 1024 * 1024
    elif case == "timeout":
        config = SandboxConfig(config.image, 1)
        result = run_sandbox(config, image_id=identity, inputs=inputs, output=output, command=["sleep", "30"])
        assert result.timed_out and result.wall_s < 17
    else:
        handler = tmp_path / "fixture.py"
        handler.write_text(
            "import base64,json,sys\nfrom pathlib import Path\nrequest,response=map(Path,sys.argv[1:])\nresponse.write_text(json.dumps({'status':200,'content_type':'application/json','body':base64.b64encode(request.read_bytes()).decode(),'usage':{'total_tokens':7,'cost_usd':0.25}}))\n"
        )
        request = "a" * 32
        script = (
            'printf \'{"body":"aGVsbG8="}\' > /output/broker/requests/' + request + ".json; "
            "while [ ! -f /output/broker/responses/" + request + ".json ]; do sleep 0.02; done; "
            "cp /output/broker/responses/" + request + ".json /output/result.json"
        )
        with ModelBroker(
            ModelBrokerConfig((sys.executable, str(handler)), 1, 1), output, tmp_path / "ledger"
        ) as broker:
            result = run_sandbox(config, image_id=identity, inputs=inputs, output=output, command=["sh", "-c", script])
        assert result.returncode == 0 and broker.failure is None
        assert broker.usage() == {"total_tokens": 7, "cost_usd": 0.25}
        assert json.loads((output / "result.json").read_text())["status"] == 200
    # A successful return also requires confirmed daemon-side removal.
    receipts = list(tmp_path.glob("sandbox-lease-*/*.receipt.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["state"] == "stopped"


def test_supervisor_removes_container_after_launcher_sigkill(tmp_path):
    import signal
    import subprocess
    import time
    import uuid

    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    config = SandboxConfig(os.environ["EVOLVE_TEST_DOCKER_IMAGE"], 30)
    identity = resolve_image(config, tmp_path)
    name = "evolve-sandbox-" + uuid.uuid4().hex
    script = (
        "import sys\nfrom pathlib import Path\nfrom evolve.runtime.sandbox import SandboxConfig,run_sandbox\n"
        "image,root,name=sys.argv[1:]\n"
        'run_sandbox(SandboxConfig(image,30),image_id=image,inputs=Path(root)/"input",'
        'output=Path(root)/"output",container_name=name,command=["sleep","100"])\n'
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, identity, str(tmp_path), name], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    def exists():
        return subprocess.run(["docker", "inspect", name], capture_output=True, timeout=5).returncode == 0

    try:
        deadline = time.monotonic() + 15
        while not exists():
            assert process.poll() is None, "launcher exited before container creation"
            assert time.monotonic() < deadline
            time.sleep(0.05)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        deadline = time.monotonic() + 10
        while exists():
            assert time.monotonic() < deadline, "container survived launcher death"
            time.sleep(0.05)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=20)
