from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


# ============================================================================
# Bootstrap
# ============================================================================

_REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(_REPO_ROOT / ".env")

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================================================
# Theme / logging
# ============================================================================

_THEME = Theme({
    "info": "cyan",
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
    "model": "bold magenta",
    "tokens": "bold cyan",
    "replace": "bold blue",
    "summary": "bold green",
    "muted": "dim white",
    "req_id": "dim cyan",
    "tool": "bold yellow",
    "system": "dim green",
    "skip": "dim yellow",
    "header": "bold bright_white",
    "badge": "bold cyan",
    "bar.high": "green",
    "bar.mid": "yellow",
    "bar.low": "red",
    "bar.empty": "dim white",
    "kv.key": "dim cyan",
    "kv.val": "white",
})

console = Console(theme=_THEME, highlight=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%H:%M:%S]",
    handlers=[
        RichHandler(
            console=console,
            show_path=False,
            show_level=False,
            rich_tracebacks=True,
            markup=True,
        )
    ],
)

log = logging.getLogger("proxy")
logging.getLogger("httpx").setLevel(logging.WARNING)


# ============================================================================
# Environment helpers
# ============================================================================

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_csv(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ============================================================================
# Base configuration
# ============================================================================

PROXY_URL = _env("CPA_BASE_URL", "http://127.0.0.1:8317").rstrip("/")
BRIDGE_HOST = _env("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = _env_int("BRIDGE_PORT", 4000)

BRIDGE_PASSTHROUGH = _env_bool("BRIDGE_PASSTHROUGH", False)

SAME_MODEL_RETRIES = _env_int("SAME_MODEL_RETRIES", 4)
RETRY_BASE_DELAY = _env_float("RETRY_BASE_DELAY", 1.0)
RETRY_MAX_DELAY = _env_float("RETRY_MAX_DELAY", 10.0)

COOLDOWN_RATE = _env_float("COOLDOWN_RATE", 60.0)
COOLDOWN_AUTH = _env_float("COOLDOWN_AUTH", 1800.0)
COOLDOWN_TRANSIENT = _env_float("COOLDOWN_TRANSIENT", 30.0)
COOLDOWN_SKIP = _env_float("COOLDOWN_SKIP", 300.0)

HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT", 180.0)
HTTP_CONNECT_TIMEOUT = _env_float("HTTP_CONNECT_TIMEOUT", 15.0)
HTTP_MAX_CONNECTIONS = _env_int("HTTP_MAX_CONNECTIONS", 100)
HTTP_MAX_KEEPALIVE = _env_int("HTTP_MAX_KEEPALIVE", 20)

QUOTA_INSPECTOR_RAW = _env("QUOTA_INSPECTOR_PATH", "")
QUOTA_INSPECTOR = (
    Path(QUOTA_INSPECTOR_RAW)
    if QUOTA_INSPECTOR_RAW
    else _REPO_ROOT
    / "CLIProxyAPI-Quota-Inspector"
    / "cpa-quota-inspector.exe"
)

QUOTA_POLL_INTERVAL = _env_float("QUOTA_POLL_INTERVAL", 300.0)
QUOTA_INSPECTOR_TIMEOUT = _env_float("QUOTA_INSPECTOR_TIMEOUT", 120.0)
QUOTA_FILTER_MODEL_GROUP = _env(
    "QUOTA_FILTER_MODEL_GROUP",
    "Claude/GPT",
)
QUOTA_SHOW_GROUPS = _env_csv(
    "QUOTA_SHOW_GROUPS",
    [
        "Claude/GPT",
        "Gemini 3.1 Pro Series",
        "gemini-pro-agent",
        "gemini-3.7-flash-high",
    ],
)
QUOTA_LIVE_DISPLAY = _env_bool("QUOTA_LIVE_DISPLAY", True)

QUOTA_MANAGEMENT_KEY = (
    _env("CPA_MANAGEMENT_KEY")
    or _env("MANAGEMENT_PASSWORD")
    or ""
)

UPSTREAM_API_KEY = _env("UPSTREAM_API_KEY", "sk-antigravity")
ANTHROPIC_VERSION = _env("ANTHROPIC_VERSION", "2023-06-01")

UPSTREAM_HEADERS = {
    "x-api-key": UPSTREAM_API_KEY,
    "anthropic-version": ANTHROPIC_VERSION,
    "content-type": "application/json",
    "accept": "text/event-stream",
}


# ============================================================================
# Model routing profiles
# ============================================================================

@dataclass(frozen=True)
class RoutingProfile:
    key: str
    title: str
    description: str
    heavy_models: tuple[str, ...]
    haiku_model: str

    @property
    def heavy_label(self) -> str:
        return " → ".join(self.heavy_models)

    @property
    def haiku_label(self) -> str:
        return self.haiku_model


CLAUDE_HEAVY_DEFAULT = tuple(
    _env_csv(
        "CLAUDE_HEAVY_MODELS",
        [
            "claude-opus-4-6-thinking",
            "claude-sonnet-4-6",
        ],
    )
)

MODEL_GEMINI_PRO_AGENT = _env(
    "GEMINI_PRO_AGENT_MODEL",
    "gemini-pro-agent",
)

MODEL_GEMINI_FLASH_HIGH = _env(
    "GEMINI_FLASH_HIGH_MODEL",
    "gemini-3.7-flash-high",
)


PROFILES: tuple[RoutingProfile, ...] = (
    RoutingProfile(
        key="current",
        title="Текущая конфигурация",
        description="Heavy Claude → Opus → Sonnet; Haiku → Opus 4.6 Thinking",
        heavy_models=CLAUDE_HEAVY_DEFAULT,
        haiku_model="claude-opus-4-6-thinking",
    ),
    RoutingProfile(
        key="gemini-pro-agent",
        title="Gemini Pro Agent",
        description="Все Claude Heavy + Haiku → Gemini Pro Agent",
        heavy_models=(MODEL_GEMINI_PRO_AGENT,),
        haiku_model=MODEL_GEMINI_PRO_AGENT,
    ),
    RoutingProfile(
        key="gemini-3.7-flash-high",
        title="Gemini 3.7 Flash High",
        description="Все Claude Heavy + Haiku → Gemini 3.7 Flash High",
        heavy_models=(MODEL_GEMINI_FLASH_HIGH,),
        haiku_model=MODEL_GEMINI_FLASH_HIGH,
    ),
)


# Активный профиль выбирается перед запуском Uvicorn.
ACTIVE_PROFILE: RoutingProfile = PROFILES[0]


# ============================================================================
# Claude model aliases
# ============================================================================

_HAIKU_SOURCE_IDS = _env_csv(
    "HAIKU_SOURCE_MODELS",
    [
        "claude-haiku-4-5-20251001",
        "claude-haiku-4-5",
        "claude-haiku",
        "claude-3-5-haiku-20241022",
        "claude-3-haiku-20240307",
    ],
)

CLAUDE_HEAVY_ALIASES = frozenset(
    _env_csv(
        "CLAUDE_HEAVY_ALIASES",
        [
            "claude",
            "claude-opus",
            "claude-opus-4-6",
            "claude-opus-4-6-thinking",
            "claude-opus-4-7",
            "claude-sonnet",
            "claude-sonnet-4-6",
            "claude-3-5-sonnet-20241022",
            "claude-3-7-sonnet-20250219",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
        ],
    )
)


def get_model_rewrite() -> dict[str, str]:
    return {
        model: ACTIVE_PROFILE.haiku_model
        for model in _HAIKU_SOURCE_IDS
    }


# ============================================================================
# Interactive profile selection
# ============================================================================

def _profile_table() -> Table:
    table = Table(
        box=None,
        show_header=True,
        header_style="header",
        pad_edge=False,
        padding=(0, 1),
        expand=True,
    )

    table.add_column("#", style="info", width=3)
    table.add_column("Профиль", style="model", min_width=24)
    table.add_column("Heavy", style="kv.val")
    table.add_column("Haiku", style="replace")

    for index, profile in enumerate(PROFILES, start=1):
        table.add_row(
            str(index),
            profile.title,
            profile.heavy_label,
            profile.haiku_label,
        )

    return table


def select_profile() -> RoutingProfile:
    """
    Ask the user which routing profile should be used before Uvicorn starts.
    """

    console.print()
    console.print(
        Panel(
            Group(
                Text(
                    "Выберите конфигурацию моделей",
                    style="header",
                ),
                Text(
                    "Конфигурация будет применена до запуска LLM Proxy.",
                    style="muted",
                ),
                _profile_table(),
            ),
            title="[header]⚙  Model Profile[/header]",
            border_style="bright_blue",
            padding=(1, 1),
        )
    )

    default = "1"

    while True:
        try:
            choice = console.input(
                f"[info]Выбор[/info] [muted](1-{len(PROFILES)}, Enter = {default})[/muted]: "
            ).strip()

            if not choice:
                choice = default

            index = int(choice)

            if 1 <= index <= len(PROFILES):
                selected = PROFILES[index - 1]

                console.print()
                console.print(
                    Panel(
                        _kv_table([
                            ("Profile", Text(selected.title, style="model")),
                            ("Heavy", Text(selected.heavy_label, style="model")),
                            ("Haiku", Text(selected.haiku_label, style="replace")),
                        ]),
                        title="[success]✓  Selected[/success]",
                        border_style="green",
                        padding=(0, 1),
                    )
                )
                console.print()

                return selected

        except (ValueError, EOFError, KeyboardInterrupt):
            pass

        console.print(
            f"[warning]Введите число от 1 до {len(PROFILES)}.[/warning]"
        )


# ============================================================================
# Model availability / cooldown
# ============================================================================

class ModelAvailability:
    """Per-model cooldown with reason-specific TTL."""

    def __init__(self) -> None:
        self._down: dict[str, tuple[float, float]] = {}

    def mark_down(self, model: str, ttl: float) -> None:
        self._down[model] = (time.monotonic(), ttl)

    def is_up(self, model: str) -> bool:
        entry = self._down.get(model)

        if entry is None:
            return True

        ts, ttl = entry

        if time.monotonic() - ts >= ttl:
            self._down.pop(model, None)
            return True

        return False

    def filter_chain(self, chain: list[str]) -> list[str]:
        return [model for model in chain if self.is_up(model)]

    def cooldowns_snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        result: dict[str, float] = {}

        for model, (timestamp, ttl) in list(self._down.items()):
            remaining = ttl - (now - timestamp)

            if remaining > 0:
                result[model] = round(remaining, 1)
            else:
                self._down.pop(model, None)

        return result


availability = ModelAvailability()


# ============================================================================
# Routing
# ============================================================================

def model_chain(
    model: str,
    *,
    passthrough: bool = False,
) -> list[str]:
    """
    Resolve the model chain according to the selected profile.
    """

    if passthrough:
        return [model]

    lower = model.lower()

    # Haiku → selected Gemini model.
    if model in get_model_rewrite() or "haiku" in lower:
        return [ACTIVE_PROFILE.haiku_model]

    # Claude heavy aliases → selected heavy chain.
    if (
        model in CLAUDE_HEAVY_ALIASES
        or lower.startswith("claude-opus")
        or lower.startswith("claude-sonnet")
        or lower == "claude"
        or lower.startswith("claude-")
    ):
        return list(ACTIVE_PROFILE.heavy_models)

    # Gemini and unknown models are passed through.
    return [model]


# ============================================================================
# Errors / retries
# ============================================================================

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({
    408,
    409,
    429,
    500,
    502,
    503,
    504,
    529,
})

RATE_LIMIT_TEXTS = (
    "overloaded",
    "rate limit",
    "capacity",
    "resource exhausted",
    "quota",
    "too many requests",
    "model is overloaded",
)

AUTH_ERROR_TEXTS = (
    "invalid_grant",
    "unauthorized",
    "unauthenticated",
    "invalid api key",
    "authentication",
)

SKIP_ERROR_TEXTS = (
    "not found",
    "not supported",
    "unknown model",
    "invalid model",
    "does not exist",
)

TRANSIENT_ERROR_TEXTS = (
    "timeout",
    "temporarily unavailable",
    "backend error",
    "internal error",
    "service unavailable",
)


def classify_error(
    status: int,
    body_text: str,
) -> Literal["rate", "auth", "skip", "transient", "fatal"]:
    text = body_text.lower()

    if status in (401, 403) or any(
        value in text for value in AUTH_ERROR_TEXTS
    ):
        return "auth"

    if status == 404 or any(
        value in text for value in SKIP_ERROR_TEXTS
    ):
        return "skip"

    if status == 429 or any(
        value in text for value in RATE_LIMIT_TEXTS
    ):
        return "rate"

    if status in RETRYABLE_STATUS_CODES or any(
        value in text for value in TRANSIENT_ERROR_TEXTS
    ):
        return "transient"

    return "fatal"


def cooldown_for(
    kind: Literal["rate", "auth", "skip", "transient", "fatal"],
) -> float | None:
    return {
        "rate": COOLDOWN_RATE,
        "auth": COOLDOWN_AUTH,
        "skip": COOLDOWN_SKIP,
        "transient": COOLDOWN_TRANSIENT,
        "fatal": None,
    }[kind]


def backoff_delay(
    attempt: int,
    response: httpx.Response | None = None,
) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")

        if retry_after:
            try:
                return min(float(retry_after), RETRY_MAX_DELAY)
            except ValueError:
                pass

    base = min(
        RETRY_BASE_DELAY * (2 ** attempt),
        RETRY_MAX_DELAY,
    )

    return base + random.uniform(0, base * 0.2)


# ============================================================================
# Stats
# ============================================================================

@dataclass
class Stats:
    requests: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record(
        self,
        *,
        in_tok: int,
        out_tok: int,
        elapsed_ms: float,
    ) -> None:
        self.requests += 1
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.total_ms += elapsed_ms


stats = Stats()
last_quota: dict[str, Any] = {}
quota_live: Live | None = None


# ============================================================================
# HTTP client
# ============================================================================

_http_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialised")

    return _http_client


# ============================================================================
# Helpers
# ============================================================================

def resolve_passthrough(request: Request | None = None) -> bool:
    """
    Priority:
      request header
      global BRIDGE_PASSTHROUGH
    """

    if request is not None:
        raw = (
            request.headers
            .get("x-bridge-passthrough", "")
            .strip()
            .lower()
        )

        if raw in ("1", "true", "yes", "on"):
            return True

        if raw in ("0", "false", "no", "off"):
            return False

        policy = (
            request.headers
            .get("x-bridge-policy", "")
            .strip()
            .lower()
        )

        if policy in ("passthrough", "raw", "direct", "none"):
            return True

        if policy in ("rewrite", "default", "policy"):
            return False

    return BRIDGE_PASSTHROUGH


def _short_model(model: str) -> str:
    if model.startswith("claude-"):
        return model.removeprefix("claude-").split("-")[0]

    if model.startswith("gemini-"):
        return model.removeprefix("gemini-")

    return model


def _kv_table(
    rows: list[tuple[str, Any]],
    *,
    title: str | None = None,
) -> Table:
    table = Table(
        show_header=False,
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
        title=title,
        title_style="header",
    )

    table.add_column(
        "key",
        style="kv.key",
        no_wrap=True,
        min_width=12,
    )

    table.add_column(
        "value",
        style="kv.val",
        overflow="fold",
    )

    for key, value in rows:
        if isinstance(value, Text):
            table.add_row(key, value)
        else:
            table.add_row(key, Text(str(value)))

    return table


def sanitise_body(body: dict) -> dict:
    result = _deep_fix(body)

    if result.get("tool_choice") is None:
        result.pop("tool_choice", None)

    return result


def _deep_fix(obj: object) -> object:
    if isinstance(obj, dict):
        return {
            key: (
                False
                if key == "additionalProperties"
                else _deep_fix(value)
            )
            for key, value in obj.items()
        }

    if isinstance(obj, list):
        return [_deep_fix(item) for item in obj]

    return obj


def parse_upstream_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        error = body.get("error")

        if isinstance(error, dict):
            return (
                error.get("message")
                or json.dumps(body, ensure_ascii=False)
            )

        if isinstance(error, str):
            return error

        return json.dumps(body, ensure_ascii=False)

    except Exception:
        return resp.text[:200]


def error_payload(
    resp: httpx.Response | None,
    fallback: str = "Upstream failed",
) -> dict:
    if resp is None:
        return {
            "error": {
                "type": "proxy_error",
                "message": fallback,
            }
        }

    try:
        body = resp.json()

        if isinstance(body, dict):
            return body

    except Exception:
        pass

    return {
        "error": {
            "type": "proxy_error",
            "message": parse_upstream_error(resp),
        }
    }


def usage_from_sse_line(line: str) -> dict | None:
    if not line.startswith("data: "):
        return None

    payload = line[6:].strip()

    if payload in ("", "[DONE]"):
        return None

    try:
        data = json.loads(payload)
        event_type = data.get("type", "")

        if event_type == "message_start":
            return data.get("message", {}).get("usage")

        if event_type == "message_delta":
            return data.get("usage")

    except Exception:
        pass

    return None


# ============================================================================
# Quota inspector
# ============================================================================

def _run_quota_inspector_sync() -> dict[str, Any] | None:
    if not QUOTA_INSPECTOR.is_file():
        log.warning(
            "[warning]⚠[/warning] "
            f"[muted]quota inspector not found: "
            f"{QUOTA_INSPECTOR}[/muted]"
        )
        return None

    if not QUOTA_MANAGEMENT_KEY:
        log.warning(
            "[warning]⚠[/warning] "
            "[muted]quota inspector skipped: "
            "set CPA_MANAGEMENT_KEY in .env[/muted]"
        )
        return None

    command = [
        str(QUOTA_INSPECTOR),
        "--cpa-base-url",
        PROXY_URL,
        "-k",
        QUOTA_MANAGEMENT_KEY,
        "--json",
        "--no-progress",
    ]

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=QUOTA_INSPECTOR_TIMEOUT,
            cwd=str(_REPO_ROOT),
        )

    except subprocess.TimeoutExpired:
        log.warning(
            "[warning]⚠[/warning] "
            "[muted]quota inspector timed out[/muted]"
        )
        return None

    except Exception as exc:
        log.warning(
            "[warning]⚠[/warning] "
            f"[muted]quota inspector failed: {exc}[/muted]"
        )
        return None

    if process.returncode != 0:
        error = (
            process.stderr
            or process.stdout
            or ""
        ).strip()[:300]

        log.warning(
            "[warning]⚠[/warning] "
            f"[muted]quota inspector exit "
            f"{process.returncode}: {error}[/muted]"
        )

        return None

    raw = (process.stdout or "").strip()

    if not raw:
        log.warning(
            "[warning]⚠[/warning] "
            "[muted]quota inspector returned empty output[/muted]"
        )
        return None

    try:
        return json.loads(raw)

    except json.JSONDecodeError as exc:
        log.warning(
            "[warning]⚠[/warning] "
            f"[muted]quota inspector bad JSON: {exc}[/muted]"
        )
        return None


def _bar_style(
    remaining: float | None,
    *,
    exhausted: bool = False,
) -> str:
    if remaining is None:
        return "bar.empty"

    if exhausted or remaining <= 0:
        return "bar.low"

    if remaining < 25:
        return "bar.low"

    if remaining < 50:
        return "bar.mid"

    return "bar.high"


def _bar_text(
    remaining: float | None,
    width: int = 18,
    *,
    exhausted: bool = False,
) -> Text:
    if remaining is None:
        return Text("—", style="muted")

    value = max(0.0, min(100.0, float(remaining)))
    filled = int((value / 100.0 * width) + 0.5)
    filled = min(width, max(0, filled))

    style = _bar_style(
        value,
        exhausted=exhausted,
    )

    result = Text()
    result.append("█" * filled, style=style)
    result.append(
        "░" * (width - filled),
        style="bar.empty",
    )
    result.append(
        f" {value:3.0f}%",
        style=style,
    )

    return result


def _status_text(status: str) -> Text:
    value = (status or "unknown").lower()

    if value in ("exhausted", "error"):
        style = "error"
    elif value in ("low", "warning", "degraded"):
        style = "warning"
    elif value in ("full", "ok", "healthy"):
        style = "success"
    else:
        style = "muted"

    return Text(status or "unknown", style=style)


def _short_account_name(name: str) -> str:
    result = name

    for prefix in (
        "antigravity-",
        "gemini-cli-",
        "codex-",
    ):
        if result.startswith(prefix):
            result = result[len(prefix):]
            break

    if result.endswith(".json"):
        result = result[:-5]

    return result


def _account_info_line(
    report: dict[str, Any],
) -> str | None:
    meta = report.get("meta_fields") or {}

    if not meta:
        return None

    values: list[str] = []

    for key in (
        "channel",
        "project",
        "paid_tier",
        "tier",
    ):
        value = meta.get(key)

        if value:
            values.append(str(value))

    unique: list[str] = []
    seen: set[str] = set()

    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)

    return " · ".join(unique) if unique else None


