"""HTTP handlers for the Anthropic Messages API surface of the bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from .availability import ModelAvailability
from .context import Outcome, Phase, RequestContext, Result
from .errors import anthropic_error
from .reporting import Reporter
from .routing import Router
from .sse import StreamObserver, encode_sse_error
from .stats import Stats
from .upstream import UpstreamClient, UpstreamFailure, UpstreamStream

log = logging.getLogger("llm_bridge")

STATUS_CLIENT_CLOSED = 499

# Top-level keys that hold conversation content and must never be rewritten.
_CONVERSATION_KEYS = frozenset({"messages", "system", "metadata"})


# ============================================================================
# Request body helpers
# ============================================================================


class BadRequest(Exception):
    pass


async def read_json_object(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        raise BadRequest("пустое тело запроса")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise BadRequest(f"невалидный JSON: {exc}") from None
    if not isinstance(body, dict):
        raise BadRequest("тело запроса должно быть JSON-объектом")
    return body


def _force_closed_schemas(value: Any) -> Any:
    """Set every ``additionalProperties`` to ``False`` (Gemini-compatible schemas)."""
    if isinstance(value, dict):
        return {
            key: False if key == "additionalProperties" else _force_closed_schemas(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_force_closed_schemas(item) for item in value]
    return value


def sanitise_body(body: dict[str, Any]) -> dict[str, Any]:
    """Normalise schemas (tools, output formats) without touching the conversation."""
    result = {
        key: value if key in _CONVERSATION_KEYS else _force_closed_schemas(value)
        for key, value in body.items()
    }
    if "tool_choice" in result and result["tool_choice"] is None:
        del result["tool_choice"]
    return result


def error_response(message: str, status: int) -> JSONResponse:
    return JSONResponse(anthropic_error(message, status=status), status_code=status)


# ============================================================================
# Streaming response with guaranteed cleanup
# ============================================================================


class RelayResponse(StreamingResponse):
    """StreamingResponse that runs ``on_finish`` even if the body never started."""

    def __init__(self, content: AsyncIterator[bytes], on_finish: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._on_finish = on_finish

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_finish()


# ============================================================================
# Handlers
# ============================================================================


class MessagesProxy:
    def __init__(
        self,
        router: Router,
        upstream: UpstreamClient,
        availability: ModelAvailability,
        stats: Stats,
        reporter: Reporter,
    ) -> None:
        self.router = router
        self.upstream = upstream
        self.availability = availability
        self.stats = stats
        self.reporter = reporter
        self.active: dict[str, RequestContext] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._count_tokens_errors: set[int] = set()

    # ------------------------------------------------------------------
    # POST /v1/messages
    # ------------------------------------------------------------------

    async def messages(self, request: Request) -> Response:
        try:
            body = sanitise_body(await read_json_object(request))
        except BadRequest as exc:
            log.warning("400 %s: %s", request.url.path, exc)
            return error_response(str(exc), 400)

        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            log.warning("400 %s: не указано поле model", request.url.path)
            return error_response("поле 'model' обязательно", 400)

        passthrough = self.router.resolve_passthrough(request.headers)
        tools = body.get("tools")
        ctx = RequestContext(
            original_model=model,
            chain=self.router.chain(model, passthrough=passthrough),
            passthrough=passthrough,
            stream=bool(body.get("stream")),
            has_system=bool(body.get("system")),
            tool_count=len(tools) if isinstance(tools, list) else 0,
        )

        self.stats.requests += 1
        self.active[ctx.req_id] = ctx
        self.reporter.request_started(ctx)

        try:
            result = await self.upstream.send_messages(ctx, body)

            if isinstance(result, UpstreamFailure):
                self._finish(ctx, Outcome(Result.FAILED, result.status, error=result.message))
                return JSONResponse(result.body, status_code=result.status)

            if result.stream is not None:
                stream = result.stream
                return RelayResponse(
                    self._relay(ctx, stream),
                    on_finish=lambda: self._finish(
                        ctx, Outcome(Result.CANCELLED, STATUS_CLIENT_CLOSED), stream
                    ),
                    status_code=result.status,
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-bridge-request-id": ctx.req_id},
                )

            response_body = result.body or {}
            ctx.usage.update(response_body.get("usage"))
            self._finish(ctx, Outcome(Result.OK, result.status))
            return JSONResponse(
                response_body,
                status_code=result.status,
                headers={"x-bridge-request-id": ctx.req_id},
            )

        except asyncio.CancelledError:
            self._finish(ctx, Outcome(Result.CANCELLED, STATUS_CLIENT_CLOSED))
            raise
        except Exception as exc:
            log.exception("[%s] необработанная ошибка при обработке запроса", ctx.req_id)
            self._finish(ctx, Outcome(Result.FAILED, 500, error=f"{type(exc).__name__}: {exc}"))
            return error_response(f"внутренняя ошибка bridge: {type(exc).__name__}: {exc}", 500)

    async def _relay(self, ctx: RequestContext, stream: UpstreamStream) -> AsyncIterator[bytes]:
        observer = StreamObserver(ctx)
        outcome: Outcome | None = None
        try:
            async for line in stream:
                if observer.feed(line):
                    self.reporter.phase_changed(ctx)
                if ctx.phase is Phase.THINKING:
                    self.reporter.progress(ctx)
                yield (line + "\n").encode("utf-8")

            if observer.error is not None:
                outcome = Outcome(Result.FAILED, stream.status_code, error=observer.error_message)
            elif not observer.message_stopped:
                outcome = Outcome(Result.OK, stream.status_code, warning="stream завершился без message_stop")
            else:
                outcome = Outcome(Result.OK, stream.status_code)

        except (asyncio.CancelledError, GeneratorExit):
            outcome = Outcome(Result.CANCELLED, STATUS_CLIENT_CLOSED)
            raise
        except Exception as exc:
            description = f"{type(exc).__name__}: {exc}".rstrip(": ")
            if not isinstance(exc, httpx.HTTPError):
                log.exception("[%s] ошибка при ретрансляции stream", ctx.req_id)
            outcome = Outcome(Result.FAILED, stream.status_code, error=f"stream прерван: {description}")
            # Headers are already sent: tell the client in-band so it can retry.
            yield encode_sse_error(f"bridge: upstream stream прерван ({description})")
        finally:
            self._finish(ctx, outcome or Outcome(Result.CANCELLED, STATUS_CLIENT_CLOSED), stream)

    # ------------------------------------------------------------------
    # POST /v1/messages/count_tokens
    # ------------------------------------------------------------------

    async def count_tokens(self, request: Request) -> Response:
        try:
            body = sanitise_body(await read_json_object(request))
        except BadRequest as exc:
            return error_response(str(exc), 400)

        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            return error_response("поле 'model' обязательно", 400)

        passthrough = self.router.resolve_passthrough(request.headers)
        chain = self.router.chain(model, passthrough=passthrough)
        available, _ = self.availability.split(chain)
        target = (available or chain)[0]

        status, payload = await self.upstream.count_tokens({**body, "model": target})
        if status >= 400 and status not in self._count_tokens_errors:
            self._count_tokens_errors.add(status)
            log.warning(
                "count_tokens → HTTP %s (%s); повторные ошибки с этим кодом скрыты",
                status,
                target,
            )
        return JSONResponse(payload, status_code=status)

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def _finish(self, ctx: RequestContext, outcome: Outcome, stream: UpstreamStream | None = None) -> None:
        """Record the outcome exactly once and release the upstream stream."""
        if stream is not None and not stream.closed:
            self._spawn(stream.aclose())
        if self.active.pop(ctx.req_id, None) is None:
            return

        ctx.set_phase(Phase.DONE)
        stats = self.stats
        stats.add_usage(ctx.usage, ctx.model or (ctx.chain[0] if ctx.chain else ctx.original_model))
        if outcome.result is Result.OK:
            stats.succeeded += 1
            stats.success_ms += ctx.elapsed * 1000
        elif outcome.result is Result.CANCELLED:
            stats.cancelled += 1
        else:
            stats.failed += 1

        try:
            self.reporter.request_finished(ctx, outcome)
        except Exception:
            log.exception("reporter failed")

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run cleanup outside the (possibly cancelled) request task."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no running loop (interpreter shutdown)
            coro.close()
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def wait_idle(self, timeout: float) -> None:
        """Wait until no request is in flight (or the timeout expires)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.active and loop.time() < deadline:
            await asyncio.sleep(0.05)

    def abandon_all(self) -> None:
        """Record every still-active request as cancelled (hard shutdown)."""
        for ctx in list(self.active.values()):
            self._finish(ctx, Outcome(Result.CANCELLED, STATUS_CLIENT_CLOSED))

    async def drain(self, timeout: float = 2.0) -> None:
        """Wait for pending stream-close tasks (called on shutdown)."""
        if self._background:
            await asyncio.wait(set(self._background), timeout=timeout)
