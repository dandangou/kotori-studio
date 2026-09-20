"""OpenAI-compatible translation contract over real loopback HTTP sockets.

No external API or real credentials are used. Run with:
    .venv/bin/python -m pytest tests/test_api_translation.py -q
The host must permit binding an ephemeral 127.0.0.1 port.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.pipeline import APIModel


FAKE_KEY = "TEST_ONLY_NOT_A_REAL_API_KEY_0d4c22"
PRIVATE_RESPONSE = "TEST_ONLY_PRIVATE_RESPONSE_CONTENT_61b8"


def completion(text="你好，大家！"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def local_api(monkeypatch):
    # Never inherit a user's real key or route this localhost test via a proxy.
    monkeypatch.delenv("KOTORI_API_KEY", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")
    running = []

    def start(respond=None):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                record = {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": body,
                }
                requests.append(record)
                status, payload, headers = (
                    respond(record) if respond else (200, completion(), {})
                )
                data = payload if isinstance(payload, bytes) else json.dumps(
                    payload, ensure_ascii=False
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_POST = handle_request
            do_GET = handle_request

            def log_message(self, *args):
                # Headers and test credentials stay in this fixture's memory.
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        running.append((server, thread))
        return SimpleNamespace(
            base=f"http://127.0.0.1:{server.server_port}", requests=requests
        )

    yield start
    for server, thread in reversed(running):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "local test HTTP server did not shut down"


@pytest.mark.parametrize(
    "base_suffix,expected_path",
    [
        ("/v1", "/v1/chat/completions"),
        ("/v1/", "/v1/chat/completions"),
        ("/v1/chat/completions", "/v1/chat/completions"),
        ("/v1/chat/completions/", "/v1/chat/completions"),
    ],
)
def test_post_path_json_authorization_and_utf8_response(
    local_api, base_suffix, expected_path
):
    server = local_api()
    model = APIModel({
        "api_base": server.base + base_suffix,
        "api_model": "test-model-only",
        "api_key": FAKE_KEY,
    })

    assert model.complete("翻译测试。", "こんにちは、みんな！", max_tokens=73) == "你好，大家！"
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == expected_path
    assert request["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request["headers"]["Content-Type"] == "application/json"
    assert json.loads(request["body"].decode("utf-8")) == {
        "model": "test-model-only",
        "messages": [
            {"role": "system", "content": "翻译测试。"},
            {"role": "user", "content": "こんにちは、みんな！"},
        ],
        "temperature": 0,
        "max_tokens": 73,
    }
    assert FAKE_KEY.encode() not in request["body"]


def test_worker_environment_key_is_used_without_adding_it_to_payload(local_api, monkeypatch):
    server = local_api()
    monkeypatch.setenv("KOTORI_API_KEY", FAKE_KEY)
    model = APIModel({"api_base": server.base + "/v1", "api_model": "test-only"})

    model.complete("system", "prompt")
    request = server.requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY.encode() not in request["body"]
    assert json.loads(request["body"])["max_tokens"] == 1600


def test_local_server_without_key_receives_no_authorization_header(local_api):
    server = local_api()
    model = APIModel({"api_base": server.base, "api_model": "test-only"})

    assert model.complete("system", "prompt") == "你好，大家！"
    assert server.requests[0]["path"] == "/chat/completions"
    assert "Authorization" not in server.requests[0]["headers"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed_and_key_never_reaches_second_server(local_api, status):
    destination = local_api()
    origin = local_api(lambda request: (
        status,
        {"error": f"{PRIVATE_RESPONSE}: {FAKE_KEY}"},
        {"Location": destination.base + "/v1/chat/completions"},
    ))
    model = APIModel({
        "api_base": origin.base + "/v1",
        "api_model": "test-only",
        "api_key": FAKE_KEY,
    })

    with pytest.raises(RuntimeError) as error:
        model.complete("system", "prompt")
    assert f"HTTP {status}" in str(error.value)
    assert len(origin.requests) == 1
    assert origin.requests[0]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert destination.requests == [], "redirect target must receive no request at all"
    assert FAKE_KEY not in str(error.value)
    assert PRIVATE_RESPONSE not in str(error.value)


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_error_message_does_not_echo_key_or_response_content(local_api, status, capsys):
    server = local_api(lambda request: (
        status, {"error": {"message": f"{PRIVATE_RESPONSE}: {FAKE_KEY}"}}, {}
    ))
    model = APIModel({
        "api_base": server.base + "/v1",
        "api_model": "test-only",
        "api_key": FAKE_KEY,
    })

    with pytest.raises(RuntimeError) as error:
        model.complete("system", "prompt")
    assert f"HTTP {status}" in str(error.value)
    captured = capsys.readouterr()
    emitted = str(error.value) + captured.out + captured.err
    assert FAKE_KEY not in emitted
    assert PRIVATE_RESPONSE not in emitted


@pytest.mark.parametrize("payload", [
    f"invalid JSON {PRIVATE_RESPONSE} {FAKE_KEY}".encode(),
    {"unexpected": f"{PRIVATE_RESPONSE} {FAKE_KEY}"},
    {"choices": []},
    {"choices": [{"message": {"content": {"unexpected": f"{PRIVATE_RESPONSE} {FAKE_KEY}"}}}]},
])
def test_invalid_success_response_is_rejected_without_echoing_content(local_api, payload, capsys):
    server = local_api(lambda request: (200, payload, {}))
    model = APIModel({
        "api_base": server.base + "/v1",
        "api_model": "test-only",
        "api_key": FAKE_KEY,
    })

    with pytest.raises(RuntimeError) as error:
        model.complete("system", "prompt")
    assert "响应无效" in str(error.value)
    captured = capsys.readouterr()
    emitted = str(error.value) + captured.out + captured.err
    assert FAKE_KEY not in emitted
    assert PRIVATE_RESPONSE not in emitted
