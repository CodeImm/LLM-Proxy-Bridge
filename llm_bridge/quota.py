"""CLIProxyAPI Quota Inspector integration.

The inspector binary runs as a subprocess in a worker thread. It is killed
on timeout *and* on cancellation, so Ctrl+C never waits for it to finish.
The management key is passed via environment, not argv, so it does not show
up in process listings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .config import REPO_ROOT, RoutingProfile, Settings

log = logging.getLogger("llm_bridge.quota")


class QuotaError(Exception):
    pass


# ============================================================================
# Model → quota group
# ============================================================================

# Mirrors ``antigravityModelGroups`` in CLIProxyAPI-Quota-Inspector/providers.go:
# the inspector reports one quota window per group, not per model.
INSPECTOR_MODEL_GROUPS: dict[str, str] = {
    "claude-sonnet-4-6": "Claude/GPT",
    "claude-opus-4-6-thinking": "Claude/GPT",
    "gpt-oss-120b-medium": "Claude/GPT",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro Series",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro Series",
    "gemini-3-pro-high": "Gemini 3 Pro",
    "gemini-3-pro-low": "Gemini 3 Pro",
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.5-flash-thinking": "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
    "gemini-3-flash": "Gemini 3 Flash",
}

# Models the inspector has no window for, mapped to the closest group.
# These are assumptions; override with QUOTA_MODEL_GROUPS in .env.
ASSUMED_MODEL_GROUPS: dict[str, str] = {
    "gemini-pro-agent": "Gemini 3.1 Pro Series",
    "gemini-3.7-flash-high": "Gemini 3 Flash",
}


def quota_group_for_model(model: str, overrides: dict[str, str] | None = None) -> str:
    """Inspector quota group a model is billed against.

    Order: QUOTA_MODEL_GROUPS → inspector table → assumptions → Claude/GPT
    for any ``claude-*`` / ``gpt-*`` → the model id itself (the inspector
    labels ungrouped Gemini CLI models by id).
    """
    key = model.strip().lower()
    for table in (overrides or {}, INSPECTOR_MODEL_GROUPS, ASSUMED_MODEL_GROUPS):
        for name, group in table.items():
            if name.lower() == key:
                return group
    if key.startswith(("claude-", "gpt-")):
        return "Claude/GPT"
    return model


def assumed_quota_models(profile: RoutingProfile, settings: Settings) -> list[str]:
    """Profile models whose quota group is a guess (ASSUMED_MODEL_GROUPS, not overridden)."""
    if settings.quota_show_groups:
        return []
    overridden = {name.lower() for name, _ in settings.quota_model_groups}
    assumed = {name.lower() for name in ASSUMED_MODEL_GROUPS}
    return [m for m in profile.models if m.lower() in assumed and m.lower() not in overridden]


def profile_quota_groups(profile: RoutingProfile, settings: Settings) -> tuple[str, ...]:
    """Quota groups to display: QUOTA_SHOW_GROUPS if set, else the profile's models."""
    if settings.quota_show_groups:
        return settings.quota_show_groups
    overrides = dict(settings.quota_model_groups)
    return tuple(dict.fromkeys(quota_group_for_model(m, overrides) for m in profile.models))


# ============================================================================
# Snapshot
# ============================================================================


@dataclass(slots=True)
class QuotaSnapshot:
    data: dict[str, Any]
    fetched_at: float  # time.time()

    @property
    def summary(self) -> dict[str, Any]:
        summary = self.data.get("summary")
        return summary if isinstance(summary, dict) else {}

    @property
    def reports(self) -> list[dict[str, Any]]:
        reports = self.data.get("reports")
        return [r for r in reports if isinstance(r, dict)] if isinstance(reports, list) else []

    def health_view(self) -> dict[str, Any]:
        """Compact JSON for ``GET /health``."""
        summary = self.summary
        keys = (
            "accounts",
            "exhausted_accounts",
            "low_accounts",
            "error_accounts",
            "status_counts",
            "provider_counts",
            "exhausted_names",
            "low_names",
            "plan_counts",
            "antigravity_equivalents",
            "gemini_equivalents",
        )
        view: dict[str, Any] = {"ts": self.fetched_at}
        view.update({key: summary.get(key) for key in keys})
        view["accounts_detail"] = [
            {
                "name": report.get("name"),
                "provider": report.get("provider"),
                "plan_type": report.get("plan_type"),
                "status": report.get("status"),
                "windows": [
                    {
                        "label": window.get("label") or window.get("id"),
                        "remaining_percent": window.get("remaining_percent"),
                        "reset_label": window.get("reset_label"),
                        "exhausted": window.get("exhausted"),
                    }
                    for window in (report.get("windows") or [])
                    if isinstance(window, dict)
                ],
            }
            for report in self.reports
        ]
        return view


