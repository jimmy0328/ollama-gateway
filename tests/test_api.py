import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_llm_api.app import create_app
from local_llm_api.config import Settings

KEY = "test-secret-with-at-least-16-characters"
AUTH = {"Authorization": f"Bearer {KEY}"}
PROMPT = {"messages": [{"role": "user", "content": "你好"}]}
COMPLETION = {"object": "chat.completion", "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "你好！"}, "finish_reason": "stop"}
]}


def settings(**kwargs):
    kwargs.setdefault("ollama_base_url", "http://192.168.0.105:11434")
    return Settings(_env_file=None, api_key=KEY, **kwargs)


@pytest.fixture
def backend():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.8:27b-mlx"}]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "qwen3.8:27b-mlx", "object": "model"},
                {"id": "qwen2.5-coder:32b", "object": "model"},
            ]})
        payload = json.loads(request.content)
        if payload.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
                'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
                'data: [DONE]\n\n').encode())
        return httpx.Response(200, json=COMPLETION)

    return calls, httpx.MockTransport(handler)


@pytest.mark.parametrize("selected", [None, "qwen2.5-coder:32b"])
def test_completion_and_model_selection(backend, selected):
    calls, transport = backend
    with TestClient(create_app(settings(), transport=transport)) as client:
        payload = {**PROMPT, **({"model": selected} if selected else {})}
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200
        assert response.json() == COMPLETION
        assert json.loads(calls[-1].content)["model"] == (selected or "qwen3.8:27b-mlx")
        assert calls[-1].url.host == "192.168.0.105"
        assert calls[-1].url.port == 11434
        assert "authorization" not in calls[-1].headers


def test_auth_and_discovery(backend):
    calls, transport = backend
    with TestClient(create_app(settings(), transport=transport)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"Authorization": b"Bearer wrong-key"}).status_code == 401
        assert [m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]] == [
            "qwen3.8:27b-mlx", "qwen2.5-coder:32b"]
        assert client.get("/readyz", headers=AUTH).status_code == 200


@pytest.mark.parametrize("extra, status", [
    ({"model": ""}, 422),
    ({"draft_model": "unapproved-repo"}, 422),
    ({"max_tokens": -1}, 422),
    ({"max_tokens": 5000}, 422),
    ({"temperature": 3}, 422),
    ({"messages": []}, 422),
    ({"messages": [{"role": "user", "content": [{"image_url": "http://internal"}]}]}, 422),
    ({"stop": [""]}, 422),
    ({"stream_options": {"include_usage": True}}, 422),
])
def test_invalid_inputs_do_not_reach_backend(backend, extra, status):
    calls, transport = backend
    with TestClient(create_app(settings(), transport=transport)) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json={**PROMPT, **extra})
        assert response.status_code == status
        assert not calls


def test_stream_and_release(backend):
    calls, transport = backend
    app = create_app(settings(), transport=transport)
    with TestClient(app) as client:
        with client.stream("POST", "/v1/chat/completions", headers=AUTH,
                           json={**PROMPT, "stream": True}) as response:
            data = b"".join(response.iter_bytes()).decode()
            assert response.status_code == 200
            assert "你好" in data and "data: [DONE]" in data
            assert response.headers["content-type"].startswith("text/event-stream")
        assert app.state.inflight == 0


@pytest.mark.parametrize("failure, code", [("timeout", 504), ("connect", 503), ("http", 502), ("json", 502)])
def test_backend_failure_and_release(failure, code):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private error", request=request)
        if failure == "connect":
            raise httpx.ConnectError("private error", request=request)
        if failure == "http":
            return httpx.Response(500, text="private error")
        return httpx.Response(200, content=b"not json")

    app = create_app(settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json=PROMPT)
        assert response.status_code == code
        assert "private error" not in response.text
        assert app.state.inflight == 0


def test_missing_model_is_not_ready():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"models": []}))
    with TestClient(create_app(settings(), transport=transport)) as client:
        assert client.get("/readyz", headers=AUTH).status_code == 503


def test_body_and_prompt_limits(backend):
    calls, transport = backend
    with TestClient(create_app(settings(max_body_bytes=200, max_prompt_chars=3), transport=transport)) as client:
        assert client.post("/v1/chat/completions", headers=AUTH,
                           content=iter([b"x" * 100, b"x" * 101])).status_code == 413
        assert client.post("/v1/chat/completions", headers=AUTH,
                           json={"messages": [{"role": "user", "content": "1234"}]}).status_code == 413
        assert not calls


def test_busy_request_is_rejected():
    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=COMPLETION)

        app = create_app(settings(max_inflight=1), transport=httpx.MockTransport(handler))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/chat/completions", headers=AUTH, json=PROMPT))
                await entered.wait()
                second = await client.post("/v1/chat/completions", headers=AUTH, json=PROMPT)
                assert second.status_code == 429
                finish.set()
                assert (await first).status_code == 200
                assert app.state.inflight == 0
    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 404, 429])
def test_ollama_client_error_status_is_preserved(status):
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text="private backend detail"))
    with TestClient(create_app(settings(), transport=transport)) as client:
        response = client.post("/v1/chat/completions", headers=AUTH, json=PROMPT)
        assert response.status_code == status
        assert "private backend detail" not in response.text


def test_reasoning_setting_is_forwarded(backend):
    calls, transport = backend
    with TestClient(create_app(settings(), transport=transport)) as client:
        response = client.post("/v1/chat/completions", headers=AUTH,
                               json={**PROMPT, "reasoning_effort": "none"})
        assert response.status_code == 200
        assert json.loads(calls[-1].content)["reasoning_effort"] == "none"


@pytest.mark.parametrize("url", ["http://192.168.0.105:11434", "https://ollama.example.com/"])
def test_remote_ollama_origin_is_accepted(url):
    assert settings(ollama_base_url=url).ollama_base_url == url.rstrip("/")


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://example.com/v1", "http://user:secret@example.com", "http://example.com:99999"])
def test_invalid_ollama_origin_is_rejected(url):
    with pytest.raises(ValueError):
        settings(ollama_base_url=url)
