"""
LLM Proxy — FastAPI reverse-proxy for Claude Code → CLIProxyAPI.

Policy (default):
  • Heavy Claude requests → Prefer Opus, then Sonnet (Claude-only, no Gemini).
  • Haiku (simple CC tasks) → rewrite to gemini-3.1-pro-low.
  • Same-model retries give CPA time to rotate accounts on rate-limit/quota.
  • Quota inspector runs at startup and on an interval.

Passthrough (no rewrite / no model fallback chain):
  • Env: BRIDGE_PASSTHROUGH=1
  • Per-request: header X-Bridge-Passthrough: 1  (or X-Bridge-Policy: passthrough)
  • Header 0/false overrides global passthrough for that request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, AsyncIterator, Literal

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

_REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(_REPO_ROOT / ".env")

# ── Theme ────────────────────────────────────────────────────────────────

_THEME = Theme({
    "info":     "cyan",
    "success":  "bold green",
    "warning":  "bold yellow",
    "error":    "bold red",
    "model":    "bold magenta",
    "tokens":   "bold cyan",
    "replace":  "bold blue",
    "summary":  "bold green",
    "muted":    "dim white",
    "req_id":   "dim cyan",
    "tool":     "bold yellow",
    "system":   "dim green",
    "skip":     "dim yellow",
    "header":   "bold bright_white",
    "badge":    "bold cyan",
    "bar.high": "green",
    "bar.mid":  "yellow",
    "bar.low":  "red",
    "bar.empty": "dim white",
    "kv.key":   "dim cyan",
    "kv.val":   "white",
})

console = Console(theme=_THEME, highlight=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%H:%M:%S]",
    handlers=[RichHandler(
        console=console,
        show_path=False,
        show_level=False,
        rich_tracebacks=True,
        markup=True,
    )],
)
log = logging.getLogger("proxy")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Config (from .env / environment; see .env.example) ───────────────────

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_csv(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


PROXY_URL   = _env("CPA_BASE_URL", "http://127.0.0.1:8317").rstrip("/")
BRIDGE_HOST = _env("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = _env_int("BRIDGE_PORT", 4000)

# When true: never rewrite models, never fall back to another model id.
# Same-model retries + cooldowns still apply to the requested id.
BRIDGE_PASSTHROUGH = _env_bool("BRIDGE_PASSTHROUGH", False)

# Same-model retries let CLIProxyAPI rotate auth accounts on 429/quota.
SAME_MODEL_RETRIES = _env_int("SAME_MODEL_RETRIES", 4)   # → up to N+1 attempts per model
RETRY_BASE_DELAY   = _env_float("RETRY_BASE_DELAY", 1.0)
RETRY_MAX_DELAY    = _env_float("RETRY_MAX_DELAY", 10.0)

# Cooldown TTL by failure class (seconds)
COOLDOWN_RATE      = _env_float("COOLDOWN_RATE", 60.0)       # rate-limit / quota / overloaded
COOLDOWN_AUTH      = _env_float("COOLDOWN_AUTH", 1800.0)     # invalid_grant / 401
COOLDOWN_TRANSIENT = _env_float("COOLDOWN_TRANSIENT", 30.0)  # 5xx / connect / timeout
COOLDOWN_SKIP      = _env_float("COOLDOWN_SKIP", 300.0)      # model not found / not supported

# Prefer Opus for heavy work; Claude-only (no Gemini fallback).
CLAUDE_HEAVY: list[str] = _env_csv("CLAUDE_HEAVY_MODELS", [
    "claude-opus-4-6-thinking",
    "claude-sonnet-4-6",
])

# Haiku (Claude Code simple tasks) → Gemini
HAIKU_TARGET_MODEL = _env("HAIKU_TARGET_MODEL", "gemini-3.1-pro-low")
HAIKU_GEMINI: list[str] = [HAIKU_TARGET_MODEL]
_HAIKU_SOURCE_IDS = _env_csv("HAIKU_SOURCE_MODELS", [
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
    "claude-haiku",
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307",
])
MODEL_REWRITE: dict[str, str] = {mid: HAIKU_TARGET_MODEL for mid in _HAIKU_SOURCE_IDS}

# Aliases / exact ids that map onto CLAUDE_HEAVY
CLAUDE_HEAVY_ALIASES: frozenset[str] = frozenset(_env_csv("CLAUDE_HEAVY_ALIASES", [
    "claude",
    "claude-opus",
    "claude-opus-4-6",
    "claude-opus-4-6-thinking",
    "claude-opus-4-7",
    "claude-sonnet",
    "claude-sonnet-4-6",
    "claude-3-5-sonnet-20241022",
    "claude-3-7-sonnet-20250219",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
]))

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
RATE_LIMIT_TEXTS = (
    "overloaded", "rate limit", "capacity", "resource exhausted", "quota",
    "too many requests", "model is overloaded",
)
AUTH_ERROR_TEXTS = ("invalid_grant", "unauthorized", "unauthenticated", "invalid api key", "authentication")
SKIP_ERROR_TEXTS = ("not found", "not supported", "unknown model", "invalid model", "does not exist")
TRANSIENT_ERROR_TEXTS = (
    "timeout", "temporarily unavailable", "backend error", "internal error", "service unavailable",
)

RETRYABLE_ERROR_TEXTS = RATE_LIMIT_TEXTS + AUTH_ERROR_TEXTS + TRANSIENT_ERROR_TEXTS

UPSTREAM_API_KEY = _env("UPSTREAM_API_KEY", "sk-antigravity")
ANTHROPIC_VERSION = _env("ANTHROPIC_VERSION", "2023-06-01")
UPSTREAM_HEADERS = {
    "x-api-key":         UPSTREAM_API_KEY,
    "anthropic-version": ANTHROPIC_VERSION,
    "content-type":      "application/json",
    "accept":            "text/event-stream",
}

# HTTP client
HTTP_TIMEOUT       = _env_float("HTTP_TIMEOUT", 180.0)
HTTP_CONNECT_TIMEOUT = _env_float("HTTP_CONNECT_TIMEOUT", 15.0)
HTTP_MAX_CONNECTIONS = _env_int("HTTP_MAX_CONNECTIONS", 100)
HTTP_MAX_KEEPALIVE   = _env_int("HTTP_MAX_KEEPALIVE", 20)

# Quota inspector
_quota_inspector_raw = _env("QUOTA_INSPECTOR_PATH", "")
QUOTA_INSPECTOR = (
    Path(_quota_inspector_raw)
    if _quota_inspector_raw
    else _REPO_ROOT / "CLIProxyAPI-Quota-Inspector" / "cpa-quota-inspector.exe"
)
QUOTA_POLL_INTERVAL = _env_float("QUOTA_POLL_INTERVAL", 300.0)  # seconds
QUOTA_INSPECTOR_TIMEOUT = _env_float("QUOTA_INSPECTOR_TIMEOUT", 120.0)
QUOTA_FILTER_MODEL_GROUP = _env("QUOTA_FILTER_MODEL_GROUP", "Claude/GPT")  # set to 'all' for full report
QUOTA_LIVE_DISPLAY = _env_bool("QUOTA_LIVE_DISPLAY", True)  # pinned live updating display
QUOTA_MANAGEMENT_KEY = (
    _env("CPA_MANAGEMENT_KEY")
    or _env("MANAGEMENT_PASSWORD")
    or ""
)

# ── Model Availability ───────────────────────────────────────────────────

class ModelAvailability:
    """Per-model cooldown with reason-specific TTL."""

    def __init__(self) -> None:
        # model → (marked_at monotonic, ttl seconds)
        self._down: dict[str, tuple[float, float]] = {}

    def mark_down(self, model: str, ttl: float) -> None:
        self._down[model] = (time.monotonic(), ttl)

    def is_up(self, model: str) -> bool:
        entry = self._down.get(model)
        if entry is None:
            return True
        ts, ttl = entry
        if time.monotonic() - ts >= ttl:
            self._down.pop(model, None)
            return True
        return False

    def filter_chain(self, chain: list[str]) -> list[str]:
        return [m for m in chain if self.is_up(m)]

    def cooldowns_snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        out: dict[str, float] = {}
        for m, (ts, ttl) in list(self._down.items()):
            remaining = ttl - (now - ts)
            if remaining > 0:
                out[m] = round(remaining, 1)
            else:
                self._down.pop(m, None)
        return out


availability = ModelAvailability()

# ── Stats + last quota snapshot ──────────────────────────────────────────

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
        self.requests += 1
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.total_ms += elapsed_ms


stats = Stats()
last_quota: dict[str, Any] = {}
quota_live: Live | None = None

# ── HTTP Client ──────────────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None

def get_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialised")
    return _http_client

# ── Helpers ──────────────────────────────────────────────────────────────

def resolve_passthrough(request: Request | None = None) -> bool:
    """
    Whether to skip model rewrite / multi-model fallback for this call.

    Priority: per-request header → global BRIDGE_PASSTHROUGH.
      X-Bridge-Passthrough: 1|true|yes|on  → force passthrough
      X-Bridge-Passthrough: 0|false|no|off → force rewrite policy
      X-Bridge-Policy: passthrough|raw|direct → force passthrough
      X-Bridge-Policy: rewrite|default      → force rewrite policy
    """
    if request is not None:
        raw = (request.headers.get("x-bridge-passthrough") or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        policy = (request.headers.get("x-bridge-policy") or "").strip().lower()
        if policy in ("passthrough", "raw", "direct", "none"):
            return True
        if policy in ("rewrite", "default", "policy"):
            return False
    return BRIDGE_PASSTHROUGH


def model_chain(model: str, *, passthrough: bool = False) -> list[str]:
    """Resolve rewrite + fallback chain for a requested model id."""
    if passthrough:
        return [model]

    if model in MODEL_REWRITE:
        return list(HAIKU_GEMINI)

    # Prefix match for haiku (covers future dated ids)
    lower = model.lower()
    if "haiku" in lower:
        return list(HAIKU_GEMINI)

    if model in CLAUDE_HEAVY_ALIASES or lower.startswith("claude-opus") or lower.startswith("claude-sonnet"):
        return list(CLAUDE_HEAVY)

    if lower == "claude" or lower.startswith("claude-"):
        # Other Claude ids still Prefer Opus path
        return list(CLAUDE_HEAVY)

    # Gemini / unknown: passthrough (no forced Claude rewrite)
    return [model]


def classify_error(status: int, body_text: str) -> Literal["rate", "auth", "skip", "transient", "fatal"]:
    text = body_text.lower()
    if status in (401, 403) or any(t in text for t in AUTH_ERROR_TEXTS):
        return "auth"
    if status == 404 or any(t in text for t in SKIP_ERROR_TEXTS):
        return "skip"
    if status == 429 or any(t in text for t in RATE_LIMIT_TEXTS):
        return "rate"
    if status in RETRYABLE_STATUS_CODES or any(t in text for t in TRANSIENT_ERROR_TEXTS):
        return "transient"
    return "fatal"


def cooldown_for(kind: Literal["rate", "auth", "skip", "transient", "fatal"]) -> float | None:
    return {
        "rate": COOLDOWN_RATE,
        "auth": COOLDOWN_AUTH,
        "skip": COOLDOWN_SKIP,
        "transient": COOLDOWN_TRANSIENT,
        "fatal": None,
    }[kind]


def is_retryable(status: int, body_text: str) -> bool:
    kind = classify_error(status, body_text)
    return kind in ("rate", "transient", "auth")


def backoff_delay(attempt: int, response: httpx.Response | None = None) -> float:
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
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message") or json.dumps(body, ensure_ascii=False)
        if isinstance(err, str):
            return err
        return json.dumps(body, ensure_ascii=False)
    except Exception:
        return resp.text[:200]


def error_payload(resp: httpx.Response | None, fallback: str = "Upstream failed") -> dict:
    if resp is None:
        return {"error": {"type": "proxy_error", "message": fallback}}
    try:
        body = resp.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    return {"error": {"type": "proxy_error", "message": parse_upstream_error(resp) if resp else fallback}}


def _short_model(m: str) -> str:
    if m.startswith("claude-"):
        return m.removeprefix("claude-").split("-")[0]
    if m.startswith("gemini-"):
        return m.removeprefix("gemini-")
    return m


def sanitise_body(body: dict) -> dict:
    result = _deep_fix(body)
    if result.get("tool_choice") is None:
        result.pop("tool_choice", None)
    return result


def _deep_fix(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: False if k == "additionalProperties" else _deep_fix(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_fix(i) for i in obj]
    return obj


def usage_from_sse_line(line: str) -> dict | None:
    if not line.startswith("data: "):
        return None
    payload = line[6:].strip()
    if payload in ("", "[DONE]"):
        return None
    try:
        data = json.loads(payload)
        t = data.get("type", "")
        if t == "message_start":
            return data.get("message", {}).get("usage")
        if t == "message_delta":
            return data.get("usage")
    except Exception:
        pass
    return None

# ── Quota Inspector ──────────────────────────────────────────────────────

def _run_quota_inspector_sync() -> dict[str, Any] | None:
    if not QUOTA_INSPECTOR.is_file():
        log.warning(
            f"[warning]⚠[/warning] [muted]quota inspector not found: {QUOTA_INSPECTOR}[/muted]"
        )
        return None
    if not QUOTA_MANAGEMENT_KEY:
        log.warning(
            "[warning]⚠[/warning] [muted]quota inspector skipped: set CPA_MANAGEMENT_KEY in .env[/muted]"
        )
        return None

    cmd = [
        str(QUOTA_INSPECTOR),
        "--cpa-base-url", PROXY_URL,
        "-k", QUOTA_MANAGEMENT_KEY,
        "--json",
        "--no-progress",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=QUOTA_INSPECTOR_TIMEOUT,
            cwd=str(_REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        log.warning("[warning]⚠[/warning] [muted]quota inspector timed out[/muted]")
        return None
    except Exception as exc:
        log.warning(f"[warning]⚠[/warning] [muted]quota inspector failed: {exc}[/muted]")
        return None

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        log.warning(
            f"[warning]⚠[/warning] [muted]quota inspector exit {proc.returncode}: {err}[/muted]"
        )
        return None

    raw = (proc.stdout or "").strip()
    if not raw:
        log.warning("[warning]⚠[/warning] [muted]quota inspector returned empty output[/muted]")
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(f"[warning]⚠[/warning] [muted]quota inspector bad JSON: {exc}[/muted]")
        return None


def _bar_style(remaining: float | None, *, exhausted: bool = False) -> str:
    if remaining is None:
        return "bar.empty"
    if exhausted or remaining <= 0:
        return "bar.low"
    if remaining < 25:
        return "bar.low"
    if remaining < 50:
        return "bar.mid"
    return "bar.high"


def _bar_text(
    remaining: float | None,
    width: int = 18,
    *,
    exhausted: bool = False,
) -> Text:
    """Colored unicode progress bar (█/░) + percent."""
    if remaining is None:
        return Text("—", style="muted")
    v = max(0.0, min(100.0, float(remaining)))
    filled = int((v / 100.0 * width) + 0.5)
    filled = min(width, max(0, filled))
    style = _bar_style(v, exhausted=exhausted)
    out = Text()
    out.append("█" * filled, style=style)
    out.append("░" * (width - filled), style="bar.empty")
    out.append(f" {v:3.0f}%", style=style)
    return out


def _status_text(status: str) -> Text:
    s = (status or "unknown").lower()
    if s in ("exhausted", "error"):
        style = "error"
    elif s in ("low", "warning", "degraded"):
        style = "warning"
    elif s in ("full", "ok", "healthy"):
        style = "success"
    else:
        style = "muted"
    return Text(status or "unknown", style=style)


def _kv_table(rows: list[tuple[str, Any]], *, title: str | None = None) -> Table:
    """Compact key/value table for panels."""
    t = Table(
        show_header=False,
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
        title=title,
        title_style="header",
    )
    t.add_column("key", style="kv.key", no_wrap=True, min_width=10)
    t.add_column("val", style="kv.val", overflow="fold")
    for k, v in rows:
        if isinstance(v, Text):
            t.add_row(k, v)
        else:
            t.add_row(k, Text(str(v)))
    return t


def _account_info_line(report: dict[str, Any]) -> str | None:
    """Build info line from meta_fields (channel / project / tier)."""
    meta = report.get("meta_fields") or {}
    if not meta:
        return None
    parts: list[str] = []
    for key in ("channel", "project", "paid_tier", "tier"):
        val = meta.get(key)
        if val:
            parts.append(str(val))
    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return " · ".join(uniq) if uniq else None


def _short_account_name(name: str) -> str:
    """Strip provider prefix / .json for cleaner headers."""
    n = name
    for prefix in ("antigravity-", "gemini-cli-", "codex-"):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    if n.endswith(".json"):
        n = n[:-5]
    return n


def _windows_table(windows: list[dict[str, Any]]) -> Table:
    t = Table(
        box=None,
        show_header=True,
        header_style="header",
        pad_edge=False,
        expand=True,
        padding=(0, 1),
        row_styles=["none", "dim"],
    )
    t.add_column("Model group", style="info", min_width=18, max_width=28, overflow="ellipsis")
    t.add_column("Remaining", min_width=26, no_wrap=True)
    t.add_column("Reset", style="muted", width=12, no_wrap=True)
    for window in windows:
        label = str(window.get("label") or window.get("id") or "?")
        rem = window.get("remaining_percent")
        exhausted = bool(window.get("exhausted"))
        reset = str(window.get("reset_label") or "—")
        rem_f = None if rem is None else float(rem)
        t.add_row(label, _bar_text(rem_f, exhausted=exhausted), reset)
    return t


def _account_block(report: dict[str, Any]) -> Group:
    name = str(report.get("name") or "?")
    plan = str(report.get("plan_type") or "unknown")
    status = str(report.get("status") or "unknown")

    header = Text()
    header.append(_short_account_name(name), style="model")
    header.append("  ")
    header.append(plan, style="badge")
    header.append("  ")
    header.append_text(_status_text(status))

    parts: list[Any] = [header]

    if report.get("error"):
        parts.append(Text(f"error: {report['error']}", style="error"))

    windows = list(report.get("windows") or [])
    windows.extend(report.get("additional_windows") or [])
    if windows:
        parts.append(_windows_table(windows))
    else:
        parts.append(Text("no quota windows", style="muted"))

    info = _account_info_line(report)
    if info:
        parts.append(Text(info, style="muted"))

    return Group(*parts)


def _render_quota_report(data: dict[str, Any]) -> None:
    """Full or compact per-account quota dump — Rich tables + colored bars."""
    summary = data.get("summary") or {}
    reports: list[dict[str, Any]] = data.get("reports") or []
    provider_summaries: list[dict[str, Any]] = data.get("provider_summaries") or []

    exhausted = int(summary.get("exhausted_accounts") or 0)
    low = int(summary.get("low_accounts") or 0)
    border = "red" if exhausted else ("yellow" if low else "green")

    target_group = QUOTA_FILTER_MODEL_GROUP.strip()

    # If QUOTA_FILTER_MODEL_GROUP is set (e.g. 'Claude/GPT'), render a single compact table across all accounts
    if target_group and target_group.lower() != "all":
        target_lower = target_group.lower()
        t = Table(
            box=None,
            show_header=True,
            header_style="header",
            pad_edge=False,
            expand=True,
            padding=(0, 1),
            row_styles=["none", "dim"],
        )
        t.add_column("Account", style="model", min_width=24)
        t.add_column(f"{target_group} Quota", min_width=26, no_wrap=True)
        t.add_column("Reset", style="muted", width=12, no_wrap=True)
        t.add_column("Status", width=10, no_wrap=True)

        for report in reports:
            name = _short_account_name(str(report.get("name") or "?"))
            status = str(report.get("status") or "unknown")
            windows = list(report.get("windows") or [])
            windows.extend(report.get("additional_windows") or [])

            matched_window = None
            for w in windows:
                w_label = str(w.get("label") or w.get("id") or "").lower()
                if target_lower in w_label or w_label in target_lower:
                    matched_window = w
                    break

            if matched_window:
                rem = matched_window.get("remaining_percent")
                exhausted_w = bool(matched_window.get("exhausted"))
                reset = str(matched_window.get("reset_label") or "—")
                rem_f = None if rem is None else float(rem)
                bar = _bar_text(rem_f, exhausted=exhausted_w)
            elif report.get("error"):
                bar = Text(f"error: {report['error'][:30]}", style="error")
                reset = "—"
            else:
                bar = Text("—", style="muted")
                reset = "—"

            t.add_row(name, bar, reset, _status_text(status))

        # Compact summary footer
        eq = summary.get("antigravity_equivalents") or summary.get("gemini_equivalents") or {}
        footer_rows: list[tuple[str, Any]] = [
            ("Accounts", Text(f"{len(reports)}  exhausted={exhausted}  low={low}", style="bold")),
        ]
        found_eq = False
        if eq:
            for k, v in sorted(eq.items()):
                if target_lower in k.lower():
                    footer_rows.append((str(k), Text(f"{v}%", style="info")))
                    found_eq = True
        if not found_eq and eq:
            for k, v in sorted(eq.items()):
                footer_rows.append((str(k), Text(f"{v}%", style="info")))

        render_group = Group(
            Rule("[header]CPA Quota[/header]", style=border),
            Panel(
                t,
                title=f"[header]Compact Quota ({target_group})[/header]  [muted]{len(reports)} accounts[/muted]",
                border_style=border,
                padding=(0, 1),
            ),
            Panel(
                _kv_table(footer_rows),
                title="[summary]Summary[/summary]",
                border_style=border,
                padding=(0, 1),
            ),
        )

        if quota_live is not None and quota_live.is_started:
            quota_live.update(render_group, refresh=True)
        else:
            console.print()
            console.print(render_group)
            console.print()
        return

    # Full report (when QUOTA_FILTER_MODEL_GROUP is 'all')
    render_parts: list[Any] = [Rule("[header]CPA Quota[/header]", style=border)]

    by_provider: dict[str, list[dict[str, Any]]] = {}
    for r in reports:
        by_provider.setdefault(r.get("provider") or "unknown", []).append(r)

    if provider_summaries:
        for ps in provider_summaries:
            title = str(ps.get("title") or ps.get("provider") or "Provider")
            plans = ps.get("plans") or {}
            statuses = ps.get("statuses") or {}
            plan_s = ", ".join(f"{k}:{v}" for k, v in sorted(plans.items())) or "—"
            status_s = ", ".join(f"{k}:{v}" for k, v in sorted(statuses.items())) or "—"
            rows: list[tuple[str, Any]] = [
                ("Accounts", Text(str(ps.get("accounts", 0)), style="bold")),
                ("Plans", plan_s),
                ("Statuses", status_s),
            ]
            for row in ps.get("extras") or []:
                rows.append((str(row.get("label", "?")), Text(str(row.get("value", "")), style="info")))
            render_parts.append(Panel(
                _kv_table(rows),
                title=f"[header]{escape(title)} Summary[/header]",
                border_style=border,
                padding=(0, 1),
            ))
    else:
        accounts = summary.get("accounts", 0)
        providers = summary.get("provider_counts") or {}
        status_counts = summary.get("status_counts") or {}
        provider_line = ", ".join(f"{k}={v}" for k, v in sorted(providers.items())) or "—"
        status_line = ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())) or "—"
        render_parts.append(Panel(
            _kv_table([
                ("Accounts", Text(f"{accounts}  exhausted={exhausted}  low={low}", style="bold")),
                ("Providers", provider_line),
                ("Statuses", status_line),
            ]),
            title="[header]CPA Quota Summary[/header]",
            border_style=border,
            padding=(0, 1),
        ))

    provider_titles = {
        "antigravity": "Antigravity",
        "gemini-cli": "Gemini CLI",
        "codex": "Codex",
    }
    for provider, items in by_provider.items():
        title = provider_titles.get(provider, provider)
        account_parts: list[Any] = []
        for i, report in enumerate(items):
            if i > 0:
                account_parts.append(Rule(style="dim"))
            account_parts.append(_account_block(report))

        body: Any = Group(*account_parts) if account_parts else Text("no accounts", style="muted")
        render_parts.append(Panel(
            body,
            title=f"[header]{escape(title)}[/header]  [muted]{len(items)} accounts[/muted]",
            border_style=border,
            padding=(0, 1),
        ))

    eq = summary.get("antigravity_equivalents") or summary.get("gemini_equivalents") or {}
    footer_rows: list[tuple[str, Any]] = [
        ("Accounts", Text(str(summary.get("accounts", 0)), style="bold")),
        ("Exhausted", Text(str(exhausted), style="error" if exhausted else "muted")),
        ("Low", Text(str(low), style="warning" if low else "muted")),
        ("Errors", Text(str(summary.get("error_accounts", 0)), style="muted")),
    ]
    if eq:
        for k, v in sorted(eq.items()):
            footer_rows.append((str(k), Text(f"{v}%", style="info")))
    render_parts.append(Panel(
        _kv_table(footer_rows),
        title="[summary]Summary[/summary]",
        border_style=border,
        padding=(0, 1),
    ))

    full_renderable = Group(*render_parts)
    if quota_live is not None and quota_live.is_started:
        quota_live.update(full_renderable, refresh=True)
    else:
        console.print()
        console.print(full_renderable)
        console.print()


async def refresh_quota(*, silent: bool = False) -> None:
    global last_quota
    data = await asyncio.to_thread(_run_quota_inspector_sync)
    if data is None:
        return
    summary = data.get("summary") or {}
    reports = data.get("reports") or []
    last_quota = {
        "ts": time.time(),
        "accounts": summary.get("accounts"),
        "exhausted_accounts": summary.get("exhausted_accounts"),
        "low_accounts": summary.get("low_accounts"),
        "error_accounts": summary.get("error_accounts"),
        "status_counts": summary.get("status_counts"),
        "provider_counts": summary.get("provider_counts"),
        "exhausted_names": summary.get("exhausted_names"),
        "low_names": summary.get("low_names"),
        "plan_counts": summary.get("plan_counts"),
        "antigravity_equivalents": summary.get("antigravity_equivalents"),
        "gemini_equivalents": summary.get("gemini_equivalents"),
        # compact per-account snapshot for /health
        "accounts_detail": [
            {
                "name": r.get("name"),
                "provider": r.get("provider"),
                "plan_type": r.get("plan_type"),
                "status": r.get("status"),
                "windows": [
                    {
                        "label": w.get("label") or w.get("id"),
                        "remaining_percent": w.get("remaining_percent"),
                        "reset_label": w.get("reset_label"),
                        "exhausted": w.get("exhausted"),
                    }
                    for w in (r.get("windows") or [])
                ],
            }
            for r in reports
        ],
    }
    if not silent:
        _render_quota_report(data)
    elif summary.get("exhausted_accounts"):
        names = ", ".join((summary.get("exhausted_names") or [])[:5])
        log.warning(
            f"[warning]⚠[/warning] [muted]quota: exhausted={summary['exhausted_accounts']} {names}[/muted]"
        )


async def _quota_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=QUOTA_POLL_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        await refresh_quota(silent=False)

# ── Upstream Result ──────────────────────────────────────────────────────

@dataclass
class UpstreamResult:
    resp:           httpx.Response | None
    used_model:     str | None
    skipped:        list[str] = field(default_factory=list)
    total_attempts: int = 0
    fallback_count: int = 0

# ── Final Log ────────────────────────────────────────────────────────────

def _log_final(
    req_id: str,
    original_model: str,
    result: UpstreamResult,
    in_tok: int,
    out_tok: int,
    elapsed_ms: float,
    *,
    is_stream: bool = False,
    has_system: bool = False,
    tools: list | None = None,
    passthrough: bool = False,
) -> None:
    elapsed_s = elapsed_ms / 1000
    ok = result.used_model is not None
    status = "[success]✓[/success]" if ok else "[error]✗[/error]"

    effective = result.used_model or original_model
    chain_start = model_chain(original_model, passthrough=passthrough)[0]

    if passthrough:
        model_part = f"[model]{effective}[/model] [muted]passthrough[/muted]"
    elif original_model != chain_start:
        model_part = f"[replace]{original_model}[/replace] → [model]{effective}[/model]"
    elif result.fallback_count > 0:
        model_part = f"[muted]{_short_model(chain_start)}[/muted] → [model]{effective}[/model]"
    else:
        model_part = f"[model]{effective}[/model]"

    flags: list[str] = []
    if result.total_attempts > 1:
        flags.append(f"[warning]×{result.total_attempts}[/warning]")
    if result.skipped:
        flags.append(f"[skip]−{','.join(_short_model(m) for m in result.skipped)}[/skip]")
    if is_stream:
        flags.append("[muted]stream[/muted]")
    if has_system:
        flags.append("[system]sys[/system]")
    if tools:
        flags.append(f"[tool]⚒{len(tools)}[/tool]")
    if not ok:
        flags.append("[error]failed[/error]")

    flags_str = (" " + " ".join(flags)) if flags else ""
    tok_part = (
        f"[tokens]↑{in_tok:,} ↓{out_tok:,}[/tokens]"
        if ok and (in_tok or out_tok)
        else "[muted]—[/muted]"
    )

    log.info(
        f"{status} [req_id]{req_id}[/req_id] │ {model_part}{flags_str} │ "
        f"{tok_part} │ [muted]{elapsed_s:.1f}s[/muted]"
    )

# ── Call Upstream ────────────────────────────────────────────────────────

async def call_upstream(
    req_id: str,
    body: dict,
    original_model: str,
    chain: list[str],
) -> UpstreamResult:
    client = get_client()
    skipped: list[str] = []
    total_attempts = 0
    fallback_count = 0

    effective_chain = availability.filter_chain(chain)
    if not effective_chain:
        log.warning(
            f"   [warning]⚠[/warning] [req_id]{req_id}[/req_id] "
            f"[muted]все модели в cooldown, fail-open[/muted]"
        )
        effective_chain = list(chain)
    else:
        skipped = [m for m in chain if not availability.is_up(m)]

    for chain_idx, candidate in enumerate(effective_chain):
        body["model"] = candidate
        next_candidate = (
            effective_chain[chain_idx + 1] if chain_idx + 1 < len(effective_chain) else None
        )

        last_resp: httpx.Response | None = None
        for attempt in range(SAME_MODEL_RETRIES + 1):
            total_attempts += 1

            if attempt > 0:
                delay = backoff_delay(attempt - 1, last_resp)
                log.warning(
                    f"   [warning]↺[/warning] [req_id]{req_id}[/req_id] "
                    f"[muted]{candidate} · backoff {delay:.1f}s · "
                    f"{attempt + 1}/{SAME_MODEL_RETRIES + 1}[/muted]"
                )
                await asyncio.sleep(delay)

            try:
                resp = await client.post(
                    f"{PROXY_URL}/v1/messages?beta=true",
                    json=body,
                    headers=UPSTREAM_HEADERS,
                )
            except httpx.ConnectError:
                stats.errors += 1
                availability.mark_down(candidate, COOLDOWN_TRANSIENT)
                log.error(
                    f"   [error]✗[/error] [req_id]{req_id}[/req_id] "
                    f"[muted]{candidate} · connect error · cooldown {COOLDOWN_TRANSIENT:.0f}s"
                    f"{' → ' + next_candidate if next_candidate else ''}[/muted]"
                )
                if next_candidate:
                    fallback_count += 1
                    break  # next model
                return UpstreamResult(None, None, skipped, total_attempts, fallback_count)

            except httpx.TimeoutException:
                stats.errors += 1
                is_last = attempt == SAME_MODEL_RETRIES
                log.warning(
                    f"   [warning]⏱[/warning] [req_id]{req_id}[/req_id] "
                    f"[muted]{candidate} · timeout · "
                    f"{attempt + 1}/{SAME_MODEL_RETRIES + 1}[/muted]"
                )
                if not is_last:
                    continue  # same-model retry
                availability.mark_down(candidate, COOLDOWN_TRANSIENT)
                if next_candidate:
                    fallback_count += 1
                    break
                return UpstreamResult(None, None, skipped, total_attempts, fallback_count)

            if resp.status_code < 400:
                return UpstreamResult(resp, candidate, skipped, total_attempts, fallback_count)

            last_resp = resp
            err_msg = parse_upstream_error(resp)
            kind = classify_error(resp.status_code, err_msg)
            retryable = kind in ("rate", "transient", "auth")
            is_last = attempt == SAME_MODEL_RETRIES

            if retryable and not is_last:
                continue

            # Exhausted same-model attempts (or non-retryable) → cooldown + maybe next
            ttl = cooldown_for(kind)
            if ttl is not None:
                availability.mark_down(candidate, ttl)

            if next_candidate and (retryable or kind == "skip"):
                fallback_count += 1

            cooldown_part = f" · cooldown {ttl:.0f}s" if ttl is not None else ""
            next_part = f" → {next_candidate}" if next_candidate else ""
            log.warning(
                f"   [warning]↗[/warning] [req_id]{req_id}[/req_id] "
                f"[muted]{candidate} · HTTP {resp.status_code} {err_msg[:100]}"
                f"{cooldown_part}{next_part}[/muted]"
            )

            if not next_candidate:
                return UpstreamResult(resp, None, skipped, total_attempts, fallback_count)
            break  # next model in chain

    return UpstreamResult(None, None, skipped, total_attempts, fallback_count)

# ── Lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _http_client, quota_live
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
        limits=httpx.Limits(
            max_connections=HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=HTTP_MAX_KEEPALIVE,
        ),
    )

    console.print()
    console.print(Panel(
        _kv_table([
            ("Target", Text(PROXY_URL, style="bold cyan")),
            ("Listen", Text(f"{BRIDGE_HOST}:{BRIDGE_PORT}", style="bold cyan")),
            ("Policy", (
                Text("PASSTHROUGH — no rewrite / no model fallback", style="replace")
                if BRIDGE_PASSTHROUGH
                else Text.assemble(
                    ("Opus→Sonnet", "model"),
                    (" (Claude-only) · ", "muted"),
                    ("haiku→gemini-3.1-pro-low", "replace"),
                )
            )),
            ("Passthrough", Text(
                "on (BRIDGE_PASSTHROUGH)" if BRIDGE_PASSTHROUGH
                else "off · header X-Bridge-Passthrough: 1",
                style="bold" if BRIDGE_PASSTHROUGH else "muted",
            )),
            ("Retries", Text(f"{SAME_MODEL_RETRIES + 1} / model", style="bold")),
            ("Quota", Text(f"every {QUOTA_POLL_INTERVAL:.0f}s", style="bold")),
        ]),
        title="[header]🚀  LLM Proxy[/header]",
        border_style="bright_blue",
        padding=(0, 1),
    ))

    if QUOTA_LIVE_DISPLAY:
        quota_live = Live(
            Text("Initialising quota monitor...", style="muted"),
            console=console,
            auto_refresh=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        quota_live.start()

    await refresh_quota(silent=False)

    stop = asyncio.Event()
    quota_task = asyncio.create_task(_quota_loop(stop), name="quota-loop")

    try:
        yield
    finally:
        stop.set()
        quota_task.cancel()
        try:
            await quota_task
        except asyncio.CancelledError:
            pass
        if quota_live is not None:
            quota_live.stop()
            quota_live = None
        await _http_client.aclose()

    n = max(stats.requests, 1)
    console.print()
    console.print(Panel(
        _kv_table([
            ("Requests", Text.assemble(
                (str(stats.requests), "bold"),
                ("  errors=", "muted"),
                (str(stats.errors), "error" if stats.errors else "muted"),
            )),
            ("Tokens", Text(f"{stats.total_tokens:,}", style="tokens")),
            ("Avg", Text(f"{stats.total_ms / n:,.0f} ms", style="muted")),
        ]),
        title="[summary]📊  Session Summary[/summary]",
        border_style="green",
        padding=(0, 1),
    ))
    console.print()


app = FastAPI(lifespan=lifespan)

# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "requests": stats.requests,
        "errors": stats.errors,
        "total_tokens": stats.total_tokens,
        "unavailable": availability.cooldowns_snapshot(),
        "quota": last_quota,
        "policy": {
            "passthrough": BRIDGE_PASSTHROUGH,
            "claude_heavy": CLAUDE_HEAVY,
            "haiku_rewrite": MODEL_REWRITE,
            "same_model_retries": SAME_MODEL_RETRIES,
        },
    }


@app.post("/v1/messages", response_model=None)
async def messages(request: Request) -> StreamingResponse | JSONResponse:
    req_id = uuid.uuid4().hex[:8].upper()
    t_start = time.perf_counter()

    try:
        body = sanitise_body(await request.json())
    except Exception:
        log.exception(f"[req_id]{req_id}[/req_id] [error]invalid JSON[/error]")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    original_model = body.get("model", "unknown")
    passthrough = resolve_passthrough(request)
    chain = model_chain(original_model, passthrough=passthrough)
    tools = body.get("tools")
    is_stream = body.get("stream", False)
    has_system = bool(body.get("system"))

    result: UpstreamResult = await call_upstream(req_id, body, original_model, chain)
    elapsed = (time.perf_counter() - t_start) * 1000

    if result.used_model is None:
        stats.errors += 1
        _log_final(
            req_id, original_model, result, 0, 0, elapsed,
            is_stream=is_stream, has_system=has_system, tools=tools,
            passthrough=passthrough,
        )
        status = result.resp.status_code if result.resp is not None else 502
        # Keep 4xx from upstream; map total failure without body to 502
        if result.resp is None:
            status = 502
        elif status < 400:
            status = 502
        return JSONResponse(error_payload(result.resp), status_code=status)

    # Streaming
    if is_stream:
        async def stream_gen():
            in_tok = out_tok = 0
            try:
                async for line in result.resp.aiter_lines():
                    yield (line + "\n").encode()
                    if usage := usage_from_sse_line(line):
                        in_tok = usage.get("input_tokens", in_tok)
                        out_tok = usage.get("output_tokens", out_tok)
            except Exception as exc:
                log.error(f"[req_id]{req_id}[/req_id] [error]✗ stream error[/error] {exc}")
            finally:
                final_elapsed = (time.perf_counter() - t_start) * 1000
                stats.record(in_tok=in_tok, out_tok=out_tok, elapsed_ms=final_elapsed)
                _log_final(
                    req_id, original_model, result, in_tok, out_tok, final_elapsed,
                    is_stream=True, has_system=has_system, tools=tools,
                    passthrough=passthrough,
                )

        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "connection": "keep-alive"},
        )

    # Non-stream
    try:
        resp_body = result.resp.json()
        usage = resp_body.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
    except Exception:
        resp_body = {"error": "Invalid upstream JSON"}
        in_tok = out_tok = 0

    stats.record(in_tok=in_tok, out_tok=out_tok, elapsed_ms=elapsed)
    _log_final(
        req_id, original_model, result, in_tok, out_tok, elapsed,
        is_stream=False, has_system=has_system, tools=tools,
        passthrough=passthrough,
    )

    return JSONResponse(resp_body, status_code=result.resp.status_code)


if __name__ == "__main__":
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT, log_level="warning")
