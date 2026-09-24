"""FastAPI application: wiring, lifespan and routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from rich.text import Text
from starlette.exceptions import HTTPException as StarletteHTTPException

from .availability import ModelAvailability
from .config import RoutingProfile, Settings
from .errors import anthropic_error
from .proxy import MessagesProxy
from .quota import (
    QuotaInspector,
    QuotaMonitor,
    QuotaSnapshot,
    assumed_quota_models,
    profile_quota_groups,
)
from .routing import Router
from .stats import Stats
from .ui import ConsoleReporter, Dashboard, LiveDisplay, console
from .ui.fmt import fmt_seconds_short
from .ui.panels import session_summary, startup_banner
from .ui.quota_view import render_quota
from .upstream import UpstreamClient

log = logging.getLogger("llm_bridge")

QUOTA_TASK_STOP_TIMEOUT = 3.0
REQUESTS_SETTLE_TIMEOUT = 1.0


@dataclass
class Bridge:
    """Everything the running bridge owns; stored on ``app.state.bridge``."""

    settings: Settings
    profile: RoutingProfile
    router: Router
    availability: ModelAvailability
    stats: Stats
    client: httpx.AsyncClient
    live: LiveDisplay
    reporter: ConsoleReporter
    proxy: MessagesProxy
    quota_inspector: QuotaInspector
    quota_groups: tuple[str, ...] = ()
    quota_monitor: QuotaMonitor | None = None
    quota_task: asyncio.Task[None] | None = None
    summary_printed: bool = False

    # ------------------------------------------------------------------

    def quota_status(self) -> Text:
        settings = self.settings
        if not settings.quota_enabled:
            return Text("выключены (QUOTA_POLL_INTERVAL=0)", style="muted")
        reason = self.quota_inspector.unavailable_reason()
        if reason:
            return Text(reason, style="warning")
        text = Text.assemble(
            (f"опрос каждые {fmt_seconds_short(settings.quota_poll_interval)}", "bold"),
            ("  группа " if len(self.quota_groups) == 1 else "  группы ", "muted"),
            (", ".join(self.quota_groups) or "все", "info"),
            ("" if settings.quota_show_groups else " (по профилю)", "muted"),
        )
        assumed = assumed_quota_models(self.profile, settings)
        if assumed:
            text.append(
                f"\n{', '.join(assumed)}: своей квоты в inspector нет — показана предполагаемая группа "
                "(поменять: QUOTA_MODEL_GROUPS)",
                style="warning",
            )
        return text

    def show_quota(self, snapshot: QuotaSnapshot) -> None:
        self.live.show_quota(
            render_quota(
                snapshot,
                filter_group=self.settings.quota_filter_group,
                groups=self.quota_groups,
            )
        )

    async def startup(self) -> None:
        console.print()
        console.print(
            startup_banner(
                self.settings, self.profile, quota_status=self.quota_status(), live=self.live.enabled
            )
        )
        console.print()

        if self.settings.quota_enabled:
            reason = self.quota_inspector.unavailable_reason()
            if reason:
                log.warning(reason)
            else:
                self.quota_monitor = QuotaMonitor(
                    self.quota_inspector, self.settings.quota_poll_interval, self.show_quota
                )
                # Runs in the background: startup never waits for the inspector.
                self.quota_task = asyncio.create_task(self.quota_monitor.run(), name="quota-monitor")

        self.live.start()

    async def shutdown(self, *, summary: bool = True) -> None:
        """Idempotent teardown; each step is isolated so one failure cannot skip the rest."""
        task, self.quota_task = self.quota_task, None
        if task is not None:
            task.cancel()
            with suppress(Exception):
                await asyncio.wait({task}, timeout=QUOTA_TASK_STOP_TIMEOUT)
            if task.done() and not task.cancelled() and task.exception() is not None:
                log.error("quota monitor завершился с ошибкой: %s", task.exception())

        # Requests cancelled by uvicorn finish on the next loop iterations;
        # give them a moment so their lines land before the summary.
        with suppress(Exception):
            await self.proxy.wait_idle(REQUESTS_SETTLE_TIMEOUT)
        self.proxy.abandon_all()
        with suppress(Exception):
            await self.proxy.drain()

        try:
            await self.client.aclose()
        except Exception as exc:
            log.warning("не удалось закрыть HTTP-клиент: %s", exc)

        self.live.stop()
        if summary:
            self.print_summary()

    def print_summary(self) -> None:
        if self.summary_printed:
            return
        self.summary_printed = True
        console.print()
        console.print(session_summary(self.stats, self.profile))
        console.print()

    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        stats = self.stats
        cooldowns = self.availability.snapshot()
        snapshot = self.quota_monitor.snapshot if self.quota_monitor else None
        return {
            "status": "ok",
            # Backwards-compatible top-level counters.
            "requests": stats.requests,
            "errors": stats.failed,
            "total_tokens": stats.total_tokens,
            "stats": stats.as_dict(),
            "active": [
                {
                    "id": ctx.req_id,
                    "model": ctx.model or ctx.original_model,
                    "phase": ctx.phase.value,
                    "elapsed_s": round(ctx.elapsed, 1),
                }
                for ctx in list(self.proxy.active.values())
            ],
            "unavailable": {model: round(left, 1) for model, (left, _) in cooldowns.items()},
            "unavailable_reasons": {model: reason for model, (_, reason) in cooldowns.items()},
            "quota": snapshot.health_view() if snapshot else {},
            "quota_error": self.quota_monitor.last_error if self.quota_monitor else None,
            "policy": {
                "profile": self.profile.key,
                "profile_title": self.profile.title,
                "passthrough": self.settings.passthrough_default,
                "heavy_models": list(self.profile.heavy_models),
                "haiku_rewrite": self.router.haiku_rewrite(),
                "same_model_retries": self.settings.same_model_retries,
                "quota_groups": list(self.quota_groups),
            },
        }


def build_bridge(settings: Settings, profile: RoutingProfile) -> Bridge:
    availability = ModelAvailability()
    stats = Stats()
    router = Router(settings, profile)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout, connect=settings.http_connect_timeout),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
        ),
    )

    # The dashboard reads in-flight requests from the proxy (bound lazily).
    dashboard = Dashboard(settings, stats, availability, lambda: list(proxy.active.values()))
    live = LiveDisplay(dashboard, enabled=settings.live_display)
    reporter = ConsoleReporter(live)
    upstream = UpstreamClient(settings, client, availability, stats, reporter)
    proxy = MessagesProxy(router, upstream, availability, stats, reporter)

    return Bridge(
        settings=settings,
        profile=profile,
        router=router,
        availability=availability,
        stats=stats,
        client=client,
        live=live,
        reporter=reporter,
        proxy=proxy,
        quota_inspector=QuotaInspector(settings),
        quota_groups=profile_quota_groups(profile, settings),
    )


def create_app(settings: Settings, profile: RoutingProfile) -> tuple[FastAPI, Bridge]:
    bridge = build_bridge(settings, profile)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        started = False
        try:
            await bridge.startup()
            started = True
            yield
        finally:
            await bridge.shutdown(summary=started)

    app = FastAPI(
        title="LLM Bridge",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bridge = bridge

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return bridge.health()

    @app.post("/v1/messages", response_model=None)
    async def messages(request: Request) -> Response:
        return await bridge.proxy.messages(request)

    @app.post("/v1/messages/count_tokens", response_model=None)
    async def count_tokens(request: Request) -> Response:
        return await bridge.proxy.count_tokens(request)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code in (404, 405):
            bridge.reporter.unknown_route(request.method, request.url.path)
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            anthropic_error(detail, status=exc.status_code),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    return app, bridge
