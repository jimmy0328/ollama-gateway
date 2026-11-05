import hmac
import json
import logging
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .schemas import ChatRequest

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

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url, transport=transport, trust_env=False,
            timeout=httpx.Timeout(settings.request_timeout, connect=5, pool=5),
            limits=httpx.Limits(max_connections=settings.max_inflight + 2),
        ) as client:
            app.state.client = client
            app.state.inflight = 0
            yield

    app = FastAPI(title="Ollama REST API", version="0.2.0", lifespan=lifespan)
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
        if app.state.inflight >= settings.max_inflight:
            raise HTTPException(429, "Model is busy; retry later", headers={"Retry-After": "1"})

        # No await between the check and increment: admission is atomic on this event loop.
        app.state.inflight += 1
        upstream = None
        handed_off = False

        async def release():
            # Starlette cancels stream iteration when the client disconnects.
            with anyio.CancelScope(shield=True):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    app.state.inflight -= 1

        try:
            payload = body.model_dump(exclude_none=True)
            payload["model"] = body.model or settings.model_id
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
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                    except httpx.HTTPError:
                        logger.warning("Model backend stream interrupted")
                        event = {"error": {"message": "Model backend stream interrupted"}}
                        yield f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
                    finally:
                        await release()

                handed_off = True
                return StreamingResponse(events(), media_type="text/event-stream", headers={
                    "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                })

            data = await upstream.aread()
            try:
                result = json.loads(data)
            except ValueError:
                raise HTTPException(502, "Invalid JSON from model backend") from None
            if not isinstance(result, dict) or not result.get("choices"):
                raise HTTPException(502, "Invalid completion from model backend")
            return JSONResponse(result)
        except httpx.TimeoutException:
            raise HTTPException(504, "Model backend timed out") from None
        except httpx.HTTPError:
            raise HTTPException(503, "Model backend unavailable") from None
        finally:
            if not handed_off:
                await release()

    return app