def _windows_table(
    windows: list[dict[str, Any]],
) -> Table:
    table = Table(
        box=None,
        show_header=True,
        header_style="header",
        pad_edge=False,
        expand=True,
        padding=(0, 1),
        row_styles=["none", "dim"],
    )

    table.add_column(
        "Model group",
        style="info",
        min_width=18,
        max_width=28,
        overflow="ellipsis",
    )

    table.add_column(
        "Remaining",
        min_width=26,
        no_wrap=True,
    )

    table.add_column(
        "Reset",
        style="muted",
        width=12,
        no_wrap=True,
    )

    for window in windows:
        label = str(
            window.get("label")
            or window.get("id")
            or "?"
        )

        remaining = window.get("remaining_percent")
        exhausted = bool(window.get("exhausted"))

        reset = str(
            window.get("reset_label")
            or "—"
        )

        remaining_float = (
            None
            if remaining is None
            else float(remaining)
        )

        table.add_row(
            label,
            _bar_text(
                remaining_float,
                exhausted=exhausted,
            ),
            reset,
        )

    return table


def _account_block(
    report: dict[str, Any],
) -> Group:
    name = str(report.get("name") or "?")
    plan = str(report.get("plan_type") or "unknown")
    status = str(report.get("status") or "unknown")

    header = Text()
    header.append(
        _short_account_name(name),
        style="model",
    )
    header.append("  ")
    header.append(plan, style="badge")
    header.append("  ")
    header.append_text(_status_text(status))

    parts: list[Any] = [header]

    if report.get("error"):
        parts.append(
            Text(
                f"error: {report['error']}",
                style="error",
            )
        )

    windows = list(report.get("windows") or [])
    windows.extend(
        report.get("additional_windows") or []
    )

    if windows:
        parts.append(_windows_table(windows))
    else:
        parts.append(
            Text(
                "no quota windows",
                style="muted",
            )
        )

    info = _account_info_line(report)

    if info:
        parts.append(
            Text(
                info,
                style="muted",
            )
        )

    return Group(*parts)


