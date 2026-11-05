import hmac
import json
import logging
import sqlite3
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .schemas import ChatRequest, SessionCreate, SummaryRequest
from .memory import MemoryStore, SessionNotFound, build_context
from .conversation import StreamTurn, completion_text, sse_frames, summarize

logger = logging.getLogger(__name__)


class BodyLimit:
    """Bound actual bytes even when Content-Length is absent or inaccurate."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                response = JSONResponse({"error": {"message": "Request body too large"}}, 413)
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None, *, transport=None) -> FastAPI:
    settings = settings or Settings()
    store = MemoryStore(settings.memory_db_path)

    async def db(method, *args):
        return await anyio.to_thread.run_sync(method, *args)

    @asynccontextmanager
    async def lifespan(app):
        await db(store.initialize)
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url, transport=transport, trust_env=False,
            timeout=httpx.Timeout(settings.request_timeout, connect=5, pool=5),
            limits=httpx.Limits(max_connections=settings.max_inflight + 2),
        ) as client:
            app.state.client = client
            app.state.inflight = 0
            app.state.active_sessions = set()
            yield

    app = FastAPI(title="Ollama REST API", version="0.3.0", lifespan=lifespan)
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), settings.api_key.get_secret_value().encode()
        ):
            raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": {"message": exc.detail}}, exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Avoid echoing potentially sensitive prompt text in validation errors.
        return JSONResponse({"error": {"message": "Invalid request", "details": [
            {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
        ]}}, 422)

    @app.exception_handler(SessionNotFound)
    async def session_not_found(request, exc):
        return JSONResponse({"error": {"message": "Session not found for this user"}}, 404)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        logger.error("Conversation storage failed: %s", type(exc).__name__)
        return JSONResponse({"error": {"message": "Conversation storage unavailable"}}, 503)

    def claim(key=None):
        if key is not None and key in app.state.active_sessions:
            raise HTTPException(409, "This session has an active request; retry after it finishes")
        if app.state.inflight >= settings.max_inflight:
            raise HTTPException(429, "Model is busy; retry later", headers={"Retry-After": "1"})
        app.state.inflight += 1
        if key is not None:
            app.state.active_sessions.add(key)

    def unclaim(key=None):
        app.state.inflight -= 1
        if key is not None:
            app.state.active_sessions.discard(key)

    @app.post("/v1/sessions", status_code=201, dependencies=[Depends(authenticate)])
    async def create_session(body: SessionCreate):
        if len(body.system_prompt) >= settings.max_prompt_chars:
            raise HTTPException(422, "system_prompt exceeds context budget")
        return await db(store.create_session, body.user_id, body.system_prompt)

    @app.get("/v1/users/{user_id}/sessions", dependencies=[Depends(authenticate)])
    async def list_sessions(user_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return {"data": await db(store.list_sessions, user_id, limit, offset)}

    @app.get("/v1/users/{user_id}/sessions/{session_id}", dependencies=[Depends(authenticate)])
    async def session_history(user_id: str, session_id: str,
                              limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        result = await db(store.get_session, user_id, session_id)
        result["turns"] = await db(store.get_turns, user_id, session_id, limit, offset)
        return result

    @app.delete("/v1/users/{user_id}/sessions/{session_id}", status_code=204, dependencies=[Depends(authenticate)])
    async def delete_session(user_id: str, session_id: str):
        key = (user_id, session_id)
        if key in app.state.active_sessions:
            raise HTTPException(409, "Cannot delete a session with an active request")
        app.state.active_sessions.add(key)
        try:
            await db(store.delete_session, user_id, session_id)
        finally:
            app.state.active_sessions.discard(key)
        return Response(status_code=204)

    @app.get("/v1/users/{user_id}/sessions/{session_id}/summary", dependencies=[Depends(authenticate)])
    async def latest_summary(user_id: str, session_id: str):
        result = await db(store.latest_summary, user_id, session_id)
        if result is None:
            raise HTTPException(404, "No summary has been generated")
        return result

    @app.post("/v1/users/{user_id}/sessions/{session_id}/summary", dependencies=[Depends(authenticate)])
    async def create_summary(user_id: str, session_id: str, body: SummaryRequest):
        if body.max_tokens > settings.max_tokens:
            raise HTTPException(422, f"max_tokens must be <= {settings.max_tokens}")
        key = (user_id, session_id)
        claim(key)
        try:
            turns = await db(store.get_turns, user_id, session_id)
            if not turns:
                raise HTTPException(422, "Session has no completed conversation turns")
            content, calls, reason = await summarize(app.state.client, settings, body, turns)
            return await db(store.save_summary, user_id, session_id, content,
                            body.model or settings.model_id, body.instructions, turns, calls, reason)
        except httpx.TimeoutException:
            raise HTTPException(504, "Ollama summary request timed out") from None
        except httpx.HTTPError:
            raise HTTPException(503, "Ollama unavailable while summarizing") from None
        finally:
            unclaim(key)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz", dependencies=[Depends(authenticate)])
    async def ready():
        """Checks backend health and model registration, not a warm inference."""
        try:
            response = await app.state.client.get("/api/tags", timeout=5)
            response.raise_for_status()
            names = {m["name"] for m in response.json()["models"]}
            if settings.model_id not in names:
                raise HTTPException(503, "Default model is not installed in Ollama")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "Model backend unavailable") from None
        return {"status": "ok", "backend": "ollama", "default_model": settings.model_id}

    @app.get("/v1/models", dependencies=[Depends(authenticate)])
    async def models():
        try:
            response = await app.state.client.get("/v1/models", timeout=5)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValueError("Invalid model list")
            return result
        except httpx.TimeoutException:
            raise HTTPException(504, "Ollama timed out") from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "Ollama model list unavailable") from None

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
    async def chat(body: ChatRequest):
        if body.max_tokens > settings.max_tokens:
            raise HTTPException(422, f"max_tokens must be <= {settings.max_tokens}")
        if sum(len(m.content) for m in body.messages) > settings.max_prompt_chars:
            raise HTTPException(413, "Conversation exceeds MAX_PROMPT_CHARS")
        key = (body.user_id, body.session_id) if body.session_id is not None else None
        claim(key)
        upstream = None
        handed_off = False
        released = False

        async def release():
            nonlocal released
            if released:
                return
            released = True
            # Starlette cancels stream iteration when the client disconnects.
            with anyio.CancelScope(shield=True):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    unclaim(key)

        async def persist(content, reason):
            await db(store.append_turn, body.user_id, body.session_id, body.messages[0].content,
                     content, body.model or settings.model_id, reason)

        try:
            payload = body.model_dump(exclude_none=True, exclude={"session_id", "user_id"})
            payload["model"] = body.model or settings.model_id
            if key is not None:
                session = await db(store.get_session, body.user_id, body.session_id)
                turns = await db(store.get_turns, body.user_id, body.session_id,
                                 settings.memory_context_turns, 0, True)
                try:
                    payload["messages"] = build_context(session["system_prompt"], turns,
                                                        body.messages[0].content, settings.max_prompt_chars)
                except ValueError as exc:
                    raise HTTPException(413, str(exc)) from None
            request = app.state.client.build_request("POST", "/v1/chat/completions", json=payload)
            upstream = await app.state.client.send(request, stream=True)
            if upstream.is_error:
                # Do not expose backend filesystem paths or traceback text.
                code = upstream.status_code if 400 <= upstream.status_code < 500 else 502
                raise HTTPException(code, f"Ollama returned HTTP {upstream.status_code}")

            if body.stream:
                if "text/event-stream" not in upstream.headers.get("content-type", ""):
                    raise HTTPException(502, "Model backend did not return an SSE stream")

                async def events():
                    try:
                        if key is not None:
                            turn = StreamTurn(settings.max_tokens * 64)
                            async for lines in sse_frames(upstream):
                                done = turn.read(lines)
                                if done:
                                    await persist("".join(turn.parts), turn.reason)
                                    # Persist and release before sending terminal [DONE].
                                    await release()
                                yield ("\n".join(lines) + "\n\n").encode()
                                if done:
                                    return
                            raise ValueError("Stream ended before [DONE]")
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                    except (httpx.HTTPError, ValueError, sqlite3.Error, SessionNotFound, TypeError, KeyError) as exc:
                        logger.warning("Model stream or session persistence failed: %s", type(exc).__name__)
                        event = {"error": {"message": "Model stream or conversation storage failed; turn was not saved"}}
                        yield f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
                    finally:
                        await release()

                handed_off = True
                headers = {
                    "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                }
                if key is not None:
                    headers["X-Session-Id"] = body.session_id
                return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

            data = await upstream.aread()
            try:
                result = json.loads(data)
            except ValueError:
                raise HTTPException(502, "Invalid JSON from model backend") from None
            if not isinstance(result, dict) or not result.get("choices"):
                raise HTTPException(502, "Invalid completion from model backend")
            if key is not None:
                content, reason = completion_text(result)
                await persist(content, reason)
                result["session_id"] = body.session_id
                return JSONResponse(result, headers={"X-Session-Id": body.session_id})
            return JSONResponse(result)
        except httpx.TimeoutException:
            raise HTTPException(504, "Model backend timed out") from None
        except httpx.HTTPError:
            raise HTTPException(503, "Model backend unavailable") from None
        finally:
            if not handed_off:
                await release()

    return app
