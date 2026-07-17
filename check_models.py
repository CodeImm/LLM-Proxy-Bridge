"""
check_models.py — проверка доступности моделей Claude (Anthropic) и Gemini (Google).
Запуск:
    python check_models.py                             # все модели
    python check_models.py --url http://localhost:4000
    python check_models.py --concurrency 4
    python check_models.py --timeout 30
"""
import argparse
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
import httpx
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
# ─── Тема ──────────────────────────────────────────────────────────────────────
THEME = Theme({
    "ok":      "bold green",
    "fail":    "bold red",
    "timeout": "bold yellow",
    "pending": "dim white",
    "spin":    "bold cyan",
    "model":   "bold magenta",
    "ms":      "dim cyan",
    "header":  "bold bright_white on dark_blue",
    "section": "bold bright_blue",
    "muted":   "dim white",
    "depr":    "dim red",
})
console = Console(theme=THEME, highlight=False)
# ─── Список моделей ─────────────────────────────────────────────────────────────
#
#  "deprecated": True — модель устаревает/скоро отключается, но тест всё равно
#  прогоняется, в таблице помечается особо.
MODELS: list[dict] = [
    # ══════════════════════════════════════════════════════════════════════════
    #  ANTHROPIC / CLAUDE
    # ══════════════════════════════════════════════════════════════════════════
    # ── Claude 4.x (актуальное поколение) ─────────────────────────────────────
    {"id": "claude-opus-4-7",              "tier": "🥇", "label": "Opus 4.7",      "family": "Claude 4"},
    {"id": "claude-opus-4-6",              "tier": "🥇", "label": "Opus 4.6",      "family": "Claude 4"},
    {"id": "claude-opus-4-6-thinking",     "tier": "🧠", "label": "Opus 4.6 Think","family": "Claude 4"},
    {"id": "claude-sonnet-4-6",            "tier": "🥈", "label": "Sonnet 4.6",    "family": "Claude 4"},
    {"id": "claude-haiku-4-5-20251001",    "tier": "🥉", "label": "Haiku 4.5",     "family": "Claude 4"},
    {"id": "claude-haiku-4-5",             "tier": "🥉", "label": "Haiku 4.5 alias","family": "Claude 4"},
    # ── Claude 3.x (legacy, всё ещё доступны через API) ───────────────────────
    {"id": "claude-3-5-sonnet-20241022",   "tier": "🔷", "label": "3.5 Sonnet v2", "family": "Claude 3.x"},
    {"id": "claude-3-5-haiku-20241022",    "tier": "🔷", "label": "3.5 Haiku",     "family": "Claude 3.x"},
    {"id": "claude-3-7-sonnet-20250219",   "tier": "🔷", "label": "3.7 Sonnet",    "family": "Claude 3.x"},
    {"id": "claude-3-opus-20240229",       "tier": "🔷", "label": "3 Opus",        "family": "Claude 3.x"},
    {"id": "claude-3-sonnet-20240229",     "tier": "🔷", "label": "3 Sonnet",      "family": "Claude 3.x"},
    {"id": "claude-3-haiku-20240307",      "tier": "🔷", "label": "3 Haiku",       "family": "Claude 3.x"},
    # ══════════════════════════════════════════════════════════════════════════
    #  GOOGLE / GEMINI — кастомные ID прокси
    # ══════════════════════════════════════════════════════════════════════════
    {"id": "gemini-3-pro-high",            "tier": "⚡", "label": "3 Pro High",    "family": "Gemini (proxy)"},
    {"id": "gemini-3-pro-low",             "tier": "⚡", "label": "3 Pro Low",     "family": "Gemini (proxy)"},
    {"id": "gemini-3.1-pro-high",          "tier": "⚡", "label": "3.1 Pro High",  "family": "Gemini (proxy)"},
    {"id": "gemini-3.1-pro-low",           "tier": "⚡", "label": "3.1 Pro Low",   "family": "Gemini (proxy)"},
    {"id": "gemini-pro-agent",             "tier": "🤖", "label": "Pro Agent",     "family": "Gemini (proxy)"},
    {"id": "gemini-3-flash-agent",         "tier": "🤖", "label": "3 Flash Agent", "family": "Gemini (proxy)"},
    {"id": "gemini-3.5-flash-low",         "tier": "🔋", "label": "3.5 Flash Low", "family": "Gemini (proxy)"},
    {"id": "gemini-3-flash",               "tier": "🔋", "label": "3 Flash",       "family": "Gemini (proxy)"},
    # ══════════════════════════════════════════════════════════════════════════
    #  GOOGLE / GEMINI — официальные API ID (Google AI Studio / Vertex AI)
    # ══════════════════════════════════════════════════════════════════════════
    # ── Gemini 3.x (актуальное поколение, 2026) ───────────────────────────────
    {"id": "gemini-3.1-pro",               "tier": "⭐", "label": "3.1 Pro",        "family": "Gemini 3.x"},
    {"id": "gemini-3.1-pro-preview",       "tier": "⭐", "label": "3.1 Pro Preview", "family": "Gemini 3.x"},
    {"id": "gemini-3-pro",                 "tier": "⭐", "label": "3 Pro",           "family": "Gemini 3.x"},
    {"id": "gemini-3-flash",               "tier": "⚡", "label": "3 Flash",         "family": "Gemini 3.x"},
    {"id": "gemini-3-flash-preview",       "tier": "⚡", "label": "3 Flash Preview", "family": "Gemini 3.x"},
    {"id": "gemini-3.5-flash",             "tier": "⚡", "label": "3.5 Flash",       "family": "Gemini 3.x"},
    {"id": "gemini-3.1-flash-lite",        "tier": "🔋", "label": "3.1 Flash Lite",  "family": "Gemini 3.x"},
    {"id": "gemini-3.1-flash-lite-preview","tier": "🔋", "label": "3.1 Flash Lite P","family": "Gemini 3.x"},
    # ── Gemini 2.5 (стабильное предыдущее поколение) ──────────────────────────
    {"id": "gemini-2.5-pro",               "tier": "🔹", "label": "2.5 Pro",         "family": "Gemini 2.5"},
    {"id": "gemini-2.5-flash",             "tier": "🔹", "label": "2.5 Flash",        "family": "Gemini 2.5"},
    {"id": "gemini-2.5-flash-lite",        "tier": "🔹", "label": "2.5 Flash Lite",   "family": "Gemini 2.5"},
    {"id": "gemini-2.5-flash-preview-05-20","tier": "🔹","label": "2.5 Flash P 05-20","family": "Gemini 2.5"},
    # ── Gemini 2.0 (устаревает 01.06.2026) ────────────────────────────────────
    {"id": "gemini-2.0-flash",             "tier": "⚠️", "label": "2.0 Flash",       "family": "Gemini 2.0", "deprecated": True},
    {"id": "gemini-2.0-flash-001",         "tier": "⚠️", "label": "2.0 Flash 001",   "family": "Gemini 2.0", "deprecated": True},
    {"id": "gemini-2.0-flash-lite",        "tier": "⚠️", "label": "2.0 Flash Lite",  "family": "Gemini 2.0", "deprecated": True},
]
# ─── Статус ────────────────────────────────────────────────────────────────────
class Status(Enum):
    PENDING  = "pending"
    CHECKING = "checking"
    OK       = "ok"
    FAIL     = "fail"
    TIMEOUT  = "timeout"