def _render_quota_report(
    data: dict[str, Any],
) -> None:
    summary = data.get("summary") or {}
    reports = list(data.get("reports") or [])
    provider_summaries = list(
        data.get("provider_summaries") or []
    )

    exhausted = int(
        summary.get("exhausted_accounts") or 0
    )

    low = int(
        summary.get("low_accounts") or 0
    )

    border = (
        "red"
        if exhausted
        else "yellow"
        if low
        else "green"
    )

    target_group = QUOTA_FILTER_MODEL_GROUP.strip()

    # ------------------------------------------------------------------------
    # Compact report
    # ------------------------------------------------------------------------

    if target_group and target_group.lower() != "all":
        target_lower = target_group.lower()

        table = Table(
            box=None,
            show_header=True,
            header_style="header",
            pad_edge=False,
            expand=True,
            padding=(0, 1),
            row_styles=["none", "dim"],
        )

        table.add_column(
            "Account",
            style="model",
            min_width=20,
        )

        table.add_column(
            "Model group",
            style="info",
            min_width=14,
        )

        table.add_column(
            "Quota",
            min_width=26,
            no_wrap=True,
        )

        table.add_column(
            "Reset",
            style="muted",
            width=12,
            no_wrap=True,
        )

        table.add_column(
            "Status",
            width=10,
            no_wrap=True,
        )

        show_lower = [g.lower() for g in QUOTA_SHOW_GROUPS]

        for report in reports:
            name = _short_account_name(
                str(report.get("name") or "?")
            )

            status = str(
                report.get("status") or "unknown"
            )

            windows = list(
                report.get("windows") or []
            )

            windows.extend(
                report.get("additional_windows") or []
            )

            visible = [
                w for w in windows
                if any(
                    g in str(
                        w.get("label")
                        or w.get("id")
                        or ""
                    ).lower()
                    or str(
                        w.get("label")
                        or w.get("id")
                        or ""
                    ).lower() in g
                    for g in show_lower
                )
            ]

            if not visible:
                if report.get("error"):
                    table.add_row(
                        name,
                        "—",
                        Text(
                            f"error: {report['error'][:30]}",
                            style="error",
                        ),
                        "—",
                        _status_text(status),
                    )
                else:
                    table.add_row(
                        name,
                        "—",
                        Text("—", style="muted"),
                        "—",
                        _status_text(status),
                    )
                continue

            for wi, window in enumerate(visible):
                label = str(
                    window.get("label")
                    or window.get("id")
                    or "?"
                )

                remaining = window.get(
                    "remaining_percent"
                )

                exhausted_window = bool(
                    window.get("exhausted")
                )

                reset = str(
                    window.get("reset_label")
                    or "—"
                )

                remaining_float = (
                    None
                    if remaining is None
                    else float(remaining)
                )

                bar = _bar_text(
                    remaining_float,
                    exhausted=exhausted_window,
                )

                table.add_row(
                    name if wi == 0 else "",
                    label,
                    bar,
                    reset,
                    _status_text(status)
                    if wi == 0
                    else Text(""),
                )

        equivalent = {
            **(summary.get("antigravity_equivalents") or {}),
            **(summary.get("gemini_equivalents") or {})
        }

        footer_rows: list[tuple[str, Any]] = [
            (
                "Accounts",
                Text(
                    f"{len(reports)}  "
                    f"exhausted={exhausted}  "
                    f"low={low}",
                    style="bold",
                ),
            ),
        ]

        for key, value in sorted(
            equivalent.items()
        ):
            kl = key.lower()
            if "claude" in kl or "gpt" in kl or "gemini" in kl:
                footer_rows.append(
                    (
                        str(key),
                        Text(
                            f"{value}%",
                            style="info",
                        ),
                    )
                )

        render_group = Group(
            Rule(
                "[header]CPA Quota[/header]",
                style=border,
            ),
            Panel(
                table,
                title=(
                    f"[header]Quota[/header]  "
                    f"[muted]{len(reports)} accounts[/muted]"
                ),
                border_style=border,
                padding=(0, 1),
            ),
            Panel(
                _kv_table(footer_rows),
                title="[summary]Summary[/summary]",
                border_style=border,
                padding=(0, 1),
            ),
        )

        if quota_live is not None and quota_live.is_started:
            quota_live.update(
                render_group,
                refresh=True,
            )
        else:
            console.print()
            console.print(render_group)
            console.print()

        return

    # ------------------------------------------------------------------------
    # Full report
    # ------------------------------------------------------------------------

    render_parts: list[Any] = [
        Rule(
            "[header]CPA Quota[/header]",
            style=border,
        )
    ]

    by_provider: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for report in reports:
        by_provider.setdefault(
            report.get("provider") or "unknown",
            [],
        ).append(report)

    if provider_summaries:
        for provider_summary in provider_summaries:
            title = str(
                provider_summary.get("title")
                or provider_summary.get("provider")
                or "Provider"
            )

            plans = (
                provider_summary.get("plans")
                or {}
            )

            statuses = (
                provider_summary.get("statuses")
                or {}
            )

            plan_text = (
                ", ".join(
                    f"{key}:{value}"
                    for key, value in sorted(
                        plans.items()
                    )
                )
                or "—"
            )

            status_text = (
                ", ".join(
                    f"{key}:{value}"
                    for key, value in sorted(
                        statuses.items()
                    )
                )
                or "—"
            )

            rows: list[tuple[str, Any]] = [
                (
                    "Accounts",
                    Text(
                        str(
                            provider_summary.get(
                                "accounts",
                                0,
                            )
                        ),
                        style="bold",
                    ),
                ),
                ("Plans", plan_text),
                ("Statuses", status_text),
            ]

            for row in (
                provider_summary.get("extras")
                or []
            ):
                rows.append(
                    (
                        str(
                            row.get("label", "?")
                        ),
                        Text(
                            str(
                                row.get(
                                    "value",
                                    "",
                                )
                            ),
                            style="info",
                        ),
                    )
                )

            render_parts.append(
                Panel(
                    _kv_table(rows),
                    title=(
                        f"[header]{title} "
                        "Summary[/header]"
                    ),
                    border_style=border,
                    padding=(0, 1),
                )
            )

    provider_titles = {
        "antigravity": "Antigravity",
        "gemini-cli": "Gemini CLI",
        "codex": "Codex",
    }

    for provider, items in by_provider.items():
        title = provider_titles.get(
            provider,
            provider,
        )

        account_parts: list[Any] = []

        for index, report in enumerate(items):
            if index > 0:
                account_parts.append(
                    Rule(style="dim")
                )

            account_parts.append(
                _account_block(report)
            )

        body: Any = (
            Group(*account_parts)
            if account_parts
            else Text(
                "no accounts",
                style="muted",
            )
        )

        render_parts.append(
            Panel(
                body,
                title=(
                    f"[header]{title}[/header]  "
                    f"[muted]{len(items)} accounts[/muted]"
                ),
                border_style=border,
                padding=(0, 1),
            )
        )

    equivalent = (
        summary.get("antigravity_equivalents")
        or summary.get("gemini_equivalents")
        or {}
    )

    footer_rows: list[tuple[str, Any]] = [
        (
            "Accounts",
            Text(
                str(summary.get("accounts", 0)),
                style="bold",
            ),
        ),
        (
            "Exhausted",
            Text(
                str(exhausted),
                style=(
                    "error"
                    if exhausted
                    else "muted"
                ),
            ),
        ),
        (
            "Low",
            Text(
                str(low),
                style=(
                    "warning"
                    if low
                    else "muted"
                ),
            ),
        ),
        (
            "Errors",
            Text(
                str(
                    summary.get(
                        "error_accounts",
                        0,
                    )
                ),
                style="muted",
            ),
        ),
    ]

    for key, value in sorted(
        equivalent.items()
    ):
        footer_rows.append(
            (
                str(key),
                Text(
                    f"{value}%",
                    style="info",
                ),
            )
        )

    render_parts.append(
        Panel(
            _kv_table(footer_rows),
            title="[summary]Summary[/summary]",
            border_style=border,
            padding=(0, 1),
        )
    )

    full_renderable = Group(*render_parts)

    if quota_live is not None and quota_live.is_started:
        quota_live.update(
            full_renderable,
            refresh=True,
        )
    else:
        console.print()
        console.print(full_renderable)
        console.print()


