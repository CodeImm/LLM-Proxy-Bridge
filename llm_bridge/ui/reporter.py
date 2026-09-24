"""Console implementation of ``Reporter``: one aligned line per request."""

from __future__ import annotations

import time

from rich.text import Text

from ..context import Outcome, Phase, RequestContext, Result
from ..errors import UpstreamError
from .console import print_line
from .dashboard import LiveDisplay
from .fmt import fmt_duration, fmt_seconds_short, fmt_tokens, short_model, truncate

THINKING_LOG_INTERVAL = 15.0
ERROR_TEXT_LIMIT = 160

_RESULT_ICON = {
    Result.OK: ("✓", "success"),
    Result.FAILED: ("✗", "error"),
    Result.CANCELLED: ("⊘", "warning.strong"),
}


def _status_style(status: int) -> str:
    if status < 400:
        return "status.2xx"
    if status < 500 or status == 499:
        return "status.4xx"
    return "status.5xx"


def _model_part(ctx: RequestContext) -> Text:
    effective = ctx.model or (ctx.chain[0] if ctx.chain else ctx.original_model)
    text = Text()
    if ctx.passthrough:
        text.append(effective, style="model")
    elif ctx.rewritten:
        text.append(ctx.original_model, style="replace")
        text.append(" → ", style="muted")
        text.append(effective, style="model")
    elif ctx.chain and effective != ctx.chain[0]:
        text.append(short_model(ctx.chain[0]), style="muted")
        text.append(" ⇢ ", style="warning")
        text.append(effective, style="model")
    else:
        text.append(effective, style="model")
    return text


def _tokens_part(ctx: RequestContext, outcome: Outcome) -> Text:
    usage = ctx.usage
    if not usage.any:
        return Text("—".ljust(15), style="muted")
    value = f"↑{fmt_tokens(usage.input_tokens)} ↓{fmt_tokens(usage.output_tokens)}"
    return Text(value.ljust(15), style="tokens" if outcome.result is Result.OK else "muted")


class ConsoleReporter:
    def __init__(self, live: LiveDisplay) -> None:
        self.live = live
        self._unknown_routes: set[tuple[str, str]] = set()

    # -- lifecycle ------------------------------------------------------

    def request_started(self, ctx: RequestContext) -> None:
        self.live.refresh()

    def phase_changed(self, ctx: RequestContext) -> None:
        if self.live.running:
            return
        if ctx.phase is Phase.THINKING and not ctx.last_progress_log:
            ctx.last_progress_log = time.perf_counter()
            print_line("✻", "phase.think", (ctx.req_id, "req_id"), ("  thinking…", "muted"))

    def progress(self, ctx: RequestContext) -> None:
        if self.live.running or not ctx.last_progress_log:
            return
        now = time.perf_counter()
        if now - ctx.last_progress_log >= THINKING_LOG_INTERVAL:
            ctx.last_progress_log = now
            print_line(
                "✻",
                "phase.think",
                (ctx.req_id, "req_id"),
                (f"  всё ещё thinking… {fmt_duration(ctx.elapsed)}", "muted"),
            )

    def request_finished(self, ctx: RequestContext, outcome: Outcome) -> None:
        icon, icon_style = _RESULT_ICON[outcome.result]
        status = str(outcome.status) if outcome.result is not Result.CANCELLED else "---"

        line = Text()
        line.append(ctx.req_id, style="req_id")
        line.append("  ")
        line.append(status, style=_status_style(outcome.status))
        line.append(f"  {fmt_duration(ctx.elapsed):>7}  ", style="muted")
        line.append_text(_tokens_part(ctx, outcome))
        line.append(" ")
        line.append_text(_model_part(ctx))

        flags: list[tuple[str, str]] = []
        if ctx.passthrough:
            flags.append(("passthrough", "muted"))
        if ctx.attempts > 1:
            flags.append((f"×{ctx.attempts}", "warning"))
        if ctx.skipped:
            flags.append(("−" + ",".join(short_model(m) for m in ctx.skipped), "skip"))
        if ctx.stream:
            flags.append(("stream", "muted"))
        if ctx.has_system:
            flags.append(("sys", "system"))
        if ctx.tool_count:
            flags.append((f"⚒{ctx.tool_count}", "tool"))
        if outcome.result is Result.OK and ctx.stream and ctx.ttft is not None:
            flags.append((f"ttft {fmt_duration(ctx.ttft)}", "muted"))
        if ctx.usage.cache_read_tokens:
            flags.append((f"cache {fmt_tokens(ctx.usage.cache_read_tokens)}", "muted"))
        for label, style in flags:
            line.append("  ")
            line.append(label, style=style)

        if outcome.error:
            line.append("  ")
            line.append(truncate(outcome.error, ERROR_TEXT_LIMIT), style="error.soft")
        if outcome.warning:
            line.append("  ⚠ ", style="warning")
            line.append(outcome.warning, style="warning")

        print_line(icon, icon_style, line)
        self.live.refresh()

    # -- upstream events ------------------------------------------------

    def retrying(
        self,
        ctx: RequestContext,
        model: str,
        attempt: int,
        max_attempts: int,
        delay: float,
        error: UpstreamError,
    ) -> None:
        print_line(
            "↺",
            "warning.strong",
            (ctx.req_id, "req_id"),
            "  ",
            (model, "model.dim"),
            (f" · {error.label}", "warning"),
            (f" {truncate(error.message, 100)}" if error.message and not error.timeout else "", "muted"),
            (f" · попытка {attempt}/{max_attempts} через {delay:.1f}s", "muted"),
        )

    def model_failed(
        self,
        ctx: RequestContext,
        model: str,
        error: UpstreamError,
        cooldown: float | None,
        next_model: str | None,
    ) -> None:
        parts: list[tuple[str, str] | str] = [
            (ctx.req_id, "req_id"),
            "  ",
            (model, "model.dim"),
            (f" · {error.label}", "warning"),
        ]
        if error.message and not error.timeout and not error.connect:
            parts.append((f" {truncate(error.message, 100)}", "muted"))
        if cooldown:
            parts.append((f" · cooldown {fmt_seconds_short(cooldown)} ({error.kind.value})", "muted"))
        if next_model:
            parts.append((" → ", "muted"))
            parts.append((next_model, "model"))
        print_line("↗", "warning.strong", *parts)

    def all_models_cooling_down(self, ctx: RequestContext) -> None:
        print_line(
            "⚠",
            "warning.strong",
            (ctx.req_id, "req_id"),
            ("  все модели цепочки в cooldown — пробую всё равно (fail-open)", "warning"),
        )

    def unknown_route(self, method: str, path: str) -> None:
        key = (method, path)
        if key in self._unknown_routes:
            return
        self._unknown_routes.add(key)
        print_line(
            "⚠",
            "warning.strong",
            (f"404 {method} {path}", "warning"),
            ("  — маршрут не поддерживается bridge (повторы скрыты)", "muted"),
        )
