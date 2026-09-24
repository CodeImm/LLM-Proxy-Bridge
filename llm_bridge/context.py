"""Per-request state shared between the proxy, upstream client and UI."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .stats import Usage


class Phase(str, Enum):
    QUEUED = "queued"
    WAITING = "waiting"  # request sent, waiting for the first byte
    BACKOFF = "backoff"  # sleeping before a retry
    STREAMING = "streaming"  # message_start received
    THINKING = "thinking"
    WRITING = "writing"
    TOOL = "tool"
    DONE = "done"


def new_request_id() -> str:
    return uuid.uuid4().hex[:8].upper()


@dataclass(slots=True, eq=False)
class RequestContext:
    original_model: str
    chain: list[str]
    passthrough: bool
    stream: bool
    has_system: bool = False
    tool_count: int = 0
    req_id: str = field(default_factory=new_request_id)
    started: float = field(default_factory=time.perf_counter)

    # Progress, mutated while the request is in flight.
    model: str | None = None  # candidate being tried / model that answered
    phase: Phase = Phase.QUEUED
    phase_detail: str = ""  # e.g. tool name or "2/5"
    attempts: int = 0
    fallbacks: int = 0
    skipped: list[str] = field(default_factory=list)
    first_event_at: float | None = None
    usage: Usage = field(default_factory=Usage)
    last_progress_log: float = 0.0

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def ttft(self) -> float | None:
        """Seconds until the first upstream SSE event (streams only)."""
        if self.first_event_at is None:
            return None
        return self.first_event_at - self.started

    @property
    def rewritten(self) -> bool:
        return not self.passthrough and bool(self.chain) and self.chain[0] != self.original_model

    def set_phase(self, phase: Phase, detail: str = "") -> None:
        self.phase = phase
        self.phase_detail = detail


class Result(str, Enum):
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Outcome:
    result: Result
    status: int
    error: str | None = None
    warning: str | None = None