async def refresh_quota(
    *,
    silent: bool = False,
) -> None:
    global last_quota

    data = await asyncio.to_thread(
        _run_quota_inspector_sync
    )

    if data is None:
        return

    summary = data.get("summary") or {}
    reports = data.get("reports") or []

    last_quota = {
        "ts": time.time(),
        "accounts": summary.get("accounts"),
        "exhausted_accounts": summary.get(
            "exhausted_accounts"
        ),
        "low_accounts": summary.get(
            "low_accounts"
        ),
        "error_accounts": summary.get(
            "error_accounts"
        ),
        "status_counts": summary.get(
            "status_counts"
        ),
        "provider_counts": summary.get(
            "provider_counts"
        ),
        "exhausted_names": summary.get(
            "exhausted_names"
        ),
        "low_names": summary.get(
            "low_names"
        ),
        "plan_counts": summary.get(
            "plan_counts"
        ),
        "antigravity_equivalents": summary.get(
            "antigravity_equivalents"
        ),
        "gemini_equivalents": summary.get(
            "gemini_equivalents"
        ),
        "accounts_detail": [
            {
                "name": report.get("name"),
                "provider": report.get(
                    "provider"
                ),
                "plan_type": report.get(
                    "plan_type"
                ),
                "status": report.get(
                    "status"
                ),
                "windows": [
                    {
                        "label": (
                            window.get("label")
                            or window.get("id")
                        ),
                        "remaining_percent": window.get(
                            "remaining_percent"
                        ),
                        "reset_label": window.get(
                            "reset_label"
                        ),
                        "exhausted": window.get(
                            "exhausted"
                        ),
                    }
                    for window in (
                        report.get("windows")
                        or []
                    )
                ],
            }
            for report in reports
        ],
    }

    if not silent:
        _render_quota_report(data)

    elif summary.get("exhausted_accounts"):
        names = ", ".join(
            (
                summary.get(
                    "exhausted_names"
                )
                or []
            )[:5]
        )

        log.warning(
            "[warning]⚠[/warning] "
            f"[muted]quota: "
            f"exhausted="
            f"{summary['exhausted_accounts']} "
            f"{names}[/muted]"
        )


