import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from local_llm_api.app import create_app
from local_llm_api.config import Settings
from local_llm_api.memory import MemoryStore, build_context

KEY = "memory-test-secret-at-least-16-characters"
AUTH = {"Authorization": f"Bearer {KEY}"}


def settings(**kwargs):
    kwargs.setdefault("ollama_base_url", "http://192.168.0.105:11434")
    return Settings(_env_file=None, api_key=KEY, **kwargs)


def completion(content):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


def create_session(client, user="alice", system="請用繁體中文回答"):
    response = client.post("/v1/sessions", headers=AUTH, json={"user_id": user, "system_prompt": system})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def turn(client, session, text, **kwargs):
    return client.post("/v1/chat/completions", headers=AUTH, json={
        "user_id": "alice", "session_id": session,
        "messages": [{"role": "user", "content": text}], **kwargs,
    })


def history(client, session, user="alice"):
    return client.get(f"/v1/users/{user}/sessions/{session}", headers=AUTH)


def test_session_replays_inputs_and_outputs_after_restart():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=completion("先建立 API 規格"))

    config = settings()
    with TestClient(create_app(config, transport=httpx.MockTransport(handler))) as client:
        sid = create_session(client)
        assert turn(client, sid, "專案叫海風").status_code == 200
    # A fresh application instance opens the same SQLite file.
    with TestClient(create_app(config, transport=httpx.MockTransport(handler))) as client:
        response = turn(client, sid, "你剛才建議什麼？")
        assert response.status_code == 200
        assert response.headers["X-Session-Id"] == sid
        assert calls[-1]["messages"] == [
            {"role": "system", "content": "請用繁體中文回答"},
            {"role": "user", "content": "專案叫海風"},
            {"role": "assistant", "content": "先建立 API 規格"},
            {"role": "user", "content": "你剛才建議什麼？"},
        ]
        assert "session_id" not in calls[-1] and "user_id" not in calls[-1]
        record = history(client, sid).json()
        assert record["turn_count"] == 2
        assert record["turns"][0]["assistant_content"] == "先建立 API 規格"


def test_user_namespace_and_session_deletion():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=completion("答覆")))
    with TestClient(create_app(settings(), transport=transport)) as client:
        sid = create_session(client)
        other = create_session(client, "bob")
        assert turn(client, sid, "問題").status_code == 200
        assert history(client, sid, "bob").status_code == 404
        assert turn(client, other, "不能存取別人的對話").status_code == 404
        assert client.delete(f"/v1/users/bob/sessions/{sid}", headers=AUTH).status_code == 404
        assert [s["id"] for s in client.get("/v1/users/alice/sessions", headers=AUTH).json()["data"]] == [sid]
        path = f"/v1/users/alice/sessions/{sid}/summary"
        assert client.post(path, headers=AUTH, json={}).status_code == 200
        assert client.delete(f"/v1/users/alice/sessions/{sid}", headers=AUTH).status_code == 204
        assert history(client, sid).status_code == 404
        assert client.get(path, headers=AUTH).status_code == 404
        with sqlite3.connect(settings().memory_db_path) as db:
            assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM summaries").fetchone()[0] == 0


@pytest.mark.parametrize("extra", [
    {"user_id": "alice"}, {"session_id": "fake"},
    {"user_id": "alice", "session_id": "fake", "messages": [{"role": "assistant", "content": "x"}]},
    {"user_id": "alice", "session_id": "fake", "messages": [{"role": "user", "content": "x"}] * 2},
])
def test_session_requires_owner_and_only_new_user_message(extra):
    with TestClient(create_app(settings())) as client:
        response = client.post("/v1/chat/completions", headers=AUTH,
                               json={"messages": [{"role": "user", "content": "x"}], **extra})
        assert response.status_code == 422


def test_failed_generation_is_not_stored_and_stateless_is_unchanged():
    failed = True

    def handler(request):
        return httpx.Response(500) if failed else httpx.Response(200, json=completion("answer"))

    app = create_app(settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        sid = create_session(client)
        assert turn(client, sid, "first").status_code == 502
        assert history(client, sid).json()["turn_count"] == 0
        failed = False
        response = client.post("/v1/chat/completions", headers=AUTH,
                               json={"messages": [{"role": "user", "content": "stateless"}]})
        assert response.status_code == 200 and "session_id" not in response.json()
        assert history(client, sid).json()["turn_count"] == 0
        assert app.state.inflight == 0 and not app.state.active_sessions


def test_context_is_bounded_but_summary_reads_early_assistant_outputs():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json=completion("OLD_OUTPUT" if len(calls) == 1 else "current output"))

    with TestClient(create_app(settings(memory_context_turns=1), transport=httpx.MockTransport(handler))) as client:
        sid = create_session(client)
        for text in ["EARLY_INPUT", "middle", "last"]:
            assert turn(client, sid, text).status_code == 200
        assert "EARLY_INPUT" not in json.dumps(calls[-1])
        path = f"/v1/users/alice/sessions/{sid}/summary"
        response = client.post(path, headers=AUTH, json={"instructions": "包含所有模型輸出"})
        assert response.status_code == 200, response.text
        assert "EARLY_INPUT" in calls[-1]["messages"][1]["content"]
        assert "OLD_OUTPUT" in calls[-1]["messages"][1]["content"]
        assert response.json()["source_turns"] == 3
        assert client.get(path, headers=AUTH).json()["id"] == response.json()["id"]
        assert history(client, sid).json()["turn_count"] == 3
        page = client.get(f"/v1/users/alice/sessions/{sid}?limit=1&offset=1", headers=AUTH).json()
        assert page["turns"][0]["user_content"] == "middle"