@dataclass
class Result:
    model_id:    str
    family:      str
    tier:        str
    label:       str
    deprecated:  bool        = False
    status:      Status      = Status.PENDING
    http_code:   int | None  = None
    latency_ms:  float | None = None
    error:       str | None  = None
    @property
    def status_cell(self) -> Text:
        match self.status:
            case Status.OK:
                return Text("✓  OK", style="ok")
            case Status.FAIL:
                code = f" ({self.http_code})" if self.http_code else ""
                return Text(f"✗  FAIL{code}", style="fail")
            case Status.TIMEOUT:
                return Text("⏱  TIMEOUT", style="timeout")
            case Status.CHECKING:
                return Text("⟳  ...", style="spin")
            case _:
                return Text("—", style="pending")
    @property
    def latency_cell(self) -> Text:
        if self.latency_ms is None:
            return Text("—", style="pending")
        return Text(f"{self.latency_ms:.0f} ms", style="ms")
    @property
    def error_cell(self) -> Text:
        if not self.error:
            return Text("")
        short = self.error[:64] + ("…" if len(self.error) > 64 else "")
        style = "depr" if self.deprecated else "fail"
        return Text(short, style=style)
    @property
    def label_cell(self) -> Text:
        style = "depr" if self.deprecated else "model"
        suffix = "  [depr][deprecated][/depr]" if self.deprecated else ""
        return Text.from_markup(f"[{style}]{self.label}[/{style}]{suffix}")
