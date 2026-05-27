import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ─── Тема и консоль ────────────────────────────────────────────────────────────

THEME = Theme({
    "info":      "cyan",
    "success":   "bold green",
    "warning":   "bold yellow",
    "error":     "bold red",
    "model":     "bold magenta",
    "demoted":   "bold red strike",
    "promoted":  "bold green",
    "arrow.out": "bold blue",
    "arrow.in":  "bold green",
    "muted":     "dim white",
    "req_id":    "dim cyan",
})

console = Console(theme=THEME, highlight=False)

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%H:%M:%S]",
    handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
)
log = logging.getLogger("proxy")

# ─── Конфигурация ──────────────────────────────────────────────────────────────

PROXY_URL = "http://localhost:8317"

# ─── Маппинг моделей (актуальный по результатам теста) ───────────────────────
MODEL_FALLBACK: dict[str, str] = {
    # Claude
    "claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
    "claude-opus":              "claude-opus-4-6-thinking",
    "claude-3-opus":            "claude-opus-4-6-thinking",

    "claude-sonnet-4-6":        "claude-sonnet-4-6",
    "claude-sonnet":            "claude-sonnet-4-6",
    "claude-3.5-sonnet":        "claude-sonnet-4-6",

    "claude-haiku-4-5-20251001": "gemini-3.5-flash-low",   # Haiku → Gemini
    "claude-haiku":              "gemini-3.5-flash-low",
    "claude":                    "gemini-3.5-flash-low",

    # Gemini — приоритет на рабочие
    "gemini-3-pro-high":         "gemini-3.1-pro-low",
    "gemini-3-pro":              "gemini-3.1-pro-low",
    "gemini-pro":                "gemini-3.1-pro-low",
    "gemini-3-pro-low":          "gemini-3.1-pro-low",
    "gemini-3.1-pro-low":        "gemini-3.1-pro-low",

    "gemini-pro-agent":          "gemini-pro-agent",
    "gemini-3-flash-agent":      "gemini-3-flash-agent",

    "gemini-3.5-flash-low":      "gemini-3.5-flash-low",
    "gemini-3.5-flash":          "gemini-3.5-flash-low",
    "gemini-3-flash":            "gemini-3-flash",
    "gemini-3.1-flash-lite":     "gemini-3.1-flash-lite",
}

# Цепочка деградации (от самой мощной к самой быстрой/стабильной)
MODEL_CHAIN: list[str] = [
    "claude-opus-4-6-thinking",
    "claude-sonnet-4-6",
    "gemini-3.1-pro-low",
    "gemini-pro-agent",
    "gemini-3-flash-agent",
    "gemini-3.5-flash-low",
    "gemini-3-flash",
    "gemini-3.1-flash-lite",
]

# ─── Тир-бейджи ───────────────────────────────────────────────────────────────

MODEL_TIER: dict[str, str] = {
    "claude-opus-4-6-thinking": "🥇 OPUS",
    "claude-sonnet-4-6":        "🥈 SONNET",
    "gemini-3.1-pro-low":       "⚡ GEMINI-PRO",
    "gemini-pro-agent":         "🤖 AGENT",
    "gemini-3-flash-agent":     "🤖 FLASH-AGENT",
    "gemini-3.5-flash-low":     "🔋 FLASH",
    "gemini-3-flash":           "🔋 FLASH-3",
    "gemini-3.1-flash-lite":    "🔋 LITE",
}


def model_badge(model: str) -> str:
    return MODEL_TIER.get(model, f"❓ {model[:20]}")


# ─── Состояние деградации ─────────────────────────────────────────────────────

_skipped: set[str] = set()


def next_available(current: str) -> str | None:
    try:
        idx = MODEL_CHAIN.index(current)
    except ValueError:
        return None
    for candidate in MODEL_CHAIN[idx + 1:]:
        if candidate not in _skipped:
            return candidate
    return None


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column(style="white")
    table.add_row("Target", PROXY_URL)
    table.add_row("Chain length", str(len(MODEL_CHAIN)))
    table.add_row("Listen", "http://127.0.0.1:4000")
    table.add_row("Degradation", "Enabled (invalid_grant → next)")

    console.print(
        Panel(
            table,
            title="[bold white]🚀 LLM Proxy[/bold white]",
            border_style="bright_blue",
            padding=(1, 3),
        )
    )
    yield
    console.print("[muted]Proxy stopped.[/muted]")


