"""Shared Rich console, theme and the logging bridge onto it."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Iterable
from contextlib import suppress

from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.traceback import Traceback

THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "yellow",
        "warning.strong": "bold yellow",
        "error": "bold red",
        "error.soft": "red",
        "model": "bold magenta",
        "model.dim": "magenta",
        "replace": "bold blue",
        "tokens": "cyan",
        "tokens.in": "cyan",
        "tokens.out": "bold cyan",
        "summary": "bold green",
        "muted": "dim",
        "req_id": "dim cyan",
        "tool": "yellow",
        "system": "dim green",
        "skip": "dim yellow",
        "header": "bold bright_white",
        "badge": "bold cyan",
        "bar.high": "green",
        "bar.mid": "yellow",
        "bar.low": "red",
        "bar.empty": "bright_black",
        "kv.key": "dim cyan",
        "kv.val": "default",
        "log.time": "dim",
        "status.2xx": "green",
        "status.4xx": "yellow",
        "status.5xx": "red",
        "phase": "cyan",
        "phase.think": "magenta",
        "phase.wait": "dim",
        "phase.retry": "yellow",
    }
)

console = Console(theme=THEME, highlight=False)


def configure_stdio() -> None:
    """UTF-8 output on Windows consoles (box drawing, arrows, bars)."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


# ============================================================================
# Log lines
# ============================================================================


def log_line(icon: str, icon_style: str, body: Text, *, when: float | None = None) -> Table:
    """``HH:MM:SS <icon> <body>`` with a hanging indent for wrapped text."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="log.time", no_wrap=True)
    grid.add_column(no_wrap=True, width=1)
    grid.add_column(overflow="fold")
    stamp = time.strftime("%H:%M:%S", time.localtime(when if when is not None else time.time()))
    grid.add_row(stamp, Text(icon, style=icon_style), body)
    return grid


def print_line(icon: str, icon_style: str, *parts: Text | str | tuple[str, str]) -> None:
    console.print(log_line(icon, icon_style, Text.assemble(*parts)))


def print_block(*renderables: RenderableType, spaced: bool = True) -> None:
    if spaced:
        console.print()
    for renderable in renderables:
        console.print(renderable)
    if spaced:
        console.print()


_LEVELS: dict[int, tuple[str, str, str]] = {
    logging.DEBUG: ("·", "muted", "muted"),
    logging.INFO: ("•", "info", ""),
    logging.WARNING: ("⚠", "warning.strong", "warning"),
    logging.ERROR: ("✗", "error", "error.soft"),
    logging.CRITICAL: ("✗", "error", "error"),
}


def _level_style(levelno: int) -> tuple[str, str, str]:
    for level in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO):
        if levelno >= level:
            return _LEVELS[level]
    return _LEVELS[logging.DEBUG]


def _suppressed_modules() -> Iterable[object]:
    modules = []
    for name in ("httpx", "httpcore", "anyio", "starlette", "uvicorn", "fastapi"):
        module = sys.modules.get(name)
        if module is not None:
            modules.append(module)
    return modules


class ConsoleLogHandler(logging.Handler):
    """Renders log records on the shared console (plays well with ``rich.Live``).

    A ``rich.text.Text`` passed as the log message is printed as-is, so callers
    can style output without markup (and without markup-injection from
    upstream error strings).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            icon, icon_style, text_style = _level_style(record.levelno)
            if isinstance(record.msg, Text) and not record.args:
                body = record.msg.copy()
            else:
                body = Text(record.getMessage(), style=text_style)

            if record.name.startswith(("uvicorn", "asyncio", "py.warnings")):
                body = Text.assemble((f"{record.name}: ", "muted"), body)

            console.print(log_line(icon, icon_style, body, when=record.created))

            if record.exc_info and record.exc_info[1] is not None:
                exc_type, exc_value, exc_tb = record.exc_info
                console.print(
                    Traceback.from_exception(
                        exc_type,  # type: ignore[arg-type]
                        exc_value,
                        exc_tb,
                        show_locals=False,
                        suppress=_suppressed_modules(),
                        max_frames=20,
                    )
                )
        except Exception:
            self.handleError(record)


class _UvicornMessages(logging.Filter):
    """Tame uvicorn's shutdown noise.

    Requests cancelled on shutdown are already reported by the bridge as ``⊘``,
    so uvicorn's tracebacks for ``CancelledError`` / ``KeyboardInterrupt`` are
    dropped, and the graceful-timeout message is reworded.
    """

    _QUIET = ("CancelledError", "KeyboardInterrupt")

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
            return False
        message = record.getMessage()
        if message.startswith("Traceback") and message.rstrip().endswith(self._QUIET):
            return False
        if "timeout graceful shutdown exceeded" in message:
            record.msg = "прерываю активные запросы: истёк BRIDGE_SHUTDOWN_TIMEOUT"
            record.args = ()
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True


def setup_logging(*, debug: bool = False) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(ConsoleLogHandler())
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    for noisy in ("httpx", "httpcore", "hpack", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.addFilter(_UvicornMessages())
    logging.captureWarnings(True)
