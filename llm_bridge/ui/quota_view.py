"""Rendering of Quota Inspector reports."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ..quota import QuotaSnapshot
from .panels import kv_table

BAR_WIDTH = 16
LOW_THRESHOLD = 30  # same as the inspector: remaining <= 30% is "low"

_PROVIDER_TITLES = {"antigravity": "Antigravity", "gemini-cli": "Gemini CLI", "codex": "Codex"}
_ACCOUNT_PREFIXES = ("antigravity-", "gemini-cli-", "codex-")


# ============================================================================
# Primitives
# ============================================================================


def _as_percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _bar_style(remaining: float, exhausted: bool) -> str:
    if exhausted or remaining < 25:
        return "bar.low"
    if remaining < 50:
        return "bar.mid"
    return "bar.high"


def quota_bar(remaining: Any, *, exhausted: bool = False, width: int = BAR_WIDTH) -> Text:
    value = _as_percent(remaining)
    if value is None:
        return Text("—", style="muted")
    filled = min(width, max(0, round(value / 100 * width)))
    style = _bar_style(value, exhausted)
    return Text.assemble(
        ("█" * filled, style),
        ("░" * (width - filled), "bar.empty"),
        (f" {value:3.0f}%", style),
    )


def status_text(status: Any) -> Text:
    value = str(status or "unknown")
    lowered = value.lower()
    if lowered in ("exhausted", "error"):
        style = "error"
    elif lowered in ("low", "warning", "degraded"):
        style = "warning"
    elif lowered in ("full", "ok", "healthy"):
        style = "success"
    else:
        style = "muted"
    return Text(value, style=style)


def short_account(name: Any) -> str:
    result = str(name or "?")
    for prefix in _ACCOUNT_PREFIXES:
        if result.startswith(prefix):
            result = result[len(prefix) :]
            break
    return result.removesuffix(".json")


def _windows(report: dict[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for key in ("windows", "additional_windows"):
        items = report.get(key)
        if isinstance(items, list):
            windows.extend(w for w in items if isinstance(w, dict))
    return windows


def _window_label(window: dict[str, Any]) -> str:
    return str(window.get("label") or window.get("id") or "?")


def _matches(label: str, groups: Iterable[str]) -> bool:
    lowered = label.lower()
    return any(group in lowered or lowered in group for group in groups)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def select_windows(windows: list[dict[str, Any]], groups: Iterable[str]) -> list[dict[str, Any]]:
    """Windows of the wanted groups: exact label/id match, else substring (``claude`` → Claude/GPT)."""
    selected: list[dict[str, Any]] = []
    for group in groups:
        wanted = _norm(group)
        if not wanted:
            continue
        matches = [w for w in windows if wanted in (_norm(w.get("label")), _norm(w.get("id")))]
        if not matches:
            matches = [w for w in windows if wanted in _norm(_window_label(w))]
        selected.extend(w for w in matches if all(w is not s for s in selected))
    return selected


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# The inspector formats resets in local time as "MM-DD HH:MM" (no year).
_RESET_RE = re.compile(r"^\s*(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s*$")


def reset_in_seconds(label: Any, now: datetime) -> float:
    """Seconds from ``now`` to a reset label; unknown / ``-`` → +inf (sorts last)."""
    match = _RESET_RE.match(str(label or ""))
    if match is None:
        return math.inf
    month, day, hour, minute = (int(part) for part in match.groups())
    best: float | None = None
    for year in (now.year - 1, now.year, now.year + 1):  # nearest year handles Dec → Jan
        try:
            delta = (datetime(year, month, day, hour, minute) - now).total_seconds()
        except ValueError:
            continue
        if best is None or abs(delta) < abs(best):
            best = delta
    return math.inf if best is None else best


def _sort_by_reset(windows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    return sorted(windows, key=lambda w: reset_in_seconds(w.get("reset_label"), now))


def _earliest_reset(windows: list[dict[str, Any]], now: datetime) -> float:
    return min((reset_in_seconds(w.get("reset_label"), now) for w in windows), default=math.inf)


def _border_for(exhausted: int, low: int) -> str:
    if exhausted:
        return "red"
    if low:
        return "yellow"
    return "green"


def _border(summary: dict[str, Any]) -> str:
    return _border_for(_int(summary.get("exhausted_accounts")), _int(summary.get("low_accounts")))


def _summary_line(
    snapshot: QuotaSnapshot,
    accounts: int,
    *,
    exhausted: int | None = None,
    low: int | None = None,
    errors: int | None = None,
) -> Text:
    """Counts default to the inspector summary; the compact view passes its own."""
    summary = snapshot.summary
    exhausted = _int(summary.get("exhausted_accounts")) if exhausted is None else exhausted
    low = _int(summary.get("low_accounts")) if low is None else low
    errors = _int(summary.get("error_accounts")) if errors is None else errors
    updated = time.strftime("%H:%M:%S", time.localtime(snapshot.fetched_at))
    return Text.assemble(
        (f"{accounts} акк.", "bold"),
        ("  exhausted ", "muted"),
        (str(exhausted), "error" if exhausted else "muted"),
        ("  low ", "muted"),
        (str(low), "warning" if low else "muted"),
        ("  errors ", "muted"),
        (str(errors), "error.soft" if errors else "muted"),
        (f"  · {updated}", "muted"),
    )


def _equivalents(summary: dict[str, Any], groups: Iterable[str] | None = None) -> list[tuple[str, Any]]:
    """Quota equivalents, optionally only for the displayed model groups."""
    merged: dict[str, Any] = {}
    for key in ("antigravity_equivalents", "gemini_equivalents"):
        value = summary.get(key)
        if isinstance(value, dict):
            merged.update(value)
    items = sorted(merged.items())
    if groups is not None:
        wanted = list(groups)
        items = [(k, v) for k, v in items if _matches(str(k), wanted)]
    return items


def _equivalents_text(items: list[tuple[str, Any]]) -> Text | None:
    if not items:
        return None
    text = Text("эквивалент  ", style="muted")
    for index, (key, value) in enumerate(items):
        if index:
            text.append(" · ", style="muted")
        text.append(f"{key} ", style="kv.key")
        text.append(f"{value}%", style="info")
    return text


# ============================================================================
# Compact view (one table, filtered model groups)
# ============================================================================


def _compact(snapshot: QuotaSnapshot, groups: tuple[str, ...]) -> RenderableType:
    """One table with the given quota groups only, soonest reset first."""
    now = datetime.now()

    rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for report in snapshot.reports:
        visible = select_windows(_windows(report), groups)
        if visible or report.get("error"):
            rows.append((report, _sort_by_reset(visible, now)))
    # Accounts without a known reset (errors, "-") go last; ties keep inspector order.
    rows.sort(key=lambda row: _earliest_reset(row[1], now))

    single_group = len(groups) == 1
    table = Table(box=None, show_header=True, header_style="header", pad_edge=False, padding=(0, 1))
    table.add_column("Аккаунт", style="model", min_width=14, overflow="ellipsis", no_wrap=True)
    if not single_group:
        table.add_column("Группа", style="info", min_width=10, overflow="ellipsis", no_wrap=True)
    table.add_column("Остаток", min_width=BAR_WIDTH + 5, no_wrap=True)
    table.add_column("Сброс", style="muted", no_wrap=True)

    def add_row(name: str, group: str, remaining: Text, reset: str) -> None:
        cells = (name, remaining, reset) if single_group else (name, group, remaining, reset)
        table.add_row(*cells)

    exhausted = low = errors = 0
    seen: set[str] = set()
    for report, visible in rows:
        name = short_account(report.get("name"))

        if not visible:
            errors += 1
            add_row(name, "—", Text(f"error: {str(report['error'])[:40]}", style="error.soft"), "—")
            continue

        known = [v for v in (_as_percent(w.get("remaining_percent")) for w in visible) if v is not None]
        if any(w.get("exhausted") for w in visible) or (known and min(known) <= 0):
            exhausted += 1
        elif known and min(known) <= LOW_THRESHOLD:
            low += 1

        for position, window in enumerate(visible):
            seen.add(_norm(_window_label(window)))
            seen.add(_norm(window.get("id")))
            add_row(
                name if position == 0 else "",
                _window_label(window),
                quota_bar(window.get("remaining_percent"), exhausted=bool(window.get("exhausted"))),
                str(window.get("reset_label") or "—"),
            )

    parts: list[RenderableType] = [table] if rows else []
    for group in groups:
        wanted = _norm(group)
        if not any(wanted in label for label in seen):
            parts.append(
                Text.assemble(
                    ("нет данных для ", "muted"),
                    (group, "warning"),
                    (" — inspector не отдаёт такую группу (см. QUOTA_MODEL_GROUPS)", "muted"),
                )
            )
    equivalents = _equivalents_text(_equivalents(snapshot.summary, [g.lower() for g in groups]))
    if equivalents is not None:
        parts.append(equivalents)

    title = "[header]CPA Quota[/header]"
    if single_group:
        title += f" [info]· {escape(groups[0])}[/info]"
    return Panel(
        Group(*parts),
        title=title,
        title_align="left",
        subtitle=_summary_line(snapshot, len(rows), exhausted=exhausted, low=low, errors=errors),
        subtitle_align="right",
        border_style=_border_for(exhausted, low),
        padding=(0, 1),
        expand=False,
    )


# ============================================================================
# Full view (all providers, all windows)
# ============================================================================


def _account_block(report: dict[str, Any], now: datetime) -> RenderableType:
    header = Text.assemble(
        (short_account(report.get("name")), "model"),
        "  ",
        (str(report.get("plan_type") or "unknown"), "badge"),
        "  ",
        status_text(report.get("status")),
    )
    parts: list[RenderableType] = [header]

    if report.get("error"):
        parts.append(Text(f"error: {report['error']}", style="error.soft"))

    windows = _sort_by_reset(_windows(report), now)
    if windows:
        table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
        table.add_column(style="info", min_width=18, max_width=28, overflow="ellipsis", no_wrap=True)
        table.add_column(min_width=BAR_WIDTH + 5, no_wrap=True)
        table.add_column(style="muted", no_wrap=True)
        for window in windows:
            table.add_row(
                _window_label(window),
                quota_bar(window.get("remaining_percent"), exhausted=bool(window.get("exhausted"))),
                str(window.get("reset_label") or "—"),
            )
        parts.append(table)
    else:
        parts.append(Text("нет окон квот", style="muted"))

    meta = report.get("meta_fields")
    if isinstance(meta, dict):
        values = list(
            dict.fromkeys(str(meta[k]) for k in ("channel", "project", "paid_tier", "tier") if meta.get(k))
        )
        if values:
            parts.append(Text(" · ".join(values), style="muted"))

    return Group(*parts)


def _full(snapshot: QuotaSnapshot) -> RenderableType:
    summary = snapshot.summary
    border = _border(summary)
    parts: list[RenderableType] = []
    now = datetime.now()

    by_provider: dict[str, list[dict[str, Any]]] = {}
    for report in sorted(snapshot.reports, key=lambda r: _earliest_reset(_windows(r), now)):
        by_provider.setdefault(str(report.get("provider") or "unknown"), []).append(report)

    for provider, reports in by_provider.items():
        blocks: list[RenderableType] = []
        for index, report in enumerate(reports):
            if index:
                blocks.append(Rule(style="bright_black"))
            blocks.append(_account_block(report, now))
        parts.append(
            Panel(
                Group(*blocks),
                title=f"[header]{_PROVIDER_TITLES.get(provider, provider)}[/header] [muted]{len(reports)} акк.[/muted]",
                title_align="left",
                border_style=border,
                padding=(0, 1),
            )
        )

    rows: list[tuple[str, Any]] = []
    for provider_summary in snapshot.data.get("provider_summaries") or []:
        if not isinstance(provider_summary, dict):
            continue
        title = str(provider_summary.get("title") or provider_summary.get("provider") or "Provider")
        plans = provider_summary.get("plans") or {}
        statuses = provider_summary.get("statuses") or {}
        detail = Text.assemble(
            (str(provider_summary.get("accounts", 0)), "bold"),
            ("  plans ", "muted"),
            ", ".join(f"{k}:{v}" for k, v in sorted(plans.items())) or "—",
            ("  statuses ", "muted"),
            ", ".join(f"{k}:{v}" for k, v in sorted(statuses.items())) or "—",
        )
        rows.append((title, detail))
        for extra in provider_summary.get("extras") or []:
            if isinstance(extra, dict):
                rows.append((f"  {extra.get('label', '?')}", Text(str(extra.get("value", "")), style="info")))

    for key, value in _equivalents(summary):
        rows.append((str(key), Text(f"{value}%", style="info")))

    parts.append(
        Panel(
            kv_table(rows) if rows else Text("—", style="muted"),
            title="[summary]Summary[/summary]",
            title_align="left",
            subtitle=_summary_line(snapshot, len(snapshot.reports)),
            subtitle_align="right",
            border_style=border,
            padding=(0, 1),
        )
    )
    return Group(*parts)


def render_quota(snapshot: QuotaSnapshot, *, filter_group: str, groups: tuple[str, ...]) -> RenderableType:
    """Compact table of ``groups`` unless ``QUOTA_FILTER_MODEL_GROUP`` is empty or ``all``."""
    target = filter_group.strip().lower()
    if target and target != "all" and groups:
        return _compact(snapshot, groups)
    return _full(snapshot)
