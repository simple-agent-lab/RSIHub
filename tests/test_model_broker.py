import base64
import json
import sys
import time
from pathlib import Path

import pytest

from evolve.runtime.files import write_regular_file
from evolve.runtime.model_broker import ModelBroker, ModelBrokerConfig


def handler(tmp_path: Path, usage: str = "{'total_tokens':7,'cost_usd':0.25}") -> tuple[str, ...]:
    script = tmp_path / "handler.py"
    script.write_text(
        """import base64,json,sys
from pathlib import Path
request, response=map(Path,sys.argv[1:])
count=Path(__file__).with_name('calls')
with count.open('a') as stream: stream.write('call\\n')
body=request.read_bytes()
response.write_text(json.dumps({'status':200,'content_type':'application/json','body':base64.b64encode(body).decode(),'usage':USAGE}))
""".replace("USAGE", usage)
    )
    return (sys.executable, str(script))


def send(output: Path, request_id: str, body: bytes = b'{"prompt":"hello"}') -> dict:
    write_regular_file(
        output, f"broker/requests/{request_id}.json", json.dumps({"body": base64.b64encode(body).decode()}).encode()
    )
    path = output / f"broker/responses/{request_id}.json"
    deadline = time.monotonic() + 10
    while not path.is_file():
        assert time.monotonic() < deadline, "model broker did not respond"
        time.sleep(0.01)
    return json.loads(path.read_bytes())


def test_broker_meters_once_and_refuses_reused_id(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    with ModelBroker(ModelBrokerConfig(handler(tmp_path), 3, 1), output, tmp_path / "ledger") as broker:
        assert send(output, "a" * 32)["status"] == 200
        assert send(output, "a" * 32)["status"] == 200
        assert broker.usage() == {"total_tokens": 7, "cost_usd": 0.25}
        send(output, "a" * 32, b"changed")
        assert broker.stop.wait(5)
        assert "different content" in broker.failure
    assert (tmp_path / "calls").read_text().splitlines() == ["call"]
    receipt = json.loads((tmp_path / "ledger" / ("a" * 32) / "receipt.json").read_text())
    assert receipt["status"] == "completed" and receipt["cost_usd"] == 0.25


@pytest.mark.parametrize(
    "usage", ["{'total_tokens':7,'cost_usd':None}", "{'total_tokens':7,'cost_usd':float('nan')}", "{}"]
)
def test_unknown_usage_is_not_zero_and_stops_further_model_calls(tmp_path, usage):
    output = tmp_path / "output"
    output.mkdir()
    with ModelBroker(ModelBrokerConfig(handler(tmp_path, usage), 3, 1), output, tmp_path / "ledger") as broker:
        send(output, "a" * 32)
        assert broker.usage()["cost_usd"] is None
        assert send(output, "b" * 32)["status"] == 400
    assert (tmp_path / "calls").read_text().splitlines() == ["call"]


def test_budget_checked_between_requests_and_existing_ledger_not_replayed(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    config = ModelBrokerConfig(handler(tmp_path), 3, 0.25)
    with ModelBroker(config, output, tmp_path / "ledger") as broker:
        send(output, "a" * 32)
        assert send(output, "b" * 32)["status"] == 400
        assert broker.usage()["cost_usd"] == 0.25
    with pytest.raises(FileExistsError):
        with ModelBroker(config, output, tmp_path / "ledger"):
            pytest.fail("must not replay a possibly interrupted handler")


def test_request_and_response_links_cannot_read_or_overwrite_host_files(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    private = tmp_path / "private"
    private.write_text("private host data")
    with ModelBroker(ModelBrokerConfig(handler(tmp_path), 2, 1), output, tmp_path / "ledger") as broker:
        (output / "broker/requests" / ("a" * 32 + ".json")).symlink_to(private)
        assert broker.stop.wait(5)
        assert broker.failure is not None
    assert not (tmp_path / "calls").exists()
    (output / "broker/responses" / ("b" * 32 + ".json")).symlink_to(private)
    write_regular_file(output, "broker/responses/" + "b" * 32 + ".json", b"public response")
    assert private.read_text() == "private host data"
