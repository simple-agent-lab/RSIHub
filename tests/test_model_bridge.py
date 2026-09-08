import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from evolve.runtime.files import write_regular_file
from evolve.runtime.model_bridge import ModelBridge


@pytest.fixture
def bridge(tmp_path):
    (tmp_path / "broker/requests").mkdir(parents=True)
    (tmp_path / "broker/responses").mkdir(parents=True)
    with ModelBridge(tmp_path, timeout_s=0.25) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def test_http_model_response_preserves_sse_bytes_and_omits_credentials(bridge):
    import time

    body = b'{"model":"fixture","stream":true}'
    answer = b'data: {"type":"response.completed"}\n\ndata: [DONE]\n\n'
    request = Request(bridge.base_url + "/responses", data=body, headers={"Authorization": "Bearer dummy"})
    with ThreadPoolExecutor() as pool:
        future = pool.submit(urlopen, request, timeout=2)
        deadline = time.monotonic() + 2
        while not (paths := list((bridge.output / "broker/requests").glob("*.json"))):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        payload = json.loads(paths[0].read_text())
        assert payload == {"body": base64.b64encode(body).decode()}
        write_regular_file(
            bridge.output,
            "broker/responses/" + paths[0].name,
            json.dumps(
                {"status": 200, "content_type": "text/event-stream", "body": base64.b64encode(answer).decode()}
            ).encode(),
        )
        with future.result() as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            assert response.read() == answer


def test_timeout_does_not_reissue_request(bridge):
    with pytest.raises(HTTPError) as error:
        urlopen(Request(bridge.base_url + "/responses", data=b"{}"), timeout=2)
    assert error.value.code == 504
    assert len(list((bridge.output / "broker/requests").glob("*.json"))) == 1


def test_unknown_route_cannot_select_host_destination(bridge):
    with pytest.raises(HTTPError) as error:
        urlopen(Request(bridge.base_url + "/arbitrary-host-path", data=b"{}"), timeout=2)
    assert error.value.code == 404
    assert not list((bridge.output / "broker/requests").iterdir())
