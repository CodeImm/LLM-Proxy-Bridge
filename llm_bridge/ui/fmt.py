"""Small formatting helpers for console output."""

from __future__ import annotations


def fmt_tokens(value: int) -> str:
    if value < 10_000:
        return f"{value:,}"
    if value < 100_000:
        return f"{value / 1000:.1f}k"
    if value < 1_000_000:
        return f"{value / 1000:.0f}k"
    return f"{value / 1_000_000:.2f}M"


def fmt_duration(seconds: float) -> str:
    """``0.4s``, ``12.3s``, ``2m05s``, ``1h02m``."""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fmt_seconds_short(seconds: float) -> str:
    """Compact TTL: ``42s``, ``5m``, ``30m``, ``2h``."""
    seconds = max(0, round(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h".replace(".0h", "h")


def short_model(model: str) -> str:
    """``claude-opus-4-6-thinking`` → ``opus-4-6-thinking``; ``gemini-x`` → ``x``."""
    for prefix in ("claude-", "gemini-"):
        if model.startswith(prefix) and len(model) > len(prefix):
            return model[len(prefix) :]
    return model


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"