def test_summary_map_reduce_covers_entire_transcript():
    config = settings(summary_chunk_chars=256)
    store = MemoryStore(config.memory_db_path)
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json=completion("摘要"))

    with TestClient(create_app(config, transport=httpx.MockTransport(handler))) as client:
        sid = create_session(client)
        store.append_turn("alice", sid, "FIRST" + "甲" * 300, "OUTPUT" + "乙" * 300, "test", "stop")
        store.append_turn("alice", sid, "LAST" + "丙" * 300, "TAIL" + "丁" * 300, "test", "stop")
        response = client.post(f"/v1/users/alice/sessions/{sid}/summary", headers=AUTH, json={})
        assert response.status_code == 200, response.text
        assert response.json()["model_calls"] > 2
        combined = "".join(c["messages"][1]["content"] for c in calls[:-1])
        for marker in ["FIRST", "OUTPUT", "LAST", "TAIL"]:
            assert marker in combined
        assert all(len(c["messages"][1]["content"]) <= 256 for c in calls)


def test_summary_limits_and_empty_conversation():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completion("summary"))
    config = settings(summary_chunk_chars=256, summary_max_calls=1)
    with TestClient(create_app(config, transport=httpx.MockTransport(handler))) as client:
        sid = create_session(client)
        url = f"/v1/users/alice/sessions/{sid}/summary"
        assert client.post(url, headers=AUTH, json={}).status_code == 422
        MemoryStore(config.memory_db_path).append_turn("alice", sid, "x" * 800, "y" * 800, "m", "stop")
        assert client.post(url, headers=AUTH, json={}).status_code == 413
        assert not calls
        assert client.get(url, headers=AUTH).status_code == 404


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        # Intentionally split multibyte Chinese characters and SSE boundaries.
        for i in range(0, len(self.data), 5):
            yield self.data[i:i+5]


@pytest.mark.parametrize("complete", [True, False])
def test_stream_is_stored_only_after_completed_done(complete):
    events = 'data: {"choices":[{"index":0,"delta":{"content":"完整回答"},"finish_reason":null}]}\r\n\r\n'
    if complete:
        events += 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    transport = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                          stream=ChunkedStream(events.encode())))
    app = create_app(settings(), transport=transport)
    with TestClient(app) as client:
        sid = create_session(client)
        response = turn(client, sid, "串流問題", stream=True)
        record = history(client, sid).json()
        assert record["turn_count"] == (1 if complete else 0)
        if complete:
            assert record["turns"][0]["assistant_content"] == "完整回答"
            assert '"error"' not in response.text
        else:
            assert '"error"' in response.text
        assert app.state.inflight == 0 and not app.state.active_sessions


def test_same_session_concurrency_and_delete_are_rejected():
    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json=completion("done"))

        app = create_app(settings(), transport=httpx.MockTransport(handler))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test", headers=AUTH) as client:
                sid = (await client.post("/v1/sessions", json={"user_id": "alice"})).json()["id"]
                body = {"user_id": "alice", "session_id": sid, "messages": [{"role":"user", "content":"one"}]}
                first = asyncio.create_task(client.post("/v1/chat/completions", json=body))
                await entered.wait()
                assert (await client.post("/v1/chat/completions", json=body)).status_code == 409
                assert (await client.delete(f"/v1/users/alice/sessions/{sid}")).status_code == 409
                assert (await client.post(f"/v1/users/alice/sessions/{sid}/summary", json={})).status_code == 409
                finish.set()
                assert (await first).status_code == 200
                assert app.state.inflight == 0 and not app.state.active_sessions
    asyncio.run(run())


def test_context_never_splits_an_old_turn():
    old = [{"user_content": "1111", "assistant_content": "2222"}]
    assert build_context("sys", old, "now", 10) == [
        {"role":"system", "content":"sys"}, {"role":"user", "content":"now"}]
