"""Calls to CLIProxyAPI with retries, per-model cooldowns and fallback chains.

Responses are opened with ``stream=True`` so SSE is relayed to the client as
it arrives. For streaming requests the first real event is prefetched before
committing: if the upstream answers ``200`` but immediately sends an
``event: error`` (overloaded, quota…), the attempt is still retried or
falls back to the next model instead of failing the client.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from .availability import ModelAvailability
from .config import Settings
from .context import Phase, RequestContext
from .errors import (
    ErrorKind,
    UpstreamError,
    anthropic_error,
    backoff_delay,
    classify_error,
    cooldown_for,
    error_body_from_upstream,
    extract_error_message,
    status_for_error_type,
)
from .reporting import Reporter
from .sse import parse_data_line
from .stats import Stats

# A model that timed out this many times is abandoned for the next one,
# even if more same-model retries are configured (each timeout can be minutes).
MAX_TIMEOUTS_PER_MODEL = 2

# Antigravity Claude 4.x caps completion at 64k. A thinking budget at or above
# that cap is rejected (`max_tokens` must be greater than `thinking.budget_tokens`)
# even when the client set a larger max_tokens. Claude Code on a third-party
# base URL sends Opus 5.5 as extended thinking with budget = upper_limit - 1
# (127999) and max_tokens 128000.
_THINKING_OUTPUT_CAP = 64_000
_MIN_THINKING_BUDGET = 1024


def encode_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# Families that accept Claude Code's 128k thinking budget. Matched on the
# family token so ``opus-4-5`` is not treated as Opus 5.
_UNCAPPED_THINKING_FAMILIES = ("opus-5", "sonnet-5", "haiku-5", "fable", "mythos")


def _thinking_budget_capped(model: str) -> bool:
    """Claude 4.x on this proxy cannot take a thinking budget at the 64k cap."""
    name = model.lower()
    if not name.startswith("claude-"):
        return False
    return not any(family in name for family in _UNCAPPED_THINKING_FAMILIES)


def fit_thinking_budget(body: dict[str, Any]) -> dict[str, Any]:
    """Make ``thinking.budget_tokens < max_tokens`` for models that enforce it.

    Returns ``body`` unchanged when nothing has to move, so the conversation
    payload is not copied on the common path.
    """
    model = body.get("model")
    if not isinstance(model, str) or not _thinking_budget_capped(model):
        return body

    max_tokens = body.get("max_tokens")
    if type(max_tokens) is not int or max_tokens <= 0:
        return body

    thinking = body.get("thinking")
    output = body.get("output_config")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
    if type(budget) is not int:
        budget = None
    effort_max = isinstance(output, dict) and output.get("effort") == "max"
    if budget is None and not effort_max:
        return body

    new_max = max_tokens
    new_budget = budget
    # effort "max" is translated into a budget at the model cap. The request
    # only survives when max_tokens itself stays on that cap.
    if effort_max and new_max > _THINKING_OUTPUT_CAP:
        new_max = _THINKING_OUTPUT_CAP
    if new_budget is not None:
        if new_budget >= _THINKING_OUTPUT_CAP:
            new_budget = _THINKING_OUTPUT_CAP - 1
        if new_budget >= new_max:
            if new_max > _MIN_THINKING_BUDGET:
                new_budget = new_max - 1
            else:
                new_budget = _MIN_THINKING_BUDGET
                new_max = _MIN_THINKING_BUDGET + 1

    if new_max == max_tokens and new_budget == budget:
        return body

    fitted = body
    if new_max != max_tokens:
        fitted = {**fitted, "max_tokens": new_max}
    if new_budget != budget and isinstance(thinking, dict):
        fitted = {**fitted, "thinking": {**thinking, "budget_tokens": new_budget}}
    return fitted


class UpstreamStream:
    """An open upstream SSE response, with the prefetched lines replayed first."""

    def __init__(self, response: httpx.Response, lines: AsyncIterator[str], prefetched: list[str]) -> None:
        self.response = response
        self._lines = lines
        self._prefetched = prefetched
        self._closed = False

    @property
    def status_code(self) -> int:
        return self.response.status_code

    @property
    def closed(self) -> bool:
        return self._closed

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        while self._prefetched:
            yield self._prefetched.pop(0)
        async for line in self._lines:
            yield line

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            await self._lines.aclose()  # type: ignore[attr-defined]
        await self.response.aclose()


@dataclass(slots=True)
class UpstreamSuccess:
    status: int
    stream: UpstreamStream | None = None  # streaming requests
    body: dict[str, Any] | None = None  # non-streaming requests


@dataclass(slots=True)
class UpstreamFailure:
    status: int
    body: dict[str, Any]
    message: str


class UpstreamClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        availability: ModelAvailability,
        stats: Stats,
        reporter: Reporter,
    ) -> None:
        self.settings = settings
        self.client = client
        self.availability = availability
        self.stats = stats
        self.reporter = reporter
        self.messages_url = f"{settings.cpa_base_url}/v1/messages?beta=true"
        self.count_tokens_url = f"{settings.cpa_base_url}/v1/messages/count_tokens?beta=true"

    def _headers(self, *, stream: bool) -> dict[str, str]:
        return {
            "x-api-key": self.settings.upstream_api_key,
            "anthropic-version": self.settings.anthropic_version,
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
        }

    # ------------------------------------------------------------------
    # Messages with retries / fallback
    # ------------------------------------------------------------------

    async def send_messages(
        self, ctx: RequestContext, body: dict[str, Any]
    ) -> UpstreamSuccess | UpstreamFailure:
        settings = self.settings
        max_attempts = settings.attempts_per_model

        candidates, skipped = self.availability.split(ctx.chain)
        if not candidates:
            # Everything is cooling down: try anyway rather than refuse.
            self.reporter.all_models_cooling_down(ctx)
            candidates, skipped = list(ctx.chain), []
        ctx.skipped = skipped

        last_error: UpstreamError | None = None

        for index, model in enumerate(candidates):
            next_model = candidates[index + 1] if index + 1 < len(candidates) else None
            if index > 0:
                ctx.fallbacks += 1
                self.stats.fallbacks += 1
            ctx.model = model
            payload = encode_json(fit_thinking_budget({**body, "model": model}))
            timeouts = 0

            for attempt in range(max_attempts):
                if attempt > 0 and last_error is not None:
                    delay = backoff_delay(attempt - 1, settings, last_error.retry_after)
                    ctx.set_phase(Phase.BACKOFF, f"{attempt + 1}/{max_attempts}")
                    self.reporter.retrying(ctx, model, attempt + 1, max_attempts, delay, last_error)
                    self.stats.retries += 1
                    await asyncio.sleep(delay)

                ctx.attempts += 1
                ctx.set_phase(Phase.WAITING)
                try:
                    return await self._attempt(ctx, payload)
                except UpstreamError as error:
                    self.stats.upstream_errors += 1
                    last_error = error

                if last_error.connect:
                    # CPA itself is unreachable; every model goes through it,
                    # so there is nothing to fall back to — only retry.
                    if attempt + 1 < max_attempts:
                        continue
                    message = f"CLIProxyAPI недоступен ({settings.cpa_base_url}): {last_error.message}"
                    return UpstreamFailure(502, anthropic_error(message, status=502), message)

                if last_error.kind is ErrorKind.FATAL:
                    # The request itself is bad: another model will not help.
                    return UpstreamFailure(last_error.status, last_error.to_body(), last_error.message)

                if last_error.timeout:
                    timeouts += 1

                can_retry = (
                    last_error.kind.retryable
                    and attempt + 1 < max_attempts
                    and timeouts < MAX_TIMEOUTS_PER_MODEL
                )
                if can_retry:
                    continue

                ttl = cooldown_for(last_error.kind, settings)
                if ttl:
                    self.availability.mark_down(model, ttl, last_error.kind.value)
                self.reporter.model_failed(ctx, model, last_error, ttl, next_model)
                break

        assert last_error is not None
        return UpstreamFailure(last_error.status, last_error.to_body(), last_error.message)

    async def _attempt(self, ctx: RequestContext, payload: bytes) -> UpstreamSuccess:
        request = self.client.build_request(
            "POST", self.messages_url, content=payload, headers=self._headers(stream=ctx.stream)
        )
        try:
            response = await self.client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise UpstreamError(
                ErrorKind.TRANSIENT, 502, str(exc).strip() or type(exc).__name__, connect=True
            ) from None
        except httpx.TimeoutException as exc:
            raise UpstreamError(ErrorKind.TRANSIENT, 504, _describe(exc), timeout=True) from None
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.TRANSIENT, 502, _describe(exc)) from None

        keep_open = False
        try:
            if response.status_code >= 400:
                content = await response.aread()
                message = extract_error_message(content) or f"HTTP {response.status_code}"
                raise UpstreamError(
                    classify_error(response.status_code, message),
                    response.status_code,
                    message,
                    body=error_body_from_upstream(content, response.status_code),
                    retry_after=response.headers.get("retry-after"),
                )

            if ctx.stream:
                stream = await self._prefetch(ctx, response)
                keep_open = True
                return UpstreamSuccess(response.status_code, stream=stream)

            content = await response.aread()
            body = _decode_json_object(content)
            if body is None:
                raise UpstreamError(ErrorKind.TRANSIENT, 502, "upstream вернул не-JSON ответ")
            if body.get("type") == "error":
                raise _error_from_event(body.get("error"))
            ctx.first_event_at = time.perf_counter()
            return UpstreamSuccess(response.status_code, body=body)

        except httpx.TimeoutException as exc:
            raise UpstreamError(ErrorKind.TRANSIENT, 504, _describe(exc), timeout=True) from None
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.TRANSIENT, 502, _describe(exc)) from None
        finally:
            if not keep_open:
                await response.aclose()

    async def _prefetch(self, ctx: RequestContext, response: httpx.Response) -> UpstreamStream:
        """Buffer lines until the first meaningful event (or an error event)."""
        lines = response.aiter_lines()
        buffered: list[str] = []
        committed = False
        try:
            async for line in lines:
                buffered.append(line)
                data = parse_data_line(line)
                if data is None:
                    continue
                event = data.get("type")
                if event == "error":
                    raise _error_from_event(data.get("error"))
                if event == "ping":
                    continue
                ctx.first_event_at = time.perf_counter()
                committed = True
                return UpstreamStream(response, lines, buffered)
            raise UpstreamError(ErrorKind.TRANSIENT, 502, "upstream закрыл stream без событий")
        finally:
            if not committed:
                await lines.aclose()

    # ------------------------------------------------------------------
    # count_tokens: single attempt, no fallback
    # ------------------------------------------------------------------

    async def count_tokens(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            response = await self.client.post(
                self.count_tokens_url,
                content=encode_json(fit_thinking_budget(body)),
                headers=self._headers(stream=False),
            )
        except httpx.HTTPError as exc:
            message = f"CLIProxyAPI: {_describe(exc)}"
            return 502, anthropic_error(message, status=502)

        body_out = _decode_json_object(response.content)
        if body_out is None:
            return response.status_code, error_body_from_upstream(response.content, response.status_code)
        return response.status_code, body_out


def _decode_json_object(content: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(content)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error_from_event(error: Any) -> UpstreamError:
    """UpstreamError from an Anthropic ``{"type": "error", "error": {...}}`` payload."""
    if not isinstance(error, dict):
        error = {"type": "api_error", "message": str(error or "unknown error")}
    error_type = error.get("type")
    message = str(error.get("message") or error_type or "upstream error")
    status = status_for_error_type(error_type)
    return UpstreamError(
        classify_error(status, message),
        status,
        message,
        body={"type": "error", "error": {"type": error_type or "api_error", "message": message}},
    )


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name
