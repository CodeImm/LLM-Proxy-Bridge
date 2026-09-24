"""Session statistics and token usage accounting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def update(self, usage: Any) -> None:
        """Merge an Anthropic ``usage`` object (message_start / message_delta)."""
        if not isinstance(usage, dict):
            return
        for key, attr in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
            ("cache_creation_input_tokens", "cache_write_tokens"),
        ):
            value = _as_int(usage.get(key))
            if value is not None:
                setattr(self, attr, value)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def any(self) -> bool:
        return bool(self.total or self.cache_read_tokens or self.cache_write_tokens)

    @property
    def processed(self) -> int:
        """Everything the model processed: input + output + cache read/write."""
        return self.total + self.cache_read_tokens + self.cache_write_tokens

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


@dataclass(slots=True)
class ModelUsage:
    requests: int = 0
    usage: Usage = field(default_factory=Usage)


@dataclass(slots=True)
class Stats:
    started_at: float = field(default_factory=time.monotonic)
    requests: int = 0  # accepted /v1/messages requests
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    upstream_errors: int = 0  # failed upstream attempts (before retry/fallback)
    retries: int = 0
    fallbacks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    success_ms: float = 0.0
    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def avg_ms(self) -> float:
        return self.success_ms / self.succeeded if self.succeeded else 0.0

    @property
    def usage(self) -> Usage:
        return Usage(self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens)

    def add_usage(self, usage: Usage, model: str | None = None) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if model:
            entry = self.by_model.setdefault(model, ModelUsage())
            entry.requests += 1
            entry.usage.add(usage)

    def models_by_volume(self) -> list[tuple[str, ModelUsage]]:
        """Models that consumed tokens, largest first."""
        used = [(model, entry) for model, entry in self.by_model.items() if entry.usage.any]
        return sorted(used, key=lambda item: item[1].usage.processed, reverse=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "uptime_s": round(self.uptime, 1),
            "requests": self.requests,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "upstream_errors": self.upstream_errors,
            "retries": self.retries,
            "fallbacks": self.fallbacks,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "avg_ms": round(self.avg_ms, 1),
            "by_model": {
                model: {"requests": entry.requests, **entry.usage.as_dict()}
                for model, entry in self.models_by_volume()
            },
        }