# ─── Таблица ───────────────────────────────────────────────────────────────────
def build_table(results: list[Result], done: int, total: int) -> Panel:
    table = Table(
        show_header=True,
        header_style="header",
        border_style="bright_blue",
        row_styles=["", "dim"],
        pad_edge=True,
        expand=True,
    )
    table.add_column("",         width=3,  no_wrap=True)
    table.add_column("Модель",   min_width=30, no_wrap=True)
    table.add_column("ID",       min_width=32, no_wrap=True)
    table.add_column("Семейство",width=16)
    table.add_column("Статус",   width=14, no_wrap=True)
    table.add_column("Latency",  width=10, no_wrap=True)
    table.add_column("Детали",   min_width=22)
    current_family = None
    for r in results:
        if r.family != current_family:
            current_family = r.family
            table.add_section()
        table.add_row(
            r.tier,
            r.label_cell,
            f"[muted]{r.model_id}[/muted]",
            r.family,
            r.status_cell,
            r.latency_cell,
            r.error_cell,
        )
    ok_count   = sum(1 for r in results if r.status == Status.OK)
    fail_count = sum(1 for r in results if r.status in (Status.FAIL, Status.TIMEOUT))
    pct        = f"{done/total*100:.0f}%" if total else "0%"
    title = (
        f"[bold white]🔍  Проверка моделей[/bold white]  "
        f"[muted]{done}/{total} ({pct})[/muted]  "
        f"[ok]✓ {ok_count}[/ok]  [fail]✗ {fail_count}[/fail]"
    )
    return Panel(table, title=title, border_style="bright_blue", padding=(0, 1))
# ─── Проверка одной модели ─────────────────────────────────────────────────────
PROBE_PAYLOAD = {
    "max_tokens": 8,
    "messages": [{"role": "user", "content": "Say: OK"}],
}
async def check_model(
    result: Result,
    base_url: str,
    timeout: float,
    sem: asyncio.Semaphore,
) -> None:
    result.status = Status.CHECKING
    payload = {**PROBE_PAYLOAD, "model": result.model_id}
    async with sem:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                resp = await client.post(
                    f"{base_url}/v1/messages",
                    json=payload,
                    headers={
                        "x-api-key":         "sk-antigravity",
                        "anthropic-version": "2023-06-01",
                        "content-type":      "application/json",
                    },
                )
            elapsed = (time.perf_counter() - t0) * 1000
            result.latency_ms = elapsed
            result.http_code  = resp.status_code
            if resp.status_code == 200:
                result.status = Status.OK
            else:
                result.status = Status.FAIL
                try:
                    body = resp.json()
                    err  = body.get("error") or body.get("message") or ""
                    if isinstance(err, dict):
                        err = err.get("message") or str(err)
                    result.error = str(err)[:100] if err else resp.text[:100]
                except Exception:
                    result.error = resp.text[:100]
        except httpx.TimeoutException:
            result.status     = Status.TIMEOUT
            result.latency_ms = (time.perf_counter() - t0) * 1000
            result.error      = f"timeout >{timeout}s"
        except Exception as exc:
            result.status     = Status.FAIL
            result.latency_ms = (time.perf_counter() - t0) * 1000
            result.error      = str(exc)[:100]
