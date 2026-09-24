"""Command-line entry point: argument parsing, profile choice, server run.

Exit codes:
    0   normal stop (Ctrl+C included)
    1   runtime error (port busy, unexpected exception)
    2   configuration error (.env / arguments)
    3   application failed to start
    130 cancelled before the server started
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import signal
import socket
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from types import FrameType

import uvicorn
from rich.text import Text

from . import __version__
from .app import Bridge, create_app
from .config import ConfigError, RoutingProfile, Settings, build_profiles, find_profile
from .ui import configure_stdio, console, setup_logging
from .ui.console import print_line
from .ui.panels import error_panel, profile_picker, profiles_table

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_STARTUP = 3
EXIT_CANCELLED = 130

_ADDR_IN_USE = {errno.EADDRINUSE, 10048}  # 10048 = WSAEADDRINUSE
_ADDR_ACCESS = {errno.EACCES, 10013}  # 10013 = WSAEACCES (excluded port range)
_ADDR_NOT_AVAIL = {errno.EADDRNOTAVAIL, 10049}


# ============================================================================
# Arguments
# ============================================================================


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description="LLM Bridge — политика моделей, ретраи и fallback между Claude Code и CLIProxyAPI.",
    )
    parser.add_argument(
        "-p",
        "--profile",
        metavar="N|KEY",
        help="профиль маршрутизации (номер или ключ) — без интерактивного выбора",
    )
    parser.add_argument("--list-profiles", action="store_true", help="показать профили и выйти")
    parser.add_argument("--host", help="адрес для прослушивания (перекрывает BRIDGE_HOST)")
    parser.add_argument("--port", type=int, help="порт (перекрывает BRIDGE_PORT)")
    parser.add_argument("--no-live", action="store_true", help="без live-панели: только поток логов")
    parser.add_argument("--env-file", type=Path, help="путь к .env (по умолчанию .env рядом с bridge.py)")
    parser.add_argument("--debug", action="store_true", help="подробные логи")
    parser.add_argument("--version", action="version", version=f"llm-bridge {__version__}")
    return parser.parse_args(argv)


def load_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.load(args.env_file)
    overrides: dict[str, object] = {}
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            raise ConfigError([f"--port {args.port}: допустимо 1–65535"])
        overrides["port"] = args.port
    if args.no_live:
        overrides["live_display"] = False
    return settings.with_overrides(**overrides) if overrides else settings


# ============================================================================
# Profile selection
# ============================================================================


def choose_profile(
    args: argparse.Namespace, settings: Settings, profiles: tuple[RoutingProfile, ...]
) -> RoutingProfile:
    requested = args.profile or settings.default_profile
    if requested:
        profile = find_profile(profiles, requested)
        if profile is None:
            keys = ", ".join(p.key for p in profiles)
            source = "--profile" if args.profile else "BRIDGE_PROFILE"
            raise ConfigError([f"{source}={requested!r}: неизвестный профиль (1–{len(profiles)} или {keys})"])
        return profile

    if not (sys.stdin.isatty() and console.is_terminal):
        print_line(
            "•",
            "info",
            ("Неинтерактивный запуск — профиль по умолчанию: ", "muted"),
            (profiles[0].title, "model"),
        )
        return profiles[0]

    return prompt_profile(profiles)


def prompt_profile(profiles: tuple[RoutingProfile, ...]) -> RoutingProfile:
    """Interactive picker. Ctrl+C / EOF propagate as KeyboardInterrupt / EOFError."""
    default = profiles[0]
    console.print()
    console.print(profile_picker(profiles, default))
    while True:
        answer = console.input(f"[info]Выбор[/info] [muted](1–{len(profiles)} или ключ, Enter = 1)[/muted]: ")
        if not answer.strip():
            return default
        profile = find_profile(profiles, answer)
        if profile is not None:
            return profile
        console.print(
            f"[warning]Нет такого профиля: {answer.strip()!r}. Введите 1–{len(profiles)}.[/warning]"
        )


# ============================================================================
# Socket / server
# ============================================================================


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket up front so address errors are reported clearly."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    family, socktype, proto, _, address = infos[0]
    sock = socket.socket(family, socktype, proto)
    try:
        if sys.platform != "win32":
            # On POSIX this only skips TIME_WAIT; on Windows it would allow port hijacking.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
            with suppress(OSError):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(address)
    except BaseException:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def describe_bind_error(exc: OSError, host: str, port: int) -> tuple[str, str]:
    code = exc.errno or getattr(exc, "winerror", None)
    if isinstance(exc, socket.gaierror):
        return f"Не удалось разрешить адрес {host!r}: {exc}", "Проверьте BRIDGE_HOST в .env."
    if code in _ADDR_IN_USE:
        return (
            f"Порт {port} уже занят ({host}:{port}).",
            "Скорее всего bridge уже запущен в другой вкладке. Закройте его или задайте другой BRIDGE_PORT / --port.",
        )
    if code in _ADDR_ACCESS:
        return (
            f"Нет доступа к порту {port} ({exc}).",
            "На Windows порт может входить в зарезервированный диапазон: netsh int ipv4 show excludedportrange tcp",
        )
    if code in _ADDR_NOT_AVAIL:
        return f"Адрес {host} недоступен на этой машине ({exc}).", "Используйте 127.0.0.1 или 0.0.0.0."
    return f"Не удалось открыть {host}:{port}: {exc}", ""


class BridgeServer(uvicorn.Server):
    """uvicorn.Server that announces shutdown progress on Ctrl+C."""

    def __init__(self, config: uvicorn.Config, on_signal: Callable[[bool], None]) -> None:
        super().__init__(config)
        self._on_signal = on_signal

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        forced = self.should_exit and sig == signal.SIGINT
        super().handle_exit(sig, frame)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.call_soon_threadsafe(self._on_signal, forced)
        else:
            self._on_signal(forced)


def _signal_notice(bridge: Bridge, settings: Settings) -> Callable[[bool], None]:
    def notify(forced: bool) -> None:
        if forced:
            print_line("⏹", "error", ("Принудительная остановка", "error.soft"))
            return
        active = len(bridge.proxy.active)
        if active:
            wait = f"до {settings.shutdown_timeout:g}s" if settings.shutdown_timeout else "без ожидания"
            print_line(
                "⏹",
                "warning.strong",
                (f"Остановка… активных запросов: {active}, жду {wait}", "warning"),
                ("  (Ctrl+C ещё раз — сразу)", "muted"),
            )
        else:
            print_line("⏹", "warning.strong", ("Остановка…", "warning"))

    return notify


def run_server(settings: Settings, profile: RoutingProfile, sock: socket.socket) -> int:
    app, bridge = create_app(settings, profile)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        log_level="warning",
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=settings.shutdown_timeout,  # type: ignore[arg-type]
    )
    server = BridgeServer(config, _signal_notice(bridge, settings))

    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        # uvicorn re-raises the captured SIGINT after a graceful shutdown.
        pass
    finally:
        # Safety net for paths where the lifespan shutdown did not run.
        bridge.live.stop()
        with suppress(OSError):
            sock.close()

    if not server.started:
        console.print(
            error_panel(
                "Bridge не запустился",
                ["Приложение не смогло стартовать — подробности в логе выше."],
            )
        )
        return EXIT_STARTUP

    bridge.print_summary()
    return EXIT_OK


# ============================================================================
# main
# ============================================================================


def _swallow_late_interrupt() -> None:
    """Windows may deliver KeyboardInterrupt right after EOFError from input()."""
    with suppress(KeyboardInterrupt):
        time.sleep(0.05)


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    setup_logging(debug=args.debug)

    try:
        settings = load_settings(args)
        profiles = build_profiles(settings)
        if args.list_profiles:
            console.print(profiles_table(profiles, default=profiles[0]))
            return EXIT_OK
        profile = choose_profile(args, settings, profiles)
    except ConfigError as exc:
        console.print(
            error_panel(
                "Ошибка конфигурации",
                [Text(f"• {problem}") for problem in exc.problems],
                hint="Исправьте .env (шаблон — .env.example) или аргументы запуска.",
            )
        )
        return EXIT_CONFIG
    except (KeyboardInterrupt, EOFError):
        _swallow_late_interrupt()
        console.print()
        console.print("[muted]Запуск отменён.[/muted]")
        return EXIT_CANCELLED

    try:
        sock = bind_socket(settings.host, settings.port)
    except OSError as exc:
        message, hint = describe_bind_error(exc, settings.host, settings.port)
        console.print(error_panel("Не удалось занять порт", [message], hint=hint or None))
        return EXIT_ERROR

    try:
        return run_server(settings, profile, sock)
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception:
        console.print_exception(show_locals=False, max_frames=20)
        console.print(
            error_panel("Bridge аварийно остановлен", ["Непредвиденная ошибка — трассировка выше."])
        )
        return EXIT_ERROR
