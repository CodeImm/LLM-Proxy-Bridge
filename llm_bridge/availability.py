"""Per-model cooldown tracking (a simple circuit breaker)."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class Cooldown:
    until: float  # time.monotonic() deadline
    reason: str

    def remaining(self, now: float) -> float:
        return self.until - now


class ModelAvailability:
    def __init__(self) -> None:
        self._down: dict[str, Cooldown] = {}

    def mark_down(self, model: str, ttl: float, reason: str) -> None:
        if ttl <= 0:
            return
        self._down[model] = Cooldown(time.monotonic() + ttl, reason)

    def is_up(self, model: str) -> bool:
        entry = self._down.get(model)
        if entry is None:
            return True
        if entry.remaining(time.monotonic()) <= 0:
            self._down.pop(model, None)
            return True
        return False

    def split(self, chain: list[str]) -> tuple[list[str], list[str]]:
        """Return ``(available, cooling_down)`` preserving chain order."""
        available: list[str] = []
        skipped: list[str] = []
        for model in chain:
            (available if self.is_up(model) else skipped).append(model)
        return available, skipped

    def snapshot(self) -> dict[str, tuple[float, str]]:
        """``{model: (remaining_seconds, reason)}`` for models still cooling down."""
        now = time.monotonic()
        result: dict[str, tuple[float, str]] = {}
        for model, entry in list(self._down.items()):
            remaining = entry.remaining(now)
            if remaining > 0:
                result[model] = (remaining, entry.reason)
            else:
                self._down.pop(model, None)
        return result
