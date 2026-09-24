"""Static panels: startup banner, profile picker, summaries and error boxes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import RoutingProfile, Settings
from ..stats import Stats, Usage
from .fmt import fmt_duration, fmt_seconds_short, fmt_tokens


def kv_table(rows: Sequence[tuple[str, Any]]) -> Table:
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(style="kv.key", no_wrap=True, min_width=10)
    table.add_column(style="kv.val", overflow="fold", ratio=1)
    for key, value in rows:
        table.add_row(key, value if isinstance(value, (Text, Table, Group)) else Text(str(value)))
    return table


# ============================================================================
# Profiles
# ============================================================================


def profiles_table(profiles: Sequence[RoutingProfile], *, default: RoutingProfile | None = None) -> Table:
    table = Table(box=None, show_header=True, header_style="header", pad_edge=False, padding=(0, 2))
    table.add_column("#", style="info", width=2, no_wrap=True)
    table.add_column("Профиль", style="model", no_wrap=True)
    table.add_column("Heavy", overflow="fold")
    table.add_column("Haiku", style="replace", overflow="fold")
    for index, profile in enumerate(profiles, start=1):
        marker = Text(" ●" if profile is default else "", style="success")
        table.add_row(
            str(index), Text.assemble(profile.title, marker), profile.heavy_label, profile.haiku_model
        )
    return table


def profile_picker(profiles: Sequence[RoutingProfile], default: RoutingProfile) -> Panel:
    return Panel(
        Group(
            Text("Выберите конфигурацию моделей", style="header"),
            Text("Можно пропустить выбор: --profile <номер|ключ> или BRIDGE_PROFILE в .env", style="muted"),
            Text(),
            profiles_table(profiles, default=default),
        ),
        title="[header]⚙ Model Profile[/header]",
        title_align="left",
        border_style="bright_blue",
        padding=(1, 2),
    )


# ============================================================================
# Startup / shutdown
# ============================================================================


def startup_banner(settings: Settings, profile: RoutingProfile, *, quota_status: Text, live: bool) -> Panel:
    if settings.passthrough_default:
        policy = Text("PASSTHROUGH — без подмены моделей и fallback", style="replace")
        passthrough = Text("включён глобально (BRIDGE_PASSTHROUGH=1)", style="bold")
    else:
        policy = Text.assemble(
            ("Heavy ", "muted"),
            (profile.heavy_label, "model"),
            ("   Haiku ", "muted"),
            (profile.haiku_model, "replace"),
        )
        passthrough = Text("по запросу: заголовок X-Bridge-Passthrough: 1", style="muted")

    retries = Text.assemble(
        ("на модель ", "muted"),
        (str(settings.attempts_per_model), "bold"),
        (f"  backoff {settings.retry_base_delay:g}–{settings.retry_max_delay:g}s", "muted"),
    )
    cooldowns = Text(
        "  ".join(
            f"{name} {fmt_seconds_short(value)}"
            for name, value in (
                ("rate", settings.cooldown_rate),
                ("auth", settings.cooldown_auth),
                ("transient", settings.cooldown_transient),
                ("skip", settings.cooldown_skip),
            )
        ),
        style="muted",
    )

    rows: list[tuple[str, Any]] = [
        ("Профиль", Text(profile.title, style="model")),
        ("Политика", policy),
        ("Passthrough", passthrough),
        ("Listen", Text(f"http://{settings.host}:{settings.port}", style="bold cyan")),
        ("Upstream", Text(settings.cpa_base_url, style="bold cyan")),
        ("Ретраи", retries),
        ("Cooldown", cooldowns),
        ("Квоты", quota_status),
        ("Консоль", Text("live-панель" if live else "поток логов", style="muted")),
    ]
    return Panel(
        kv_table(rows),
        title="[header]🚀 LLM Bridge[/header]",
        title_align="left",
        subtitle="[muted]Ctrl+C — остановить[/muted]",
        subtitle_align="right",
        border_style="bright_blue",
        padding=(0, 1),
    )


def _usage_text(usage: Usage, *, requests: int | None = None) -> Text:
    text = Text.assemble(
        ("↑", "tokens.in"),
        (fmt_tokens(usage.input_tokens), "tokens.in"),
        ("  ↓", "tokens.out"),
        (fmt_tokens(usage.output_tokens), "tokens.out"),
        (
            f"   кэш чтение {fmt_tokens(usage.cache_read_tokens)} / запись {fmt_tokens(usage.cache_write_tokens)}",
            "muted",
        ),
        ("   всего ", "muted"),
        (fmt_tokens(usage.processed), "bold"),
    )
    if requests is not None:
        text.append(f"   запросов {requests}", style="muted")
    return text


def session_summary(stats: Stats, profile: RoutingProfile) -> Panel:
    requests = Text.assemble(
        (str(stats.requests), "bold"),
        ("   ✓ ", "success"),
        str(stats.succeeded),
        ("   ✗ ", "error"),
        (str(stats.failed), "error" if stats.failed else "muted"),
        ("   ⊘ ", "warning"),
        (str(stats.cancelled), "warning" if stats.cancelled else "muted"),
    )
    upstream = Text(
        f"ошибок {stats.upstream_errors} · ретраев {stats.retries} · fallback {stats.fallbacks}",
        style="muted",
    )
    rows: list[tuple[str, Any]] = [
        ("Профиль", Text(profile.title, style="model")),
        ("Аптайм", Text(fmt_duration(stats.uptime), style="muted")),
        ("Запросы", requests),
        ("Upstream", upstream),
        ("Токены", _usage_text(stats.usage)),
    ]
    for model, entry in stats.models_by_volume():
        rows.append((f"  {model}", _usage_text(entry.usage, requests=entry.requests)))
    rows.append(("Среднее", Text(f"{stats.avg_ms:,.0f} ms" if stats.succeeded else "—", style="muted")))
    return Panel(
        kv_table(rows),
        title="[summary]📊 Итоги сессии[/summary]",
        title_align="left",
        border_style="green",
        padding=(0, 1),
    )


# ============================================================================
# Errors
# ============================================================================


def error_panel(title: str, lines: Sequence[str | Text], *, hint: str | None = None) -> Panel:
    body: list[RenderableType] = []
    for line in lines:
        body.append(line if isinstance(line, Text) else Text(line))
    if hint:
        body.append(Text())
        body.append(Text(hint, style="muted"))
    return Panel(
        Group(*body),
        title=f"[error]✗ {title}[/error]",
        title_align="left",
        border_style="red",
        padding=(0, 1),
    )
