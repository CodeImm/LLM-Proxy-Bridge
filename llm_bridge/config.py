"""Configuration: .env parsing, validation and routing profiles.

Every value is read once at startup into an immutable ``Settings`` object.
Invalid values are collected and reported together via ``ConfigError``
instead of crashing with a traceback on the first bad line.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """One or more configuration values are invalid."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


# ============================================================================
# Env reader
# ============================================================================


class _EnvReader:
    """Typed accessors over an environment mapping that collect errors."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ
        self.problems: list[str] = []

    def _raw(self, key: str) -> str | None:
        value = self._environ.get(key)
        if value is None or not value.strip():
            return None
        return value.strip()

    def str(self, key: str, default: str = "") -> str:
        raw = self._raw(key)
        return default if raw is None else raw

    def first(self, *keys: str, default: str = "") -> str:
        for key in keys:
            raw = self._raw(key)
            if raw is not None:
                return raw
        return default

    def int(self, key: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
        raw = self._raw(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.problems.append(f"{key}={raw!r}: ожидается целое число")
            return default
        return self._check_range(key, value, default, lo, hi)

    def float(self, key: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
        raw = self._raw(key)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.problems.append(f"{key}={raw!r}: ожидается число")
            return default
        return self._check_range(key, value, default, lo, hi)

    def bool(self, key: str, default: bool) -> bool:
        raw = self._raw(key)
        if raw is None:
            return default
        lowered = raw.lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
        self.problems.append(f"{key}={raw!r}: ожидается 1/0, true/false, yes/no, on/off")
        return default

    def csv(self, key: str, default: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        raw = self._raw(key)
        if raw is None:
            return tuple(default)
        items = tuple(part.strip() for part in raw.split(",") if part.strip())
        if not items:
            self.problems.append(f"{key}: пустой список")
            return tuple(default)
        return items

    def pairs(self, key: str) -> tuple[tuple[str, str], ...]:
        """``a=X,b=Y`` → ``(("a", "X"), ("b", "Y"))``."""
        raw = self._raw(key)
        if raw is None:
            return ()
        result: list[tuple[str, str]] = []
        for part in raw.split(","):
            if not part.strip():
                continue
            name, sep, value = part.partition("=")
            if not sep or not name.strip() or not value.strip():
                self.problems.append(f"{key}: {part.strip()!r} — ожидается модель=Группа")
                continue
            result.append((name.strip(), value.strip()))
        return tuple(result)

    def _check_range(self, key, value, default, lo, hi):
        if lo is not None and value < lo:
            self.problems.append(f"{key}={value}: минимум {lo}")
            return default
        if hi is not None and value > hi:
            self.problems.append(f"{key}={value}: максимум {hi}")
            return default
        return value


# ============================================================================
# Settings
# ============================================================================

_DEFAULT_HEAVY_MODELS = ("claude-opus-4-6-thinking", "claude-sonnet-4-6")
_DEFAULT_CURRENT_HAIKU_MODEL = "claude-opus-4-6-thinking"

_DEFAULT_HAIKU_SOURCES = (
    "claude-haiku-4-5-20251001",
    "claude-haiku-4-5",
    "claude-haiku",
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307",
)

_DEFAULT_HEAVY_ALIASES = (
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
)


@dataclass(frozen=True, slots=True)
class Settings:
    # Upstream (CLIProxyAPI)
    cpa_base_url: str
    upstream_api_key: str
    anthropic_version: str

    # Listen
    host: str
    port: int

    # Policy
    passthrough_default: bool
    default_profile: str
    heavy_models: tuple[str, ...]
    current_haiku_model: str
    gemini_pro_agent_model: str
    gemini_flash_high_model: str
    haiku_source_models: tuple[str, ...]
    heavy_aliases: tuple[str, ...]

    # Retries / cooldowns
    same_model_retries: int
    retry_base_delay: float
    retry_max_delay: float
    cooldown_rate: float
    cooldown_auth: float
    cooldown_transient: float
    cooldown_skip: float

    # HTTP client
    http_timeout: float
    http_connect_timeout: float
    http_max_connections: int
    http_max_keepalive: int

    # Quota inspector
    quota_inspector_path: Path
    quota_poll_interval: float
    quota_inspector_timeout: float
    quota_filter_group: str
    quota_show_groups: tuple[str, ...]  # empty = derived from the routing profile
    quota_model_groups: tuple[tuple[str, str], ...]  # extra model → quota group mappings
    management_key: str

    # Console / lifecycle
    live_display: bool
    shutdown_timeout: float

    @property
    def attempts_per_model(self) -> int:
        return self.same_model_retries + 1

    @property
    def quota_enabled(self) -> bool:
        return self.quota_poll_interval > 0

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "Settings":
        env = _EnvReader(environ)

        cpa_base_url = env.str("CPA_BASE_URL", "http://127.0.0.1:8317").rstrip("/")
        if not cpa_base_url.startswith(("http://", "https://")):
            env.problems.append(f"CPA_BASE_URL={cpa_base_url!r}: должен начинаться с http:// или https://")

        inspector_raw = env.str("QUOTA_INSPECTOR_PATH")
        inspector_path = (
            Path(inspector_raw).expanduser()
            if inspector_raw
            else REPO_ROOT / "CLIProxyAPI-Quota-Inspector" / "cpa-quota-inspector.exe"
        )

        retry_base = env.float("RETRY_BASE_DELAY", 1.0, lo=0)
        retry_max = env.float("RETRY_MAX_DELAY", 10.0, lo=0)
        if retry_max < retry_base:
            env.problems.append(f"RETRY_MAX_DELAY={retry_max} меньше RETRY_BASE_DELAY={retry_base}")

        settings = cls(
            cpa_base_url=cpa_base_url,
            upstream_api_key=env.str("UPSTREAM_API_KEY", "sk-antigravity"),
            anthropic_version=env.str("ANTHROPIC_VERSION", "2023-06-01"),
            host=env.str("BRIDGE_HOST", "127.0.0.1"),
            port=env.int("BRIDGE_PORT", 4000, lo=1, hi=65535),
            passthrough_default=env.bool("BRIDGE_PASSTHROUGH", False),
            default_profile=env.str("BRIDGE_PROFILE", ""),
            heavy_models=env.csv("CLAUDE_HEAVY_MODELS", _DEFAULT_HEAVY_MODELS),
            current_haiku_model=_DEFAULT_CURRENT_HAIKU_MODEL,
            gemini_pro_agent_model=env.str("GEMINI_PRO_AGENT_MODEL", "gemini-pro-agent"),
            gemini_flash_high_model=env.str("GEMINI_FLASH_HIGH_MODEL", "gemini-3.7-flash-high"),
            haiku_source_models=env.csv("HAIKU_SOURCE_MODELS", _DEFAULT_HAIKU_SOURCES),
            heavy_aliases=env.csv("CLAUDE_HEAVY_ALIASES", _DEFAULT_HEAVY_ALIASES),
            same_model_retries=env.int("SAME_MODEL_RETRIES", 4, lo=0, hi=20),
            retry_base_delay=retry_base,
            retry_max_delay=retry_max,
            cooldown_rate=env.float("COOLDOWN_RATE", 60.0, lo=0),
            cooldown_auth=env.float("COOLDOWN_AUTH", 1800.0, lo=0),
            cooldown_transient=env.float("COOLDOWN_TRANSIENT", 30.0, lo=0),
            cooldown_skip=env.float("COOLDOWN_SKIP", 300.0, lo=0),
            http_timeout=env.float("HTTP_TIMEOUT", 180.0, lo=1),
            http_connect_timeout=env.float("HTTP_CONNECT_TIMEOUT", 15.0, lo=0.1),
            http_max_connections=env.int("HTTP_MAX_CONNECTIONS", 100, lo=1),
            http_max_keepalive=env.int("HTTP_MAX_KEEPALIVE", 20, lo=0),
            quota_inspector_path=inspector_path,
            quota_poll_interval=env.float("QUOTA_POLL_INTERVAL", 300.0, lo=0),
            quota_inspector_timeout=env.float("QUOTA_INSPECTOR_TIMEOUT", 120.0, lo=1),
            quota_filter_group=env.str("QUOTA_FILTER_MODEL_GROUP", "Claude/GPT"),
            quota_show_groups=env.csv("QUOTA_SHOW_GROUPS", ()),
            quota_model_groups=env.pairs("QUOTA_MODEL_GROUPS"),
            management_key=env.first("CPA_MANAGEMENT_KEY", "MANAGEMENT_PASSWORD"),
            live_display=env.bool("QUOTA_LIVE_DISPLAY", True),
            shutdown_timeout=env.float("BRIDGE_SHUTDOWN_TIMEOUT", 5.0, lo=0),
        )

        if env.problems:
            raise ConfigError(env.problems)
        return settings

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        """Load ``.env`` (without overriding real env vars) and build settings."""
        path = env_file or DEFAULT_ENV_FILE
        if env_file is not None and not path.is_file():
            raise ConfigError([f"файл окружения не найден: {path}"])
        load_dotenv(path, override=False)
        return cls.from_environ(os.environ)

    def with_overrides(self, **changes: object) -> "Settings":
        return replace(self, **changes)


# ============================================================================
# Routing profiles
# ============================================================================


@dataclass(frozen=True, slots=True)
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
    def models(self) -> tuple[str, ...]:
        """Every upstream model the profile can route to, in chain order."""
        return tuple(dict.fromkeys((*self.heavy_models, self.haiku_model)))


def build_profiles(settings: Settings) -> tuple[RoutingProfile, ...]:
    pro = settings.gemini_pro_agent_model
    flash = settings.gemini_flash_high_model
    return (
        RoutingProfile(
            key="current",
            title="Текущая конфигурация",
            description="Heavy Claude → цепочка CLAUDE_HEAVY_MODELS; Haiku → Opus 4.6 Thinking",
            heavy_models=settings.heavy_models,
            haiku_model=settings.current_haiku_model,
        ),
        RoutingProfile(
            key="gemini-pro-agent",
            title="Gemini Pro Agent",
            description="Все Claude Heavy + Haiku → Gemini Pro Agent",
            heavy_models=(pro,),
            haiku_model=pro,
        ),
        RoutingProfile(
            key="gemini-3.7-flash-high",
            title="Gemini 3.7 Flash High",
            description="Все Claude Heavy + Haiku → Gemini 3.7 Flash High",
            heavy_models=(flash,),
            haiku_model=flash,
        ),
    )


def find_profile(profiles: tuple[RoutingProfile, ...], choice: str) -> RoutingProfile | None:
    """Resolve a profile by 1-based index or key (case-insensitive)."""
    value = choice.strip().lower()
    if not value:
        return None
    if value.isdigit():
        index = int(value)
        if 1 <= index <= len(profiles):
            return profiles[index - 1]
        return None
    for profile in profiles:
        if profile.key.lower() == value:
            return profile
    return None