async def _quota_loop(
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=QUOTA_POLL_INTERVAL,
            )
            break

        except asyncio.TimeoutError:
            pass

        await refresh_quota(silent=False)


# ============================================================================
# Upstream result
# ============================================================================

@dataclass
class UpstreamResult:
    resp: httpx.Response | None
    used_model: str | None
    skipped: list[str] = field(default_factory=list)
    total_attempts: int = 0
    fallback_count: int = 0


# ============================================================================
# Logging
# ============================================================================

def _log_final(
    req_id: str,
    original_model: str,
    result: UpstreamResult,
    in_tok: int,
    out_tok: int,
    elapsed_ms: float,
    *,
    is_stream: bool = False,
    has_system: bool = False,
    tools: list | None = None,
    passthrough: bool = False,
) -> None:
    elapsed_s = elapsed_ms / 1000
    ok = result.used_model is not None

    status = (
        "[success]✓[/success]"
        if ok
        else "[error]✗[/error]"
    )

    effective = (
        result.used_model
        or original_model
    )

    chain = model_chain(
        original_model,
        passthrough=passthrough,
    )

    chain_start = chain[0]

    if passthrough:
        model_part = (
            f"[model]{effective}[/model] "
            "[muted]passthrough[/muted]"
        )

    elif original_model != chain_start:
        model_part = (
            f"[replace]{original_model}[/replace]"
            f" → [model]{effective}[/model]"
        )

    elif result.fallback_count > 0:
        model_part = (
            f"[muted]{_short_model(chain_start)}"
            f"[/muted] → "
            f"[model]{effective}[/model]"
        )

    else:
        model_part = (
            f"[model]{effective}[/model]"
        )

    flags: list[str] = []

    if result.total_attempts > 1:
        flags.append(
            f"[warning]×{result.total_attempts}[/warning]"
        )

    if result.skipped:
        flags.append(
            "[skip]−"
            + ",".join(
                _short_model(model)
                for model in result.skipped
            )
            + "[/skip]"
        )

    if is_stream:
        flags.append("[muted]stream[/muted]")

    if has_system:
        flags.append("[system]sys[/system]")

    if tools:
        flags.append(
            f"[tool]⚒{len(tools)}[/tool]"
        )

    if not ok:
        flags.append("[error]failed[/error]")

    flags_str = (
        " " + " ".join(flags)
        if flags
        else ""
    )

    tokens = (
        f"[tokens]↑{in_tok:,} ↓{out_tok:,}[/tokens]"
        if ok and (in_tok or out_tok)
        else "[muted]—[/muted]"
    )

    log.info(
        f"{status} "
        f"[req_id]{req_id}[/req_id] │ "
        f"{model_part}{flags_str} │ "
        f"{tokens} │ "
        f"[muted]{elapsed_s:.1f}s[/muted]"
    )


