"""Live bottom panel: quota report, in-flight requests and a status bar.

Log lines are printed above it by Rich. When live mode is off (or stdout is
not a terminal) quota reports are printed inline instead.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.live import Live
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from ..availability import ModelAvailability
from ..config import Settings
from ..context import Phase, RequestContext
from ..stats import Stats
from .console import console, print_block
from .fmt import fmt_duration, fmt_seconds_short, fmt_tokens, short_model

# The live region never takes more than this share of the terminal height,
# so the request log above it stays visible.
MAX_LIVE_SHARE = 0.6
MAX_ACTIVE_ROWS = 6

_PHASE_STYLE = {
    Phase.QUEUED: "phase.wait",
    Phase.WAITING: "phase.wait",
    Phase.BACKOFF: "phase.retry",
    Phase.STREAMING: "phase",
    Phase.THINKING: "phase.think",
    Phase.WRITING: "phase",
    Phase.TOOL: "tool",
    Phase.DONE: "muted",
}


def phase_text(ctx: RequestContext) -> Text:
    label = ctx.phase.value
    if ctx.phase_detail:
        label = f"{label} {ctx.phase_detail}"
    return Text(label, style=_PHASE_STYLE.get(ctx.phase, "phase"))


class Dashboard:
    def __init__(
        self,
        settings: Settings,
        stats: Stats,
        availability: ModelAvailability,
        active: Callable[[], list[RequestContext]],
    ) -> None:
        self.settings = settings
        self.stats = stats
        self.availability = availability
        self.active = active
        self.quota: RenderableType | None = None

    # -- pieces ---------------------------------------------------------

    def _active_table(self, requests: list[RequestContext]) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column(style="req_id", no_wrap=True)
        table.add_column(style="model.dim", no_wrap=True, max_width=36, overflow="ellipsis")
        table.add_column(no_wrap=True)
        table.add_column(style="muted", no_wrap=True, justify="right")
        shown = sorted(requests, key=lambda c: c.started)[:MAX_ACTIVE_ROWS]
        for ctx in shown:
            model = ctx.model or (ctx.chain[0] if ctx.chain else ctx.original_model)
            table.add_row(
                Text("▶", style="info"),
                ctx.req_id,
                short_model(model),
                phase_text(ctx),
                fmt_duration(ctx.elapsed),
            )
        hidden = len(requests) - len(shown)
        if hidden > 0:
            table.add_row("", "", Text(f"… ещё {hidden}", style="muted"), "", "")
        return table

    def _token_lines(self) -> list[Text]:
        """Session token totals (always shown) + per-model split when 2+ models were used."""
        stats = self.stats
        usage = stats.usage
        total = Text(no_wrap=True, overflow="ellipsis")
        total.append("Σ токены ", style="header")
        for label, value, style in (
            ("вход", usage.input_tokens, "tokens.in"),
            ("выход", usage.output_tokens, "tokens.out"),
            ("кэш чтение", usage.cache_read_tokens, "tokens"),
            ("кэш запись", usage.cache_write_tokens, "tokens"),
            ("всего", usage.processed, "bold"),
        ):
            total.append(f"  {label} ", style="muted")
            total.append(fmt_tokens(value), style=style)
        lines = [total]

        models = stats.models_by_volume()
        if len(models) > 1:
            split = Text("  ", no_wrap=True, overflow="ellipsis")
            for index, (model, entry) in enumerate(models):
                if index:
                    split.append("  · ", style="muted")
                split.append(short_model(model), style="model.dim")
                split.append(f" ↑{fmt_tokens(entry.usage.input_tokens)}", style="tokens.in")
                split.append(f" ↓{fmt_tokens(entry.usage.output_tokens)}", style="tokens.out")
                split.append(f" ({entry.requests})", style="muted")
            lines.append(split)
        return lines

    def _status_bar(self, active_count: int) -> Text:
        stats = self.stats
        bar = Text(no_wrap=True, overflow="ellipsis")
        bar.append("● ", style="success")
        bar.append(f":{self.settings.port}", style="bold")
        bar.append(" → ", style="muted")
        bar.append(self.settings.cpa_base_url.split("://", 1)[-1], style="muted")
        bar.append(f"  up {fmt_duration(stats.uptime)}", style="muted")
        bar.append("  │ ", style="muted")
        bar.append(f"{stats.requests} req ", style="bold")
        bar.append(f"✓{stats.succeeded}", style="success" if stats.succeeded else "muted")
        bar.append(f" ✗{stats.failed}", style="error" if stats.failed else "muted")
        bar.append(f" ⊘{stats.cancelled}", style="warning" if stats.cancelled else "muted")
        if active_count:
            bar.append("  │ ", style="muted")
            bar.append(f"активных {active_count}", style="info")
        cooldowns = self.availability.snapshot()
        if cooldowns:
            bar.append("  │ cooldown ", style="muted")
            bar.append(
                ", ".join(
                    f"{short_model(model)} {fmt_seconds_short(left)}"
                    for model, (left, _reason) in sorted(cooldowns.items(), key=lambda item: item[1][0])
                ),
                style="warning",
            )
        return bar

    # -- rendering ------------------------------------------------------

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        requests = self.active()
        tail: list[RenderableType] = []
        if requests:
            tail.append(self._active_table(requests))
        tail.extend(self._token_lines())
        tail.append(self._status_bar(len(requests)))

        tail_lines = [console.render_lines(item, options, pad=False) for item in tail]
        tail_height = sum(len(lines) for lines in tail_lines)

        if self.quota is not None:
            budget = max(3, int(console.size.height * MAX_LIVE_SHARE) - tail_height)
            quota_lines = console.render_lines(self.quota, options, pad=False)
            if len(quota_lines) > budget:
                hidden = len(quota_lines) - budget + 1
                quota_lines = quota_lines[: budget - 1]
                quota_lines.append(
                    console.render_lines(
                        Text(
                            f"  … ещё {hidden} строк квот (увеличьте окно или см. GET /health)", style="muted"
                        ),
                        options,
                        pad=False,
                    )[0]
                )
            for line in quota_lines:
                yield from line
                yield Segment.line()

        for lines in tail_lines:
            for line in lines:
                yield from line
                yield Segment.line()


class LiveDisplay:
    """Owns the ``rich.Live`` region; every method is safe to call in any state."""

    def __init__(self, dashboard: Dashboard, *, enabled: bool) -> None:
        self.dashboard = dashboard
        self.enabled = enabled and console.is_terminal
        self._live: Live | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._live is not None

    def start(self) -> None:
        with self._lock:
            if not self.enabled or self._live is not None:
                return
            live = Live(
                self.dashboard,
                console=console,
                auto_refresh=True,
                refresh_per_second=2,
                transient=False,
                redirect_stdout=True,
                redirect_stderr=True,
                vertical_overflow="crop",
            )
            live.start()
            self._live = live

    def stop(self) -> None:
        with self._lock:
            live, self._live = self._live, None
        if live is not None:
            with suppress(Exception):
                live.stop()

    def refresh(self) -> None:
        live = self._live
        if live is not None:
            with suppress(Exception):
                live.refresh()

    def show_quota(self, renderable: RenderableType) -> None:
        if self._live is not None:
            self.dashboard.quota = renderable
            self.refresh()
        else:
            print_block(renderable)
