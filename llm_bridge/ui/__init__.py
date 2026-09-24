"""Console UI: Rich console, log handler, panels, live dashboard, reporter."""

from .console import configure_stdio, console, setup_logging
from .dashboard import Dashboard, LiveDisplay
from .reporter import ConsoleReporter

__all__ = [
    "ConsoleReporter",
    "Dashboard",
    "LiveDisplay",
    "configure_stdio",
    "console",
    "setup_logging",
]