# ============================================================================
# Call upstream
# ============================================================================

async def call_upstream(
    req_id: str,
    body: dict,
    original_model: str,
    chain: list[str],
) -> UpstreamResult:
    client = get_client()

    skipped: list[str] = []
    total_attempts = 0
    fallback_count = 0

    effective_chain = availability.filter_chain(
        chain
    )

    if not effective_chain:
        log.warning(
            f"   [warning]⚠[/warning] "
            f"[req_id]{req_id}[/req_id] "
            "[muted]все модели в cooldown, "
            "fail-open[/muted]"
        )

        effective_chain = list(chain)

    else:
        skipped = [
            model
            for model in chain
            if not availability.is_up(model)
        ]

    for chain_index, candidate in enumerate(
        effective_chain
    ):
        body["model"] = candidate

        next_candidate = (
            effective_chain[chain_index + 1]
            if chain_index + 1 < len(effective_chain)
            else None
        )

        last_response: httpx.Response | None = None

        for attempt in range(
            SAME_MODEL_RETRIES + 1
        ):
            total_attempts += 1

            if attempt > 0:
                delay = backoff_delay(
                    attempt - 1,
                    last_response,
                )

                log.warning(
                    f"   [warning]↺[/warning] "
                    f"[req_id]{req_id}[/req_id] "
                    f"[muted]{candidate} · "
                    f"backoff {delay:.1f}s · "
                    f"{attempt + 1}/"
                    f"{SAME_MODEL_RETRIES + 1}[/muted]"
                )

                await asyncio.sleep(delay)

            try:
                response = await client.post(
                    f"{PROXY_URL}/v1/messages?beta=true",
                    json=body,
                    headers=UPSTREAM_HEADERS,
                )

            except httpx.ConnectError:
                stats.errors += 1

                availability.mark_down(
                    candidate,
                    COOLDOWN_TRANSIENT,
                )

                log.error(
                    f"   [error]✗[/error] "
                    f"[req_id]{req_id}[/req_id] "
                    f"[muted]{candidate} · "
                    f"connect error · "
                    f"cooldown "
                    f"{COOLDOWN_TRANSIENT:.0f}s"
                    f"{' → ' + next_candidate if next_candidate else ''}"
                    f"[/muted]"
                )

                if next_candidate:
                    fallback_count += 1
                    break

                return UpstreamResult(
                    None,
                    None,
                    skipped,
                    total_attempts,
                    fallback_count,
                )

            except httpx.TimeoutException:
                stats.errors += 1

                is_last = (
                    attempt == SAME_MODEL_RETRIES
                )

                log.warning(
                    f"   [warning]⏱[/warning] "
                    f"[req_id]{req_id}[/req_id] "
                    f"[muted]{candidate} · timeout · "
                    f"{attempt + 1}/"
                    f"{SAME_MODEL_RETRIES + 1}[/muted]"
                )

                if not is_last:
                    continue

                availability.mark_down(
                    candidate,
                    COOLDOWN_TRANSIENT,
                )

                if next_candidate:
                    fallback_count += 1
                    break

                return UpstreamResult(
                    None,
                    None,
                    skipped,
                    total_attempts,
                    fallback_count,
                )

            if response.status_code < 400:
                return UpstreamResult(
                    response,
                    candidate,
                    skipped,
                    total_attempts,
                    fallback_count,
                )

            last_response = response

            error_message = parse_upstream_error(
                response
            )

            error_kind = classify_error(
                response.status_code,
                error_message,
            )

            retryable = error_kind in (
                "rate",
                "transient",
                "auth",
            )

            is_last = (
                attempt == SAME_MODEL_RETRIES
            )

            if retryable and not is_last:
                continue

            ttl = cooldown_for(error_kind)

            if ttl is not None:
                availability.mark_down(
                    candidate,
                    ttl,
                )

            if next_candidate and (
                retryable
                or error_kind == "skip"
            ):
                fallback_count += 1

            cooldown_part = (
                f" · cooldown {ttl:.0f}s"
                if ttl is not None
                else ""
            )

            next_part = (
                f" → {next_candidate}"
                if next_candidate
                else ""
            )

            log.warning(
                f"   [warning]↗[/warning] "
                f"[req_id]{req_id}[/req_id] "
                f"[muted]{candidate} · "
                f"HTTP {response.status_code} "
                f"{error_message[:100]}"
                f"{cooldown_part}"
                f"{next_part}[/muted]"
            )

            if not next_candidate:
                return UpstreamResult(
                    response,
                    None,
                    skipped,
                    total_attempts,
                    fallback_count,
                )

            break

    return UpstreamResult(
        None,
        None,
        skipped,
        total_attempts,
        fallback_count,
    )


