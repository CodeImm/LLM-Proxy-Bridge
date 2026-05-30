"""
LLM Proxy — FastAPI reverse-proxy with model fallback, retry и circuit-breaker.

  Client → this proxy (:4000) → upstream (PROXY_URL)

Fallback strategy:
  Каждая модель пробуется SAME_MODEL_RETRIES + 1 раз при retryable-ошибке
  (exponential backoff), затем переход к следующей модели в цепочке.

Circuit-breaker (ModelAvailability):
  Модель, исчерпавшая retries из-за retryable-ошибки или упавшая по сети,
  помечается недоступной на UNAVAILABLE_COOLDOWN секунд.
  При следующих запросах такие модели пропускаются в цепочке.
  Если вся цепочка оказалась недоступна — пробуем её целиком (fail-open).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

# ── Theme ────────────────────────────────────────────────────────────────

_THEME = Theme({
    "info":    "cyan",
    "success": "bold green",
    "warning": "bold yellow",
    "error":   "bold red",
    "model":   "bold magenta",
    "tokens":  "bold cyan",
    "replace": "bold blue",
    "summary": "bold green",
    "muted":   "dim white",
    "req_id":  "dim cyan",
    "tool":    "bold yellow",
    "system":  "dim green",
    "skip":    "dim yellow",
})

console = Console(theme=_THEME, highlight=False)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%H:%M:%S]",
    handlers=[RichHandler(console=console, show_path=False, show_level=False, rich_tracebacks=True)],
)
log = logging.getLogger("proxy")

# ── Config ───────────────────────────────────────────────────────────────

PROXY_URL             = "http://localhost:8317"
SAME_MODEL_RETRIES    = 1      # дополнительных попыток до перехода к следующей модели
RETRY_BASE_DELAY      = 1.0    # секунды
RETRY_MAX_DELAY       = 10.0   # секунды
UNAVAILABLE_COOLDOWN  = 3600.0   # секунды до повторной проверки недоступной модели

# Доступные модели (по dashboard):
#
#   🤖  gemini-3-flash-agent     (~3.2s)   — агент, Gemini 3 Flash
#   🔋  gemini-3-flash           (~3.4s)   — быстрый, дешёвый
#   🔋  gemini-3.5-flash-low     (~3.5s)   — Flash Low
#   🥉  claude-haiku-4-5-20251001(~5.3s)   — Haiku 4.5
#   🔋  gemini-3.1-flash-lite    (~5.6s)   — Flash Lite
#   ⚡  gemini-3-pro-low         (~5.6s)   — Pro Low
#   🧠  claude-opus-4-6-thinking (~6.1s)   — Opus 4.6 Think
#   🤖  gemini-pro-agent         (~6.4s)   — Pro агент
#   ⚡  gemini-3-pro-high        (~6.7s)   — Pro High
#   🥈  claude-sonnet-4-6        (~7.3s)   — Sonnet 4.6
#   ⚡  gemini-3.1-pro-low       (~7.5s)   — Gemini 3.1 Pro Low

MODEL_FALLBACKS: dict[str, list[str]] = {
    # ── Claude Opus / thinking ───────────────────────────────────────────
    "claude-opus-4-6-thinking": [
        "claude-opus-4-6-thinking",
        "claude-sonnet-4-6",
        # "gemini-3-pro-high",
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3-flash",
    ],
    "claude-opus": [
        "claude-opus-4-6-thinking",
        "claude-sonnet-4-6",
        # "gemini-3-pro-high",
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3-flash",
    ],

    # ── Claude Sonnet ────────────────────────────────────────────────────
    "claude-sonnet-4-6": [
        "claude-sonnet-4-6",
        # "gemini-3-pro-high",
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3.5-flash-low",
        "gemini-3-flash",
    ],
    "claude-sonnet": [
        "claude-sonnet-4-6",
        # "gemini-3-pro-high",
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3.5-flash-low",
        "gemini-3-flash",
    ],
    "claude": [
        "claude-sonnet-4-6",
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3.5-flash-low",
        "gemini-3-flash",
    ],

    # ── Claude Haiku ─────────────────────────────────────────────────────
    "claude-haiku-4-5-20251001": [
        "claude-haiku-4-5-20251001",
        "gemini-3.5-flash-low",
        "gemini-3.1-flash-lite",
        "gemini-3-flash",
    ],
    "claude-haiku": [
        "claude-haiku-4-5-20251001",
        "gemini-3.5-flash-low",
        "gemini-3.1-flash-lite",
        "gemini-3-flash",
    ],

    # ── Gemini Pro ───────────────────────────────────────────────────────
    "gemini-pro-agent": [
        "gemini-pro-agent",
        # "gemini-3-pro-high",
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3-flash",
    ],
    "gemini-3-pro-high": [
        # "gemini-3-pro-high",
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3-flash",
    ],
    "gemini-3.1-pro-low": [
        "gemini-3.1-pro-low",
        # "gemini-3-pro-low",
        "gemini-3-flash",
    ],
    "gemini-3-pro-low": [
        # "gemini-3-pro-low",
        "gemini-3.5-flash-low",
        "gemini-3-flash",
    ],

    # ── Gemini Flash ─────────────────────────────────────────────────────
    "gemini-3-flash-agent": [
        "gemini-3-flash-agent",
        "gemini-3-flash",
    ],
    "gemini-3.5-flash-low": [
        "gemini-3.5-flash-low",
        "gemini-3.1-flash-lite",
        "gemini-3-flash",
    ],
    "gemini-3.1-flash-lite": [
        "gemini-3.1-flash-lite",
        "gemini-3-flash",
    ],
    "gemini-3-flash": [
        "gemini-3-flash",
    ],
}

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset(
    {408, 409, 429, 500, 502, 503, 504, 529}
)

RETRYABLE_ERROR_TEXTS = (
    "overloaded",
    "rate limit",
    "capacity",
    "timeout",
    "temporarily unavailable",
    "invalid_grant",
    "resource exhausted",
    "quota",
    "too many requests",
    "backend error",
    "internal error",
    "model is overloaded",
    "service unavailable",
)

UPSTREAM_HEADERS = {
    "x-api-key":         "sk-antigravity",
    "anthropic-version": "2023-06-01",
    "content-type":      "application/json",
    "accept":            "text/event-stream",
}

# ── Model availability (circuit-breaker) ─────────────────────────────────

class ModelAvailability:
    """
    Хранит временные метки недоступности моделей.

    Модель помечается недоступной при:
      - исчерпании retries из-за retryable-ошибки (overloaded / rate-limit)
      - сетевой ошибке (ConnectError / TimeoutException)

    Модель НЕ помечается недоступной при:
      - non-retryable HTTP-ошибке (4xx) — это ошибка запроса, а не модели

    Через UNAVAILABLE_COOLDOWN секунд модель автоматически становится
    доступной снова (lazy-expiry при следующем обращении).
    """

    def __init__(self, cooldown: float = UNAVAILABLE_COOLDOWN) -> None:
        self._down: dict[str, float] = {}   # model → monotonic timestamp
        self.cooldown = cooldown

    def mark_down(self, model: str) -> None:
        self._down[model] = time.monotonic()

    def is_up(self, model: str) -> bool:
        ts = self._down.get(model)
        if ts is None:
            return True
        if time.monotonic() - ts >= self.cooldown:
            del self._down[model]
            return True
        return False

    def filter_chain(self, chain: list[str]) -> list[str]:
        """Возвращает только доступные модели из цепочки."""
        return [m for m in chain if self.is_up(m)]

    def cooldowns_snapshot(self) -> dict[str, float]:
        """Оставшееся время cooldown для каждой недоступной модели (секунды)."""
        now = time.monotonic()
        return {
            m: round(max(0.0, self.cooldown - (now - ts)), 1)
            for m, ts in list(self._down.items())
            if now - ts < self.cooldown
        }


availability = ModelAvailability()

# ── Session stats ────────────────────────────────────────────────────────

@dataclass
class Stats:
    requests:      int   = 0
    errors:        int   = 0
    input_tokens:  int   = 0
    output_tokens: int   = 0
    total_ms:      float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record(self, *, in_tok: int, out_tok: int, elapsed_ms: float) -> None:
        self.requests      += 1
        self.input_tokens  += in_tok
        self.output_tokens += out_tok
        self.total_ms      += elapsed_ms


stats = Stats()

# ── HTTP client ──────────────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialised")
    return _http_client


# ── Helpers ──────────────────────────────────────────────────────────────

def model_chain(model: str) -> list[str]:
    """Возвращает полную fallback-цепочку для модели."""
    return MODEL_FALLBACKS.get(model, [model])


def is_retryable(status: int, body_text: str) -> bool:
    if status in RETRYABLE_STATUS_CODES:
        return True
    return any(t in body_text.lower() for t in RETRYABLE_ERROR_TEXTS)


def backoff_delay(attempt: int, response: httpx.Response | None = None) -> float:
    """Exponential backoff с jitter. Уважает заголовок Retry-After."""
    if response is not None:
        ra = response.headers.get("retry-after")
        if ra:
            try:
                return min(float(ra), RETRY_MAX_DELAY)
            except ValueError:
                pass
    base = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    return base + random.uniform(0, base * 0.2)


def parse_upstream_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return body.get("error", {}).get("message") or json.dumps(body)
    except Exception:
        return resp.text[:200]


def msg_summary(messages: list) -> str:
    counts: dict[str, int] = {}
    for m in messages:
        role = m.get("role", "?")
        counts[role] = counts.get(role, 0) + 1
    return "  ".join(f"{r}×{n}" for r, n in counts.items())


def tools_summary(tools: list | None) -> str | None:
    if not tools:
        return None
    names = [t.get("name") or t.get("function", {}).get("name", "?") for t in tools]
    suffix = "…" if len(names) > 5 else ""
    return f"{len(names)} tool(s): {', '.join(names[:5])}{suffix}"


# ── Schema sanitisation ──────────────────────────────────────────────────

def sanitise_body(body: dict) -> dict:
    """
    - additionalProperties → False (JSON Schema, требование ряда провайдеров)
    - tool_choice: null    → удаляется
    """
    result = _deep_fix(body)
    assert isinstance(result, dict)
    if result.get("tool_choice") is None:
        result.pop("tool_choice", None)
    return result


def _deep_fix(obj: object) -> object:
    if isinstance(obj, dict):
        return {
            k: False if k == "additionalProperties" else _deep_fix(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_deep_fix(i) for i in obj]
    return obj


# ── SSE usage extraction ─────────────────────────────────────────────────

def usage_from_sse_line(line: str) -> dict | None:
    if not line.startswith("data: "):
        return None
    payload = line[6:].strip()
    if payload in ("", "[DONE]"):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    t = data.get("type", "")
    if t == "message_start":
        return data.get("message", {}).get("usage")
    if t == "message_delta":
        return data.get("usage")
    return None


# ── Upstream call: retry + fallback + availability ────────────────────────

async def call_upstream(
    req_id: str,
    body: dict,
    original_model: str,
    chain: list[str],
) -> tuple[httpx.Response | None, str | None]:
    """
    Пробует каждую модель из chain (с учётом availability).

    Возвращает (response, used_model).
    used_model is None если все попытки провалились.
    """
    client = get_client()
    resp: httpx.Response | None = None

    # Фильтруем цепочку, убирая временно недоступные модели.
    # Если все модели недоступны — fail-open: пробуем исходную цепочку
    # (лучше попробовать, чем сразу вернуть 503).
    effective_chain = availability.filter_chain(chain)
    if not effective_chain:
        skipped = [m for m in chain if not availability.is_up(m)]
        log.warning(
            f"[[req_id]{req_id}[/req_id]] "
            f"[warning]⚠ все модели в цепочке недоступны, "
            f"пробуем без фильтра[/warning]  "
            f"[skip]{', '.join(skipped)}[/skip]"
        )
        effective_chain = chain
    elif len(effective_chain) < len(chain):
        skipped = [m for m in chain if m not in effective_chain]
        log.info(
            f"[[req_id]{req_id}[/req_id]] "
            f"[skip]⊘ пропущено (cooldown): {', '.join(skipped)}[/skip]"
        )

    chain_len = len(effective_chain)

    for chain_idx, candidate in enumerate(effective_chain):
        body["model"] = candidate
        next_candidate = (
            effective_chain[chain_idx + 1]
            if chain_idx + 1 < chain_len
            else None
        )
        chain_pos = f"[muted][{chain_idx + 1}/{chain_len}][/muted]"

        for attempt in range(SAME_MODEL_RETRIES + 1):
            if attempt > 0:
                delay = backoff_delay(attempt - 1, resp)
                log.warning(
                    f"[[req_id]{req_id}[/req_id]] "
                    f"[warning]⏱ backoff {delay:.1f}s[/warning] "
                    f"[muted]{candidate}[/muted] (attempt {attempt + 1})"
                )
                await asyncio.sleep(delay)

            # ── Network call ──────────────────────────────────────────────

            try:
                resp = await client.post(
                    f"{PROXY_URL}/v1/messages?beta=true",
                    json=body,
                    headers=UPSTREAM_HEADERS,
                )
            except httpx.ConnectError as exc:
                stats.errors += 1
                availability.mark_down(candidate)
                log.error(
                    f"[[req_id]{req_id}[/req_id]] "
                    f"[error]✗ connect error → marked down[/error] "
                    f"[muted]{candidate}[/muted] — {exc}"
                )
                resp = None
                break  # connect error — нет смысла повторять на той же модели

            except httpx.TimeoutException:
                stats.errors += 1
                availability.mark_down(candidate)
                log.warning(
                    f"[[req_id]{req_id}[/req_id]] "
                    f"[warning]⏱ timeout → marked down[/warning] "
                    f"[muted]{candidate}[/muted]"
                )
                resp = None
                break  # timeout → сразу к следующей модели

            # ── Success ───────────────────────────────────────────────────

            if resp.status_code < 400:
                if chain_idx > 0:
                    log.info(
                        f"[[req_id]{req_id}[/req_id]] "
                        f"[success]✓ fallback OK[/success] "
                        f"[muted]{original_model}[/muted] → "
                        f"[model]{candidate}[/model]"
                    )
                elif attempt > 0:
                    log.info(
                        f"[[req_id]{req_id}[/req_id]] "
                        f"[success]✓ retry OK[/success] "
                        f"[muted]{candidate}[/muted]"
                    )
                return resp, candidate

            # ── Error ─────────────────────────────────────────────────────

            err_msg   = parse_upstream_error(resp)
            retryable = is_retryable(resp.status_code, err_msg)
            is_last   = attempt == SAME_MODEL_RETRIES

            if retryable and not is_last:
                log.warning(
                    f"[[req_id]{req_id}[/req_id]] "
                    f"[warning]↺ retry[/warning] {chain_pos} "
                    f"[muted]{candidate}[/muted] "
                    f"HTTP {resp.status_code}  [muted]{err_msg[:120]}[/muted]"
                )
                continue  # ещё одна попытка на той же модели

            # Последняя попытка или non-retryable → переход к следующей модели
            if retryable:
                # Исчерпали retries из-за retryable-ошибки — помечаем недоступной
                availability.mark_down(candidate)
                action = (
                    f"fallback → {next_candidate}" if next_candidate else "give up"
                )
                down_msg = f"  [skip](cooldown {UNAVAILABLE_COOLDOWN:.0f}s)[/skip]"
            else:
                # 4xx и прочие non-retryable — НЕ помечаем модель недоступной,
                # это ошибка конкретного запроса
                action   = "skip (non-retryable)"
                down_msg = ""

            log.warning(
                f"[[req_id]{req_id}[/req_id]] "
                f"[warning]↺ {action}[/warning] {chain_pos} "
                f"[muted]{candidate}[/muted] "
                f"HTTP {resp.status_code}  [muted]{err_msg[:120]}[/muted]"
                f"{down_msg}"
            )
            break  # к следующей модели в chain

    return resp, None  # все попытки исчерпаны


# ── Model limits ─────────────────────────────────────────────────────────

def _fmt_tokens(val: int | None) -> str:
    """Форматирует количество токенов: 200000 → '200K', 1048576 → '1.0M'."""
    if val is None:
        return "[muted]—[/muted]"
    if val >= 1_000_000:
        return f"[tokens]{val / 1_000_000:.1f}M[/tokens]"
    if val >= 1_000:
        return f"[tokens]{val // 1_000}K[/tokens]"
    return f"[tokens]{val}[/tokens]"


async def fetch_model_limits() -> list[dict]:
    """
    GET /v1/models → список доступных моделей с лимитами токенов.

    Поддерживает форматы ответа:
      - Anthropic: {"data": [{id, display_name, context_window, max_output_tokens}]}
      - Flat list:  [{id, ...}]
    """
    try:
        resp = await get_client().get(
            f"{PROXY_URL}/v1/models",
            headers={k: v for k, v in UPSTREAM_HEADERS.items() if k != "accept"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or data.get("models") or []
    except httpx.HTTPStatusError as exc:
        log.warning(
            f"[warning]⚠ /v1/models → HTTP {exc.response.status_code}[/warning]  "
            f"[muted]лимиты недоступны[/muted]"
        )
    except Exception as exc:
        log.warning(
            f"[warning]⚠ /v1/models недоступен[/warning]  [muted]{exc}[/muted]"
        )
    return []


def _model_limits_table(models: list[dict]) -> Table:
    """Строит Rich-таблицу из списка моделей."""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="bright_blue",
        show_lines=False,
        pad_edge=True,
        expand=False,
    )
    table.add_column("Model",   style="bold magenta", no_wrap=True)
    table.add_column("Display", style="dim white",    no_wrap=True)
    table.add_column("Context", justify="right")
    table.add_column("Output",  justify="right")

    # Поля могут называться по-разному у разных провайдеров
    def _ctx(m: dict) -> int | None:
        return (
            m.get("context_window")
            or m.get("input_token_limit")
            or m.get("max_input_tokens")
        )

    def _out(m: dict) -> int | None:
        return (
            m.get("max_output_tokens")
            or m.get("output_token_limit")
        )

    for m in sorted(models, key=lambda x: x.get("id", "")):
        ctx = _ctx(m)
        out = _out(m)
        display = m.get("display_name") or m.get("name") or ""
        # Не дублируем display если совпадает с id
        display_cell = display if display != m.get("id", "") else ""
        table.add_row(
            m.get("id", "?"),
            display_cell,
            _fmt_tokens(ctx),
            _fmt_tokens(out),
        )

    return table


# ── Lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=15.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )

    console.print(Panel(
        f"Target   → [bold cyan]{PROXY_URL}[/bold cyan]\n"
        f"Port     → [bold cyan]4000[/bold cyan]\n"
        f"Cooldown → [bold cyan]{UNAVAILABLE_COOLDOWN:.0f}s[/bold cyan]",
        title="[bold white]🚀  LLM Proxy[/bold white]",
        border_style="bright_blue",
    ))

    models = await fetch_model_limits()
    if models:
        console.print(
            Panel(
                _model_limits_table(models),
                title=f"[bold cyan]📋  Models ({len(models)})[/bold cyan]",
                border_style="bright_blue",
                expand=False,
            )
        )
    else:
        console.print(
            "[muted]  ℹ  Лимиты моделей недоступны — upstream не ответил на GET /v1/models[/muted]"
        )

    yield

    await _http_client.aclose()

    n = max(stats.requests, 1)
    console.print(Panel(
        f"Requests      : [bold]{stats.requests}[/bold] "
        f"([error]{stats.errors} errors[/error])\n"
        f"Input tokens  : [tokens]{stats.input_tokens:,}[/tokens]\n"
        f"Output tokens : [tokens]{stats.output_tokens:,}[/tokens]\n"
        f"Total tokens  : [tokens]{stats.total_tokens:,}[/tokens] "
        f"([muted]avg {stats.total_tokens / n:,.0f}/req[/muted])\n"
        f"Avg latency   : [muted]{stats.total_ms / n:,.0f} ms[/muted]",
        title="[summary]📊  Session Summary[/summary]",
        border_style="green",
    ))


app = FastAPI(lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status":            "ok",
        "requests":          stats.requests,
        "errors":            stats.errors,
        "total_tokens":      stats.total_tokens,
        "unavailable_models": availability.cooldowns_snapshot(),
    }


# ── Handler ───────────────────────────────────────────────────────────────

@app.post("/v1/messages", response_model=None)
async def messages(request: Request) -> StreamingResponse | JSONResponse:
    req_id  = uuid.uuid4().hex[:8].upper()
    t_start = time.perf_counter()

    # ── Parse + sanitise ──────────────────────────────────────────────────

    try:
        body = sanitise_body(await request.json())
    except Exception:
        log.exception(f"[[req_id]{req_id}[/req_id]] [error]invalid JSON[/error]")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    original_model = body.get("model", "unknown")
    chain          = model_chain(original_model)
    msgs           = body.get("messages", [])
    tools          = body.get("tools")
    is_stream      = body.get("stream", False)

    # ── Log request ───────────────────────────────────────────────────────

    model_part = (
        f"[replace]↺[/replace] [muted]{original_model}[/muted] → [model]{chain[0]}[/model]"
        if chain[0] != original_model
        else f"[model]{chain[0]}[/model]"
    )
    extras = []
    if body.get("system"):
        extras.append("[system]sys✓[/system]")
    if ts := tools_summary(tools):
        extras.append(f"[tool]{ts}[/tool]")
    if is_stream:
        extras.append("[muted]stream[/muted]")

    log.info(
        f"[[req_id]{req_id}[/req_id]] ▶  "
        f"{model_part}  "
        f"[muted]{msg_summary(msgs)}[/muted]"
        + ("  " + "  ".join(extras) if extras else "")
    )

    # ── Upstream call ─────────────────────────────────────────────────────

    resp, used_model = await call_upstream(req_id, body, original_model, chain)

    # ── Network-level failure ─────────────────────────────────────────────

    if resp is None:
        stats.errors += 1
        return JSONResponse({"error": "All upstream attempts failed"}, status_code=502)

    # ── Upstream-level failure ────────────────────────────────────────────

    if used_model is None:
        stats.errors += 1
        err_msg = parse_upstream_error(resp)
        log.error(
            f"[[req_id]{req_id}[/req_id]] "
            f"[error]✗ upstream failed[/error] "
            f"[model]{body['model']}[/model] "
            f"HTTP {resp.status_code}  [muted]{err_msg}[/muted]  "
            f"({(time.perf_counter() - t_start) * 1000:.0f} ms)"
        )
        try:
            err_body = resp.json()
        except Exception:
            err_body = {"error": resp.text[:500]}
        return JSONResponse(err_body, status_code=resp.status_code)

    # ── Streaming response ────────────────────────────────────────────────

    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        captured_model = used_model  # захватываем до входа в генератор

        async def stream_gen() -> AsyncIterator[bytes]:
            in_tok = out_tok = 0
            try:
                async for line in resp.aiter_lines():
                    yield (line + "\n").encode()
                    if usage := usage_from_sse_line(line):
                        in_tok  = usage.get("input_tokens",  in_tok)
                        out_tok = usage.get("output_tokens", out_tok)
            except Exception as exc:
                yield (
                    f"event: error\n"
                    f"data: {json.dumps({'error': str(exc)})}\n\n"
                ).encode()
                log.error(
                    f"[[req_id]{req_id}[/req_id]] "
                    f"[error]✗ stream error[/error] {exc}"
                )
                stats.errors += 1
            finally:
                elapsed = (time.perf_counter() - t_start) * 1000
                stats.record(in_tok=in_tok, out_tok=out_tok, elapsed_ms=elapsed)
                log.info(
                    f"[[req_id]{req_id}[/req_id]] ◀  "
                    f"[model]{captured_model}[/model]  "
                    f"[tokens]in {in_tok:,} │ out {out_tok:,} │ "
                    f"Σ {in_tok + out_tok:,}[/tokens]  "
                    f"[muted]{elapsed:,.0f} ms[/muted]"
                )

        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers={
                "cache-control":     "no-cache",
                "connection":        "keep-alive",
                "x-accel-buffering": "no",
            },
        )

    # ── Non-streaming response ────────────────────────────────────────────

    elapsed = (time.perf_counter() - t_start) * 1000

    try:
        resp_body = resp.json()
    except Exception:
        stats.errors += 1
        log.error(
            f"[[req_id]{req_id}[/req_id]] [error]✗ invalid JSON from upstream[/error]"
        )
        return JSONResponse({"error": "Invalid upstream response"}, status_code=502)

    usage   = resp_body.get("usage", {})
    in_tok  = usage.get("input_tokens",  0)
    out_tok = usage.get("output_tokens", 0)

    stats.record(in_tok=in_tok, out_tok=out_tok, elapsed_ms=elapsed)
    log.info(
        f"[[req_id]{req_id}[/req_id]] ◀  "
        f"[model]{used_model}[/model]  "
        f"[tokens]in {in_tok:,} │ out {out_tok:,} │ "
        f"Σ {in_tok + out_tok:,}[/tokens]  "
        f"[muted]{elapsed:,.0f} ms[/muted]"
    )

    return JSONResponse(resp_body, status_code=resp.status_code)


# ── Entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4000, log_level="warning")