# ============================================================================
# Inspector subprocess
# ============================================================================


def _explain_failure(output: str) -> str:
    lowered = output.lower()
    if "404" in lowered:
        return "Management API выключен в CPA (пустой remote-management.secret-key)"
    if "401" in lowered or "unauthorized" in lowered:
        return "CPA_MANAGEMENT_KEY не совпадает с secret-key в config.yaml CPA"
    if "connection refused" in lowered or "actively refused" in lowered:
        return "CLIProxyAPI не запущен"
    return ""


class QuotaInspector:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.quota_inspector_path
        self.base_url = settings.cpa_base_url
        self.key = settings.management_key
        self.timeout = settings.quota_inspector_timeout

    def unavailable_reason(self) -> str | None:
        if not self.path.is_file():
            return f"quota inspector не найден: {self.path}"
        if not self.key:
            return "quota inspector отключён: задайте CPA_MANAGEMENT_KEY в .env"
        return None

    async def fetch(self) -> dict[str, Any]:
        command = [str(self.path), "--cpa-base-url", self.base_url, "--json", "--no-progress"]
        env = {**os.environ, "CPA_MANAGEMENT_KEY": self.key, "MANAGEMENT_PASSWORD": self.key}
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(REPO_ROOT),
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise QuotaError(f"не удалось запустить inspector: {exc}") from None

        try:
            stdout, stderr = await _communicate(process, self.timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            raise QuotaError(f"inspector не ответил за {self.timeout:.0f}s") from None
        except BaseException:
            # Cancelled (shutdown): kill it so nothing outlives the bridge.
            _kill(process)
            raise

        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()

        if process.returncode != 0:
            detail = " ".join((err or out or "no output").split())[:300]
            hint = _explain_failure(detail)
            message = f"inspector exit {process.returncode}: {detail}"
            raise QuotaError(f"{message} — {hint}" if hint else message)

        if not out:
            raise QuotaError("inspector вернул пустой ответ")
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise QuotaError(f"inspector вернул невалидный JSON: {exc}") from None
        if not isinstance(data, dict):
            raise QuotaError("inspector вернул неожиданный JSON (ожидался объект)")
        return data


def _kill(process: subprocess.Popen[bytes]) -> None:
    with suppress(OSError):
        process.kill()


async def _communicate(process: subprocess.Popen[bytes], timeout: float) -> tuple[bytes, bytes]:
    """``process.communicate`` in a daemon thread.

    Unlike ``asyncio.to_thread`` (default executor, joined at interpreter exit),
    a daemon thread can never keep the bridge alive during shutdown, even if a
    killed child left its pipes open.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[bytes, bytes]] = loop.create_future()

    def deliver(setter: Callable[..., None], value: Any) -> None:
        if not future.done():
            setter(value)

    def worker() -> None:
        try:
            result = process.communicate(timeout=timeout)
        except BaseException as exc:
            callback, value = future.set_exception, exc
        else:
            callback, value = future.set_result, result
        with suppress(RuntimeError):  # event loop already closed (shutdown)
            loop.call_soon_threadsafe(deliver, callback, value)

    threading.Thread(target=worker, name="quota-inspector", daemon=True).start()
    return await future


# ============================================================================
# Poll loop
# ============================================================================


class QuotaMonitor:
    def __init__(
        self,
        inspector: QuotaInspector,
        interval: float,
        on_update: Callable[[QuotaSnapshot], None],
    ) -> None:
        self.inspector = inspector
        self.interval = interval
        self.on_update = on_update
        self.snapshot: QuotaSnapshot | None = None
        self.last_error: str | None = None
        self.next_refresh_at: float | None = None  # time.monotonic()

    async def run(self) -> None:
        """Refresh immediately, then every ``interval`` seconds, until cancelled."""
        while True:
            await self.refresh()
            self.next_refresh_at = time.monotonic() + self.interval
            await asyncio.sleep(self.interval)

    async def refresh(self) -> None:
        try:
            data = await self.inspector.fetch()
        except QuotaError as exc:
            message = str(exc)
            if message != self.last_error:
                log.warning("quota: %s", message)
            self.last_error = message
            return
        except Exception:
            log.exception("quota: непредвиденная ошибка inspector")
            return

        if self.last_error is not None:
            log.info("quota: inspector снова отвечает")
            self.last_error = None

        self.snapshot = QuotaSnapshot(data, time.time())
        try:
            self.on_update(self.snapshot)
        except Exception:
            log.exception("quota: не удалось отрисовать отчёт")