# ============================================================================
# Lifespan
# ============================================================================

@asynccontextmanager
async def lifespan(
    _: FastAPI,
) -> AsyncIterator[None]:
    global _http_client, quota_live

    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            HTTP_TIMEOUT,
            connect=HTTP_CONNECT_TIMEOUT,
        ),
        limits=httpx.Limits(
            max_connections=HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=HTTP_MAX_KEEPALIVE,
        ),
    )

    console.print()

    policy_text = Text()

    if BRIDGE_PASSTHROUGH:
        policy_text.append(
            "PASSTHROUGH — no rewrite / "
            "no model fallback",
            style="replace",
        )

    else:
        policy_text.append(
            "Heavy: ",
            style="muted",
        )
        policy_text.append(
            ACTIVE_PROFILE.heavy_label,
            style="model",
        )
        policy_text.append(
            " · Haiku: ",
            style="muted",
        )
        policy_text.append(
            ACTIVE_PROFILE.haiku_model,
            style="replace",
        )

    console.print(
        Panel(
            _kv_table([
                (
                    "Profile",
                    Text(
                        ACTIVE_PROFILE.title,
                        style="model",
                    ),
                ),
                (
                    "Target",
                    Text(
                        PROXY_URL,
                        style="bold cyan",
                    ),
                ),
                (
                    "Listen",
                    Text(
                        f"{BRIDGE_HOST}:{BRIDGE_PORT}",
                        style="bold cyan",
                    ),
                ),
                (
                    "Policy",
                    policy_text,
                ),
                (
                    "Passthrough",
                    Text(
                        (
                            "on (BRIDGE_PASSTHROUGH)"
                            if BRIDGE_PASSTHROUGH
                            else "off · "
                            "header "
                            "X-Bridge-Passthrough: 1"
                        ),
                        style=(
                            "bold"
                            if BRIDGE_PASSTHROUGH
                            else "muted"
                        ),
                    ),
                ),
                (
                    "Retries",
                    Text(
                        f"{SAME_MODEL_RETRIES + 1} / model",
                        style="bold",
                    ),
                ),
                (
                    "Quota",
                    Text(
                        f"every "
                        f"{QUOTA_POLL_INTERVAL:.0f}s",
                        style="bold",
                    ),
                ),
            ]),
            title="[header]🚀  LLM Proxy[/header]",
            border_style="bright_blue",
            padding=(0, 1),
        )
    )

    if QUOTA_LIVE_DISPLAY:
        quota_live = Live(
            Text(
                "Initialising quota monitor...",
                style="muted",
            ),
            console=console,
            auto_refresh=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )

        quota_live.start()

    await refresh_quota(silent=False)

    stop = asyncio.Event()

    quota_task = asyncio.create_task(
        _quota_loop(stop),
        name="quota-loop",
    )

    try:
        yield

    finally:
        stop.set()
        quota_task.cancel()

        try:
            await quota_task
        except asyncio.CancelledError:
            pass

        if quota_live is not None:
            quota_live.stop()
            quota_live = None

        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None

    requests = max(stats.requests, 1)

    console.print()

    console.print(
        Panel(
            _kv_table([
                (
                    "Profile",
                    Text(
                        ACTIVE_PROFILE.title,
                        style="model",
                    ),
                ),
                (
                    "Requests",
                    Text.assemble(
                        (
                            str(stats.requests),
                            "bold",
                        ),
                        (
                            "  errors=",
                            "muted",
                        ),
                        (
                            str(stats.errors),
                            (
                                "error"
                                if stats.errors
                                else "muted"
                            ),
                        ),
                    ),
                ),
                (
                    "Tokens",
                    Text(
                        f"{stats.total_tokens:,}",
                        style="tokens",
                    ),
                ),
                (
                    "Avg",
                    Text(
                        f"{stats.total_ms / requests:,.0f} ms",
                        style="muted",
                    ),
                ),
            ]),
            title="[summary]📊  Session Summary[/summary]",
            border_style="green",
            padding=(0, 1),
        )
    )

    console.print()