# ─── Итоговый отчёт ────────────────────────────────────────────────────────────
def print_summary(results: list[Result]) -> None:
    ok      = [r for r in results if r.status == Status.OK]
    failed  = [r for r in results if r.status == Status.FAIL]
    timeout = [r for r in results if r.status == Status.TIMEOUT]
    console.print()
    console.rule("[bold white]Итог[/bold white]")
    console.print(f"  [ok]✓  Доступно:[/ok]    {len(ok)}")
    console.print(f"  [fail]✗  Недоступно:[/fail]  {len(failed)}")
    console.print(f"  [timeout]⏱  Таймаут:[/timeout]     {len(timeout)}")
    if ok:
        avg     = sum(r.latency_ms for r in ok) / len(ok)
        fastest = min(ok, key=lambda r: r.latency_ms)
        slowest = max(ok, key=lambda r: r.latency_ms)
        console.print(f"\n  Средний latency:     [ms]{avg:.0f} ms[/ms]")
        console.print(f"  Самая быстрая:       [model]{fastest.label}[/model]  [ms]{fastest.latency_ms:.0f} ms[/ms]  [muted]({fastest.model_id})[/muted]")
        console.print(f"  Самая медленная:     [model]{slowest.label}[/model]  [ms]{slowest.latency_ms:.0f} ms[/ms]  [muted]({slowest.model_id})[/muted]")
    if ok:
        console.print("\n  [ok]Доступные:[/ok]")
        for r in sorted(ok, key=lambda r: r.latency_ms):
            console.print(f"    {r.tier}  [model]{r.label:<22}[/model]  [ms]{r.latency_ms:>6.0f} ms[/ms]  [muted]{r.model_id}[/muted]")
    if failed or timeout:
        console.print("\n  [fail]Недоступные:[/fail]")
        for r in failed + timeout:
            console.print(f"    {r.tier}  [model]{r.label:<22}[/model]  [muted]{r.model_id}[/muted]")
            if r.error:
                console.print(f"       [fail]{r.error}[/fail]")
    console.print()
# ─── Main ──────────────────────────────────────────────────────────────────────
async def main(base_url: str, concurrency: int, timeout: float) -> None:
    results = [
        Result(
            model_id   = m["id"],
            family     = m["family"],
            tier       = m["tier"],
            label      = m["label"],
            deprecated = m.get("deprecated", False),
        )
        for m in MODELS
    ]
    total = len(results)
    sem   = asyncio.Semaphore(concurrency)
    console.print(
        Panel(
            f"  Прокси:         [bold cyan]{base_url}[/bold cyan]\n"
            f"  Семейств:       [bold white]Claude + Gemini[/bold white]\n"
            f"  Моделей:        [bold white]{total}[/bold white]\n"
            f"  Параллельность: [bold white]{concurrency}[/bold white]\n"
            f"  Таймаут:        [bold white]{timeout}s[/bold white]",
            title="[bold white]🔍  Model Availability Check[/bold white]",
            border_style="bright_blue",
            padding=(1, 3),
        )
    )
    done_count = 0
    with Live(build_table(results, 0, total), console=console, refresh_per_second=10) as live:
        async def run_one(r: Result) -> None:
            nonlocal done_count
            await check_model(r, base_url, timeout, sem)
            done_count += 1
            live.update(build_table(results, done_count, total))
        await asyncio.gather(*[run_one(r) for r in results])
        live.update(build_table(results, total, total))
    print_summary(results)
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Проверка доступности моделей Claude и Gemini")
    parser.add_argument("--url",         default="http://localhost:4000",  help="URL прокси")
    parser.add_argument("--concurrency", default=4,    type=int,   help="Параллельных запросов (default: 4)")
    parser.add_argument("--timeout",     default=30.0, type=float, help="Таймаут на модель, секунды (default: 30)")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.concurrency, args.timeout))