app = FastAPI(lifespan=lifespan)

# ─── Schema Fix ───────────────────────────────────────────────────────────────

def fix_schema(obj):
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == "additionalProperties":
                result[k] = False
            elif k == "tools" and isinstance(v, list):
                result[k] = [
                    {tk: fix_function(tv) if tk == "function" else tv
                     for tk, tv in tool.items()} if isinstance(tool, dict) else tool
                    for tool in v
                ]
            elif k == "tool_choice" and v is None:
                continue
            elif k == "parameters" and isinstance(v, dict):
                result[k] = fix_parameters(v)
            else:
                result[k] = fix_schema(v)
        return result
    if isinstance(obj, list):
        return [fix_schema(i) for i in obj]
    return obj


def fix_function(func):
    return {k: fix_parameters(v) if k == "parameters" else v
            for k, v in func.items()} if isinstance(func, dict) else func


def fix_parameters(params):
    return {k: False if k == "additionalProperties" else fix_schema(v)
            for k, v in params.items()} if isinstance(params, dict) else params


def _is_invalid_grant(resp: httpx.Response) -> bool:
    if resp.status_code != 400:
        return False
    try:
        return resp.json().get("error") == "invalid_grant"
    except:
        return False


# ─── Upstream с деградацией ───────────────────────────────────────────────────

async def call_upstream(req_id: str, fixed_body: dict, headers: dict):
    current_model = fixed_body["model"]

    while True:
        fixed_body["model"] = current_model
        t0 = time.perf_counter()

        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/messages?beta=true",
                json=fixed_body,
                headers=headers,
            )

        if not _is_invalid_grant(resp):
            return resp, current_model

        # Деградация
        _skipped.add(current_model)
        nxt = next_available(current_model)

        log.warning(
            f"[req_id][[{req_id}]] [warning]⚠ invalid_grant[/warning] "
            f"[demoted]{model_badge(current_model)}[/demoted]"
        )

        if nxt is None:
            log.error(f"[req_id][[{req_id}]] [error]Цепочка моделей исчерпана[/error]")
            return resp, current_model

        log.warning(
            f"[req_id][[{req_id}]] [arrow.out]↓[/arrow.out] Переход → [promoted]{model_badge(nxt)}[/promoted]"
        )
        current_model = nxt


# ─── Главный обработчик ───────────────────────────────────────────────────────

@app.post("/v1/messages")
async def messages(request: Request):
    req_id = uuid.uuid4().hex[:8].upper()
    t_start = time.perf_counter()

    try:
        body = await request.json()
        fixed_body = fix_schema(body)
    except Exception:
        log.exception(f"[req_id][[{req_id}]] Невалидный JSON")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    original_model = fixed_body.get("model", "unknown")
    target_model = MODEL_FALLBACK.get(original_model, original_model)

    # Если модель уже в скипе — сразу берём следующую
    if target_model in _skipped:
        recovered = next_available(target_model)
        if recovered:
            target_model = recovered

    fixed_body["model"] = target_model

    if target_model != original_model:
        log.info(f"[req_id][[{req_id}]] [arrow.out]↺[/arrow.out] Замена {original_model} → [model]{model_badge(target_model)}[/model]")
    else:
        log.info(f"[req_id][[{req_id}]] [arrow.out]→[/arrow.out] Модель [model]{model_badge(target_model)}[/model]")

    headers = {
        "x-api-key": "sk-antigravity",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }

    resp, final_model = await call_upstream(req_id, fixed_body, headers)
    elapsed = (time.perf_counter() - t_start) * 1000

    if resp.status_code == 200:
        mode = "stream" if "text/event-stream" in resp.headers.get("content-type", "") else "json"
        log.info(f"[req_id][[{req_id}]] [arrow.in]←[/arrow.in] [success]200 OK[/success] {mode} {elapsed:.0f}ms")
    else:
        log.error(f"[req_id][[{req_id}]] [arrow.in]←[/arrow.in] [error]{resp.status_code} ERROR[/error] {elapsed:.0f}ms")

    # Стриминг
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        async def stream_generator():
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk

        return StreamingResponse(stream_generator(),
                               media_type="text/event-stream",
                               headers={"cache-control": "no-cache", "connection": "keep-alive", "x-accel-buffering": "no"})

    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except:
        return JSONResponse({"error": "Invalid upstream response"}, status_code=502)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4000, log_level="info")