# ============================================================================
# FastAPI
# ============================================================================

app = FastAPI(lifespan=lifespan)


# ============================================================================
# Routes
# ============================================================================

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "requests": stats.requests,
        "errors": stats.errors,
        "total_tokens": stats.total_tokens,
        "unavailable": availability.cooldowns_snapshot(),
        "quota": last_quota,
        "policy": {
            "profile": ACTIVE_PROFILE.key,
            "profile_title": ACTIVE_PROFILE.title,
            "passthrough": BRIDGE_PASSTHROUGH,
            "heavy_models": list(
                ACTIVE_PROFILE.heavy_models
            ),
            "haiku_rewrite": get_model_rewrite(),
            "same_model_retries": SAME_MODEL_RETRIES,
        },
    }


@app.post(
    "/v1/messages",
    response_model=None,
)
async def messages(
    request: Request,
) -> StreamingResponse | JSONResponse:
    req_id = uuid.uuid4().hex[:8].upper()
    start = time.perf_counter()

    try:
        body = sanitise_body(
            await request.json()
        )

    except Exception:
        log.exception(
            f"[req_id]{req_id}[/req_id] "
            "[error]invalid JSON[/error]"
        )

        return JSONResponse(
            {"error": "Invalid JSON"},
            status_code=400,
        )

    original_model = body.get(
        "model",
        "unknown",
    )

    passthrough = resolve_passthrough(
        request
    )

    chain = model_chain(
        original_model,
        passthrough=passthrough,
    )

    tools = body.get("tools")
    is_stream = body.get("stream", False)
    has_system = bool(body.get("system"))

    result = await call_upstream(
        req_id,
        body,
        original_model,
        chain,
    )

    elapsed = (
        time.perf_counter() - start
    ) * 1000

    # ------------------------------------------------------------------------
    # Failure
    # ------------------------------------------------------------------------

    if result.used_model is None:
        stats.errors += 1

        _log_final(
            req_id,
            original_model,
            result,
            0,
            0,
            elapsed,
            is_stream=is_stream,
            has_system=has_system,
            tools=tools,
            passthrough=passthrough,
        )

        status = (
            result.resp.status_code
            if result.resp is not None
            else 502
        )

        if result.resp is None:
            status = 502
        elif status < 400:
            status = 502

        return JSONResponse(
            error_payload(result.resp),
            status_code=status,
        )

    # ------------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------------

    if is_stream:

        async def stream_gen():
            input_tokens = 0
            output_tokens = 0
            last_thinking_log = 0.0

            try:
                async for line in (
                    result.resp.aiter_lines()
                ):
                    yield (
                        line + "\n"
                    ).encode()

                    if (
                        line.startswith("data: ")
                        and line != "data: [DONE]"
                    ):
                        try:
                            data = json.loads(
                                line[6:]
                            )

                            event_type = data.get(
                                "type"
                            )

                            if event_type == (
                                "content_block_start"
                            ):
                                content_block = (
                                    data.get(
                                        "content_block"
                                    )
                                    or {}
                                )

                                if (
                                    content_block.get(
                                        "type"
                                    )
                                    == "thinking"
                                ):
                                    log.info(
                                        f"   [info]🧠[/info] "
                                        f"[req_id]{req_id}[/req_id] "
                                        "[muted]model is "
                                        "thinking...[/muted]"
                                    )

                                    last_thinking_log = (
                                        time.perf_counter()
                                    )

                            elif event_type == (
                                "content_block_delta"
                            ):
                                delta = (
                                    data.get(
                                        "delta"
                                    )
                                    or {}
                                )

                                if (
                                    delta.get("type")
                                    == "thinking_delta"
                                ):
                                    now = (
                                        time.perf_counter()
                                    )

                                    if (
                                        last_thinking_log > 0
                                        and now
                                        - last_thinking_log
                                        > 10.0
                                    ):
                                        log.info(
                                            f"   [info]🧠[/info] "
                                            f"[req_id]{req_id}[/req_id] "
                                            "[muted]...still "
                                            f"thinking "
                                            f"({int(now - start)}s)"
                                            "[/muted]"
                                        )

                                        last_thinking_log = now

                        except Exception:
                            pass

                    usage = usage_from_sse_line(
                        line
                    )

                    if usage:
                        input_tokens = usage.get(
                            "input_tokens",
                            input_tokens,
                        )

                        output_tokens = usage.get(
                            "output_tokens",
                            output_tokens,
                        )

            except Exception as exc:
                log.error(
                    f"[req_id]{req_id}[/req_id] "
                    f"[error]✗ stream error[/error] "
                    f"{exc}"
                )

            finally:
                final_elapsed = (
                    time.perf_counter()
                    - start
                ) * 1000

                stats.record(
                    in_tok=input_tokens,
                    out_tok=output_tokens,
                    elapsed_ms=final_elapsed,
                )

                _log_final(
                    req_id,
                    original_model,
                    result,
                    input_tokens,
                    output_tokens,
                    final_elapsed,
                    is_stream=True,
                    has_system=has_system,
                    tools=tools,
                    passthrough=passthrough,
                )

        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------------
    # Non-stream
    # ------------------------------------------------------------------------

    try:
        response_body = result.resp.json()

        usage = response_body.get(
            "usage",
            {},
        )

        input_tokens = usage.get(
            "input_tokens",
            0,
        )

        output_tokens = usage.get(
            "output_tokens",
            0,
        )

    except Exception:
        response_body = {
            "error": "Invalid upstream JSON"
        }

        input_tokens = 0
        output_tokens = 0

    stats.record(
        in_tok=input_tokens,
        out_tok=output_tokens,
        elapsed_ms=elapsed,
    )

    _log_final(
        req_id,
        original_model,
        result,
        input_tokens,
        output_tokens,
        elapsed,
        is_stream=False,
        has_system=has_system,
        tools=tools,
        passthrough=passthrough,
    )

    return JSONResponse(
        response_body,
        status_code=result.resp.status_code,
    )


# ============================================================================
# Entry point
# ============================================================================

if __name__ == "__main__":
    # ВАЖНО:
    # Выбор происходит здесь, до запуска Uvicorn.
    # Поэтому lifespan уже получает готовый ACTIVE_PROFILE.
    ACTIVE_PROFILE = select_profile()

    uvicorn.run(
        app,
        host=BRIDGE_HOST,
        port=BRIDGE_PORT,
        log_level="warning",
    )