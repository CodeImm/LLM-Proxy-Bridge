"""
check_models.py — проверка доступности моделей Claude (Anthropic) и Gemini (Google).

Проверка — только реальными запросами POST /v1/messages (не по GET /v1/models).
  • OK / FAIL / TIMEOUT — по ответу прокси
  • ABSENT — провайдер ответил «unknown provider» / model not found (модель проверена, её нет)
  • Thinking suffix: id ``*-thinking``
  • Thinking param: body ``thinking: {type: ...}`` (--thinking-param)

Запуск:
    python check_models.py
    python check_models.py --url http://localhost:4000
    python check_models.py --no-thinking-param
    python check_models.py --concurrency 6 --timeout 30
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ─── Тема ──────────────────────────────────────────────────────────────────────

THEME = Theme({
    "ok":       "bold green",
    "fail":     "bold red",
    "timeout":  "bold yellow",
    "pending":  "dim white",
    "spin":     "bold cyan",
    "model":    "bold magenta",
    "ms":       "dim cyan",
    "ms_fast":  "bold green",
    "ms_mid":   "bold yellow",
    "ms_slow":  "bold red",
    "header":   "bold bright_white on dark_blue",
    "section":  "bold bright_blue",
    "muted":    "dim white",
    "depr":     "dim red",
    "claude":   "bold #F59E0B",
    "gemini":   "bold #38BDF8",
    "think":    "bold #A78BFA",
    "kpi":      "bold white",
})

console = Console(theme=THEME, highlight=False)

# ─── Статический каталог ───────────────────────────────────────────────────────
#
#  "deprecated": True — модель устаревает/скоро отключается.
#  Thinking-модели с суффиксом *-thinking также подтягиваются из CPA /v1/models.

MODELS: list[dict] = [
    # ══════════════════════════════════════════════════════════════════════════
    #  ANTHROPIC / CLAUDE
    # ══════════════════════════════════════════════════════════════════════════
    # ── Claude 5 ──────────────────────────────────────────────────────────────
    {"id": "claude-fable-5",               "tier": "👑", "label": "Fable 5",        "family": "Claude 5"},
    {"id": "claude-sonnet-5",              "tier": "🥈", "label": "Sonnet 5",       "family": "Claude 5"},
    {"id": "claude-opus-5-5-high",         "tier": "👑", "label": "Opus 5.5 High",  "family": "Claude 5"},
    {"id": "claude-sonnet-5-5-high",       "tier": "🥈", "label": "Sonnet 5.5 High","family": "Claude 5"},
    {"id": "claude-opus-5-5",              "tier": "👑", "label": "Opus 5.5",       "family": "Claude 5"},
    {"id": "claude-sonnet-5-5",            "tier": "🥈", "label": "Sonnet 5.5",     "family": "Claude 5"},
    {"id": "claude-haiku-5-5",             "tier": "🥉", "label": "Haiku 5.5",      "family": "Claude 5"},

    # ── Claude 4.x ────────────────────────────────────────────────────────────
    {"id": "claude-opus-4-8",              "tier": "🥇", "label": "Opus 4.8",       "family": "Claude 4"},
    {"id": "claude-opus-4-7",              "tier": "🥇", "label": "Opus 4.7",       "family": "Claude 4"},
    {"id": "claude-opus-4-6",              "tier": "🥇", "label": "Opus 4.6",       "family": "Claude 4"},
    {"id": "claude-opus-4-6-thinking",     "tier": "🧠", "label": "Opus 4.6 Think", "family": "Claude 4"},
    {"id": "claude-opus-4-5",              "tier": "🥇", "label": "Opus 4.5 alias", "family": "Claude 4"},
    {"id": "claude-opus-4-5-20251101",     "tier": "🥇", "label": "Opus 4.5",       "family": "Claude 4"},
    {"id": "claude-sonnet-4-6",            "tier": "🥈", "label": "Sonnet 4.6",     "family": "Claude 4"},
    {"id": "claude-sonnet-4-5",            "tier": "🥈", "label": "Sonnet 4.5 alias","family": "Claude 4"},
    {"id": "claude-sonnet-4-5-20250929",   "tier": "🥈", "label": "Sonnet 4.5",     "family": "Claude 4"},
    {"id": "claude-haiku-4-5-20251001",    "tier": "🥉", "label": "Haiku 4.5",      "family": "Claude 4"},
    {"id": "claude-haiku-4-5",             "tier": "🥉", "label": "Haiku 4.5 alias","family": "Claude 4"},

    # ── Claude 3.x (legacy) ───────────────────────────────────────────────────
    {"id": "claude-3-5-sonnet-20241022",   "tier": "🔷", "label": "3.5 Sonnet v2",  "family": "Claude 3.x"},
    {"id": "claude-3-5-haiku-20241022",    "tier": "🔷", "label": "3.5 Haiku",      "family": "Claude 3.x"},
    {"id": "claude-3-7-sonnet-20250219",   "tier": "🔷", "label": "3.7 Sonnet",     "family": "Claude 3.x"},
    {"id": "claude-3-opus-20240229",       "tier": "🔷", "label": "3 Opus",         "family": "Claude 3.x"},
    {"id": "claude-3-sonnet-20240229",     "tier": "🔷", "label": "3 Sonnet",       "family": "Claude 3.x"},
    {"id": "claude-3-haiku-20240307",      "tier": "🔷", "label": "3 Haiku",        "family": "Claude 3.x"},

    # ══════════════════════════════════════════════════════════════════════════
    #  GOOGLE / GEMINI — кастомные ID прокси
    # ══════════════════════════════════════════════════════════════════════════
    {"id": "gemini-4-argon-high",          "tier": "👑", "label": "4 Argon High",   "family": "Gemini (proxy)"},
    {"id": "gemini-4-argon-medium",        "tier": "👑", "label": "4 Argon Med",    "family": "Gemini (proxy)"},
    {"id": "gemini-4-argon-low",           "tier": "👑", "label": "4 Argon Low",    "family": "Gemini (proxy)"},
    {"id": "gemini-3.8-flash-high",        "tier": "⚡", "label": "3.8 Flash High", "family": "Gemini (proxy)"},
    {"id": "gemini-3.5-flash-lite",        "tier": "🔋", "label": "3.5 Flash Lite", "family": "Gemini (proxy)"},
    {"id": "gemini-3-flash",               "tier": "🔋", "label": "3 Flash",        "family": "Gemini (proxy)"},
    # Пропали из Antigravity /v1/models к октябрю 2026:
    {"id": "gemini-3-pro-high",            "tier": "⚠️", "label": "3 Pro High",     "family": "Gemini (proxy)", "deprecated": True},
    {"id": "gemini-3-pro-low",             "tier": "⚠️", "label": "3 Pro Low",      "family": "Gemini (proxy)", "deprecated": True},
    {"id": "gemini-3.1-pro-high",          "tier": "⚠️", "label": "3.1 Pro High",   "family": "Gemini (proxy)", "deprecated": True},
    {"id": "gemini-3.1-pro-low",           "tier": "⚠️", "label": "3.1 Pro Low",    "family": "Gemini (proxy)", "deprecated": True},
    {"id": "gemini-pro-agent",             "tier": "⚠️", "label": "Pro Agent",      "family": "Gemini (proxy)", "deprecated": True},
    {"id": "gemini-3-flash-agent",         "tier": "⚠️", "label": "3 Flash Agent",  "family": "Gemini (proxy)", "deprecated": True},
    {"id": "gemini-3.7-flash-high",        "tier": "⚠️", "label": "3.7 Flash High", "family": "Gemini (proxy)", "deprecated": True},
    {"id": "gemini-3.5-flash-low",         "tier": "⚠️", "label": "3.5 Flash Low",  "family": "Gemini (proxy)", "deprecated": True},

    # ══════════════════════════════════════════════════════════════════════════
    #  GOOGLE / GEMINI — официальные API ID
    # ══════════════════════════════════════════════════════════════════════════
    {"id": "gemini-4-argon",               "tier": "👑", "label": "4 Argon",        "family": "Gemini 4"},
    {"id": "gemini-3.8-flash",             "tier": "⭐", "label": "3.8 Flash",      "family": "Gemini 3.x"},
    {"id": "gemini-3.6-flash",             "tier": "⭐", "label": "3.6 Flash",      "family": "Gemini 3.x"},
    {"id": "gemini-3.5-flash",             "tier": "⚠️", "label": "3.5 Flash",      "family": "Gemini 3.x", "deprecated": True},
    {"id": "gemini-3.1-pro",               "tier": "⭐", "label": "3.1 Pro",        "family": "Gemini 3.x"},
    {"id": "gemini-3.1-pro-preview",       "tier": "⭐", "label": "3.1 Pro Preview","family": "Gemini 3.x"},
    {"id": "gemini-3-pro",                 "tier": "⭐", "label": "3 Pro",          "family": "Gemini 3.x"},
    {"id": "gemini-3-flash-preview",       "tier": "⚡", "label": "3 Flash Preview","family": "Gemini 3.x"},
    {"id": "gemini-3.1-flash-lite",        "tier": "🔋", "label": "3.1 Flash Lite", "family": "Gemini 3.x"},
    {"id": "gemini-3.1-flash-lite-preview","tier": "⚠️", "label": "3.1 Flash Lite P","family": "Gemini 3.x", "deprecated": True},

    {"id": "gemini-2.5-pro",               "tier": "🔹", "label": "2.5 Pro",        "family": "Gemini 2.5"},
    {"id": "gemini-2.5-flash",             "tier": "🔹", "label": "2.5 Flash",      "family": "Gemini 2.5"},
    {"id": "gemini-2.5-flash-lite",        "tier": "🔹", "label": "2.5 Flash Lite", "family": "Gemini 2.5"},
    {"id": "gemini-2.5-flash-preview-05-20","tier": "⚠️","label": "2.5 Flash P 05-20","family": "Gemini 2.5", "deprecated": True},

    {"id": "gemini-2.0-flash",             "tier": "⚠️", "label": "2.0 Flash",      "family": "Gemini 2.0", "deprecated": True},
    {"id": "gemini-2.0-flash-001",         "tier": "⚠️", "label": "2.0 Flash 001",  "family": "Gemini 2.0", "deprecated": True},
    {"id": "gemini-2.0-flash-lite",        "tier": "⚠️", "label": "2.0 Flash Lite", "family": "Gemini 2.0", "deprecated": True},
]

# Claude models that support Anthropic-style thinking body param (not suffix).
# Newer models: adaptive. Older (4.5, 3.7): manual enabled + budget.
THINKING_PARAM_ADAPTIVE: set[str] = {
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
}

THINKING_PARAM_ENABLED: set[str] = {
    "claude-opus-4-5",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-3-7-sonnet-20250219",
}

PROBE_MAX_TOKENS_BARE = 8
PROBE_MAX_TOKENS_THINK = 256
PROBE_THINK_BUDGET = 1024


# ─── Статус ────────────────────────────────────────────────────────────────────

class Status(Enum):
    PENDING  = "pending"
    CHECKING = "checking"
    OK       = "ok"
    FAIL     = "fail"
    TIMEOUT  = "timeout"
    ABSENT   = "absent"  # request done: provider has no such model


def _family_style(family: str) -> str:
    if family.startswith("Claude"):
        return "claude"
    if family.startswith("Gemini"):
        return "gemini"
    if family.startswith("Thinking"):
        return "think"
    return "section"


def _latency_style(ms: float) -> str:
    if ms < 1000:
        return "ms_fast"
    if ms < 3000:
        return "ms_mid"
    return "ms_slow"


def _latency_bar(ms: float, max_ms: float, width: int = 8) -> Text:
    if max_ms <= 0:
        filled = 0
    else:
        filled = max(1, min(width, int(round((ms / max_ms) * width))))
    empty = width - filled
    style = _latency_style(ms)
    t = Text()
    t.append("█" * filled, style=style)
    t.append("░" * empty, style="muted")
    return t


def thinking_config_for(model_id: str) -> dict[str, Any] | None:
    """Return Anthropic thinking body config for a base model id, or None."""
    if model_id.endswith("-thinking"):
        return None
    if model_id in THINKING_PARAM_ADAPTIVE:
        return {"type": "adaptive"}
    if model_id in THINKING_PARAM_ENABLED:
        return {"type": "enabled", "budget_tokens": PROBE_THINK_BUDGET}
    # Heuristic for future Claude ids not yet listed
    if model_id.startswith("claude-") and any(
        x in model_id for x in ("opus", "sonnet", "haiku", "fable")
    ):
        # Prefer adaptive for modern IDs without dated snapshot of 3.x
        if model_id.startswith("claude-3"):
            return None
        return {"type": "adaptive"}
    return None


@dataclass
class Result:
    model_id:   str
    family:     str
    tier:       str
    label:      str
    deprecated: bool = False
    # bare | suffix | param — how thinking is activated (if at all)
    mode:       str = "bare"
    thinking:   dict[str, Any] | None = None
    discovered: bool = False  # added from live /v1/models
    status:     Status = Status.PENDING
    http_code:  int | None = None
    latency_ms: float | None = None
    error:      str | None = None
    # response signal: saw thinking content block
    had_thinking_block: bool | None = None

    @property
    def probe_key(self) -> str:
        """Unique row key for display/dedup."""
        if self.mode == "param" and self.thinking:
            t = self.thinking.get("type", "?")
            return f"{self.model_id}+thinking:{t}"
        return self.model_id

    @property
    def id_cell(self) -> Text:
        t = Text()
        t.append(self.model_id, style="muted")
        if self.mode == "param" and self.thinking:
            kind = self.thinking.get("type", "?")
            t.append(f"  +thinking:{kind}", style="think")
        elif self.mode == "suffix" or self.model_id.endswith("-thinking"):
            t.append("  [suffix]", style="think")
        return t

    @property
    def status_cell(self) -> Text:
        match self.status:
            case Status.OK:
                extra = ""
                if self.had_thinking_block is True:
                    extra = " 🧠"
                elif self.mode in ("suffix", "param") and self.had_thinking_block is False:
                    extra = " ·no-block"
                return Text(f"●  OK{extra}", style="ok")
            case Status.FAIL:
                code = f" {self.http_code}" if self.http_code else ""
                return Text(f"●  FAIL{code}", style="fail")
            case Status.TIMEOUT:
                return Text("●  TIMEOUT", style="timeout")
            case Status.ABSENT:
                return Text("○  ABSENT", style="muted")
            case Status.CHECKING:
                return Text("◐  …", style="spin")
            case _:
                return Text("○  —", style="pending")

    def latency_cell(self, max_ok_ms: float = 0.0) -> Text:
        if self.latency_ms is None or self.status in (Status.PENDING, Status.CHECKING):
            return Text("—", style="pending")
        ms = self.latency_ms
        if self.status != Status.OK:
            return Text(f"{ms:.0f} ms", style="muted")
        cell = Text()
        if max_ok_ms > 0:
            cell.append_text(_latency_bar(ms, max_ok_ms))
            cell.append(" ")
        cell.append(f"{ms:.0f} ms", style=_latency_style(ms))
        return cell

    @property
    def error_cell(self) -> Text:
        if self.status == Status.ABSENT:
            msg = self.error or "not on proxy"
            short = msg[:72] + ("…" if len(msg) > 72 else "")
            return Text(f"проверено · {short}", style="muted")
        if not self.error:
            if self.status == Status.OK and self.mode in ("suffix", "param"):
                if self.had_thinking_block is True:
                    return Text("thinking block ✓", style="ok")
                if self.had_thinking_block is False:
                    return Text("200, no thinking block", style="muted")
            return Text("")
        short = self.error[:72] + ("…" if len(self.error) > 72 else "")
        style = "depr" if self.deprecated else "fail"
        return Text(short, style=style)

    @property
    def label_cell(self) -> Text:
        t = Text()
        style = "depr" if self.deprecated else "model"
        t.append(self.label, style=style)
        if self.mode == "param":
            t.append("  param", style="think")
        elif self.mode == "suffix" or self.model_id.endswith("-thinking"):
            t.append("  suffix", style="think")
        if self.discovered:
            t.append("  live", style="muted")
        if self.deprecated:
            t.append("  deprecated", style="depr")
        return t


# ─── Probe list ────────────────────────────────────────────────────────────────

def build_probe_specs(include_thinking_param: bool) -> list[dict]:
    """
    Full catalog HTTP probes (+ optional Anthropic-style thinking param).
    Availability is decided only by POST /v1/messages responses.
    """
    specs: list[dict] = []
    seen_keys: set[str] = set()

    def add(spec: dict) -> None:
        key = spec["probe_key"]
        if key in seen_keys:
            return
        seen_keys.add(key)
        specs.append(spec)

    for m in MODELS:
        mid = m["id"]
        mode = "suffix" if mid.endswith("-thinking") else "bare"
        add({
            "model_id": mid,
            "family": m["family"],
            "tier": m["tier"],
            "label": m["label"],
            "deprecated": m.get("deprecated", False),
            "mode": mode,
            "thinking": None,
            "discovered": False,
            "probe_key": mid,
        })

    if include_thinking_param:
        for m in MODELS:
            mid = m["id"]
            if mid.endswith("-thinking") or not mid.startswith("claude-"):
                continue
            cfg = thinking_config_for(mid)
            if not cfg:
                continue
            t = cfg.get("type", "?")
            add({
                "model_id": mid,
                "family": "Thinking (param)",
                "tier": "🧠",
                "label": f"{m['label']} +{t}",
                "deprecated": m.get("deprecated", False),
                "mode": "param",
                "thinking": cfg,
                "discovered": False,
                "probe_key": f"{mid}+thinking:{t}",
            })

    return specs


def _is_absent_error(http_code: int | None, error: str) -> bool:
    """True when the proxy/provider says the model does not exist."""
    err = (error or "").lower()
    patterns = (
        "unknown provider",
        "model not found",
        "not_found_error",
        "does not exist",
        "invalid model",
        "unknown model",
        "no such model",
        "model_not_found",
    )
    if any(p in err for p in patterns):
        return True
    if http_code == 404 and ("model" in err or not err):
        return True
    return False


# ─── Таблица ───────────────────────────────────────────────────────────────────

def _max_ok_latency(results: list[Result]) -> float:
    ok_ms = [r.latency_ms for r in results if r.status == Status.OK and r.latency_ms is not None]
    return max(ok_ms) if ok_ms else 0.0


def build_table(results: list[Result], done: int, total: int) -> Panel:
    max_ok = _max_ok_latency(results)

    table = Table(
        show_header=True,
        header_style="header",
        border_style="bright_blue",
        row_styles=["", "dim"],
        pad_edge=True,
        expand=True,
        show_lines=False,
    )
    table.add_column("",        width=3,  no_wrap=True, justify="center")
    table.add_column("Модель",  min_width=22, no_wrap=True)
    table.add_column("ID / mode", min_width=34, no_wrap=True)
    table.add_column("Статус",  width=16, no_wrap=True)
    table.add_column("Latency", width=18, no_wrap=True)
    table.add_column("Детали",  min_width=18, overflow="ellipsis")

    current_family: str | None = None
    family_counts = Counter(r.family for r in results)

    for r in results:
        if r.family != current_family:
            current_family = r.family
            table.add_section()
            n = family_counts[r.family]
            style = _family_style(r.family)
            header = Text()
            header.append(f"  {r.family}", style=style)
            header.append(f"  ·  {n} probe{'s' if n != 1 else ''}", style="muted")
            table.add_row("", header, "", "", "", "")
        table.add_row(
            r.tier,
            r.label_cell,
            r.id_cell,
            r.status_cell,
            r.latency_cell(max_ok),
            r.error_cell,
        )

    ok_count     = sum(1 for r in results if r.status == Status.OK)
    fail_count   = sum(1 for r in results if r.status == Status.FAIL)
    to_count     = sum(1 for r in results if r.status == Status.TIMEOUT)
    absent_count = sum(1 for r in results if r.status == Status.ABSENT)
    pct          = (done / total) if total else 0.0

    bar_w = 28
    filled = int(round(pct * bar_w)) if total else 0
    bar = Text()
    bar.append("█" * filled, style="bold green")
    bar.append("░" * (bar_w - filled), style="muted")
    bar.append(f"  {done}/{total}  ", style="muted")
    bar.append(f"{pct * 100:.0f}%", style="kpi")
    bar.append("   ")
    bar.append(f"✓ {ok_count}", style="ok")
    bar.append("  ")
    bar.append(f"✗ {fail_count}", style="fail")
    bar.append("  ")
    bar.append(f"⏱ {to_count}", style="timeout")
    bar.append("  ")
    bar.append(f"○ {absent_count}", style="muted")

    title = Text.assemble(
        (" 🔍  Проверка моделей  ", "bold white"),
        (f"{done}/{total}", "muted"),
    )
    body = Group(bar, Text(""), table)
    return Panel(body, title=title, border_style="bright_blue", padding=(0, 1))


# ─── Проверка одной модели ─────────────────────────────────────────────────────

PROBE_MESSAGES = [{"role": "user", "content": "Say: OK"}]


def _response_has_thinking_block(body: Any) -> bool | None:
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking"):
            return True
    return False


async def check_model(
    result: Result,
    base_url: str,
    timeout: float,
    sem: asyncio.Semaphore,
    api_key: str,
) -> None:
    result.status = Status.CHECKING
    max_tokens = PROBE_MAX_TOKENS_THINK if result.thinking else PROBE_MAX_TOKENS_BARE
    payload: dict[str, Any] = {
        "model": result.model_id,
        "max_tokens": max_tokens,
        "messages": PROBE_MESSAGES,
    }
    if result.thinking:
        payload["thinking"] = result.thinking
        # Anthropic requires max_tokens > budget for enabled mode
        if result.thinking.get("type") == "enabled":
            budget = int(result.thinking.get("budget_tokens") or PROBE_THINK_BUDGET)
            payload["max_tokens"] = max(max_tokens, budget + 64)

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    async with sem:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/v1/messages",
                    json=payload,
                    headers=headers,
                )
            elapsed = (time.perf_counter() - t0) * 1000
            result.latency_ms = elapsed
            result.http_code  = resp.status_code
            if resp.status_code == 200:
                result.status = Status.OK
                try:
                    body = resp.json()
                    result.had_thinking_block = _response_has_thinking_block(body)
                except Exception:
                    result.had_thinking_block = None
            else:
                try:
                    body = resp.json()
                    err  = body.get("error") or body.get("message") or ""
                    if isinstance(err, dict):
                        err = err.get("message") or str(err)
                    result.error = str(err)[:100] if err else resp.text[:100]
                except Exception:
                    result.error = resp.text[:100]
                if _is_absent_error(resp.status_code, result.error or ""):
                    result.status = Status.ABSENT
                else:
                    result.status = Status.FAIL
        except httpx.TimeoutException:
            result.status     = Status.TIMEOUT
            result.latency_ms = (time.perf_counter() - t0) * 1000
            result.error      = f"timeout >{timeout}s"
        except Exception as exc:
            result.status     = Status.FAIL
            result.latency_ms = (time.perf_counter() - t0) * 1000
            result.error      = str(exc)[:100]


# ─── Итоговый отчёт ────────────────────────────────────────────────────────────

def print_summary(results: list[Result]) -> int:
    ok      = [r for r in results if r.status == Status.OK]
    failed  = [r for r in results if r.status == Status.FAIL]
    timeout = [r for r in results if r.status == Status.TIMEOUT]
    absent  = [r for r in results if r.status == Status.ABSENT]
    total   = len(results)
    # success rate among models that exist or are ambiguous (exclude ABSENT)
    active  = total - len(absent)
    rate    = (len(ok) / active * 100) if active else 0.0

    think_ok = [r for r in ok if r.mode in ("suffix", "param")]
    think_blocks = sum(1 for r in think_ok if r.had_thinking_block is True)

    console.print()
    console.rule("[bold white]Итог[/bold white]")

    kpi = Table.grid(padding=(0, 3))
    for _ in range(6):
        kpi.add_column()
    kpi.add_row(
        Text.from_markup(f"[ok]✓  Доступно[/ok]\n[kpi]{len(ok)}[/kpi]"),
        Text.from_markup(f"[fail]✗  Ошибка[/fail]\n[kpi]{len(failed)}[/kpi]"),
        Text.from_markup(f"[timeout]⏱  Таймаут[/timeout]\n[kpi]{len(timeout)}[/kpi]"),
        Text.from_markup(f"[muted]○  Нет модели[/muted]\n[kpi]{len(absent)}[/kpi]"),
        Text.from_markup(f"[muted]Success (без absent)[/muted]\n[kpi]{rate:.0f}%[/kpi]"),
        Text.from_markup(
            f"[think]🧠  Think OK[/think]\n[kpi]{len(think_ok)}[/kpi]  "
            f"[muted]({think_blocks} w/ block)[/muted]"
        ),
    )
    console.print(Panel(kpi, border_style="bright_blue", padding=(0, 2)))

    if ok:
        latencies = sorted(r.latency_ms for r in ok if r.latency_ms is not None)
        avg = sum(latencies) / len(latencies)
        mid = latencies[len(latencies) // 2]
        fastest = min(ok, key=lambda r: r.latency_ms or 0)
        slowest = max(ok, key=lambda r: r.latency_ms or 0)
        max_ok = max(latencies)

        console.print()
        console.print(
            f"  Latency  avg [ms]{avg:.0f} ms[/ms]  ·  "
            f"p50 [ms]{mid:.0f} ms[/ms]  ·  "
            f"min [{_latency_style(fastest.latency_ms or 0)}]{fastest.latency_ms:.0f} ms[/{_latency_style(fastest.latency_ms or 0)}]  ·  "
            f"max [{_latency_style(slowest.latency_ms or 0)}]{slowest.latency_ms:.0f} ms[/{_latency_style(slowest.latency_ms or 0)}]"
        )
        console.print(
            f"  Быстрая:  {fastest.tier} [model]{fastest.label}[/model]  [muted]{fastest.probe_key}[/muted]"
        )
        console.print(
            f"  Медленная: {slowest.tier} [model]{slowest.label}[/model]  [muted]{slowest.probe_key}[/muted]"
        )

        console.print("\n  [ok]Доступные[/ok] (по latency):")
        for r in sorted(ok, key=lambda x: x.latency_ms or 0):
            bar = _latency_bar(r.latency_ms or 0, max_ok)
            line = Text("    ")
            line.append(f"{r.tier}  ")
            line.append(f"{r.label:<24}", style="model")
            line.append("  ")
            line.append_text(bar)
            line.append(f"  {r.latency_ms:>6.0f} ms  ", style=_latency_style(r.latency_ms or 0))
            line.append(r.probe_key, style="muted")
            if r.had_thinking_block is True:
                line.append("  🧠", style="think")
            console.print(line)

        think_rows = [r for r in results if r.mode in ("suffix", "param")]
        if think_rows:
            console.print("\n  [think]Thinking probes[/think]:")
            for r in think_rows:
                st = {
                    Status.OK: ("✓", "ok"),
                    Status.FAIL: ("✗", "fail"),
                    Status.TIMEOUT: ("⏱", "timeout"),
                    Status.ABSENT: ("○", "muted"),
                }.get(r.status, ("?", "muted"))
                line = Text("    ")
                line.append(f"{st[0]} ", style=st[1])
                line.append(f"{r.mode:<7}", style="think")
                line.append(f" {r.probe_key}", style="muted")
                if r.status == Status.OK:
                    if r.had_thinking_block is True:
                        line.append("  → thinking block", style="ok")
                    elif r.had_thinking_block is False:
                        line.append("  → 200, no thinking block", style="muted")
                elif r.status == Status.ABSENT:
                    line.append("  → absent (проверено запросом)", style="muted")
                elif r.error:
                    line.append(f"  {r.error[:60]}", style="fail")
                console.print(line)

    bad = failed + timeout
    if bad:
        console.print("\n  [fail]Ошибки / таймауты[/fail]:")
        for r in bad:
            console.print(
                f"    {r.tier}  [model]{r.label:<24}[/model]  [muted]{r.probe_key}[/muted]"
            )
            if r.error:
                console.print(f"         [fail]{r.error[:80]}[/fail]")

    if absent:
        console.print(
            f"\n  [muted]Нет на прокси[/muted]  "
            f"[muted](проверено POST /v1/messages · unknown provider)[/muted]  "
            f"[kpi]{len(absent)}[/kpi]"
        )
        by_family: dict[str, list[Result]] = {}
        for r in absent:
            by_family.setdefault(r.family, []).append(r)
        for fam, rows in by_family.items():
            style = _family_style(fam)
            console.print(f"    [{style}]{fam}[/{style}]  [muted]· {len(rows)}[/muted]")
            for r in rows:
                dep = "  [depr]deprecated[/depr]" if r.deprecated else ""
                console.print(
                    f"      {r.tier}  [muted]{r.label:<22}[/muted]  "
                    f"[muted]{r.probe_key}[/muted]  "
                    f"[muted]· {r.error or 'absent'}[/muted]{dep}"
                )

    console.print()
    # ABSENT is expected for catalog noise — exit 1 only on fail/timeout
    return 0 if not bad else 1


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main(
    base_url: str,
    concurrency: int,
    timeout: float,
    api_key: str,
    thinking_param: bool,
) -> int:
    specs = build_probe_specs(include_thinking_param=thinking_param)
    results = [
        Result(
            model_id   = s["model_id"],
            family     = s["family"],
            tier       = s["tier"],
            label      = s["label"],
            deprecated = s["deprecated"],
            mode       = s["mode"],
            thinking   = s["thinking"],
            discovered = s["discovered"],
        )
        for s in specs
    ]
    total = len(results)
    sem   = asyncio.Semaphore(concurrency)

    n_suffix = sum(1 for r in results if r.mode == "suffix" or r.model_id.endswith("-thinking"))
    n_param  = sum(1 for r in results if r.mode == "param")
    n_bare   = sum(1 for r in results if r.mode == "bare")
    n_claude = sum(1 for r in results if "claude" in r.model_id)
    n_gemini = sum(1 for r in results if "gemini" in r.model_id)

    banner = Table.grid(padding=(0, 2))
    banner.add_column(style="muted", justify="right")
    banner.add_column()
    banner.add_row("Прокси", f"[bold cyan]{base_url}[/bold cyan]")
    banner.add_row("Метод", "[ok]POST /v1/messages[/ok]  [muted](не /v1/models)[/muted]")
    banner.add_row(
        "HTTP probes",
        f"[bold white]{total}[/bold white]  "
        f"[muted]bare {n_bare} · suffix {n_suffix} · param {n_param}[/muted]",
    )
    banner.add_row("Claude / Gemini ids", f"[claude]{n_claude}[/claude] / [gemini]{n_gemini}[/gemini]")
    banner.add_row(
        "Thinking",
        f"[think]suffix {n_suffix}[/think]  ·  [think]param {n_param}[/think]"
        + ("  [muted](param off)[/muted]" if not thinking_param else ""),
    )
    banner.add_row("Параллельность", f"[bold white]{concurrency}[/bold white]")
    banner.add_row("Таймаут", f"[bold white]{timeout}s[/bold white]")

    console.print(
        Panel(
            banner,
            title="[bold white]🔍  Model Availability Check[/bold white]",
            border_style="bright_blue",
            padding=(1, 2),
        )
    )

    done_count = 0
    with Live(build_table(results, 0, total), console=console, refresh_per_second=10) as live:
        async def run_one(r: Result) -> None:
            nonlocal done_count
            await check_model(r, base_url, timeout, sem, api_key)
            done_count += 1
            live.update(build_table(results, done_count, total))

        await asyncio.gather(*[run_one(r) for r in results])
        live.update(build_table(results, total, total))

    return print_summary(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Проверка моделей через POST /v1/messages (Claude + Gemini + thinking)"
    )
    parser.add_argument("--url", default="http://localhost:4000", help="URL bridge/прокси")
    parser.add_argument("--api-key", default=os.environ.get("UPSTREAM_API_KEY", "sk-antigravity"))
    parser.add_argument("--concurrency", default=4, type=int, help="Параллельных запросов (default: 4)")
    parser.add_argument("--timeout", default=30.0, type=float, help="Таймаут на probe, сек (default: 30)")
    parser.add_argument(
        "--thinking-param",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Доп. probes: base Claude id + thinking body param (default: on)",
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            main(
                base_url=args.url,
                concurrency=args.concurrency,
                timeout=args.timeout,
                api_key=args.api_key,
                thinking_param=args.thinking_param,
            )
        )
    )
