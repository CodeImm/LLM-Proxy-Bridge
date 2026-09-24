"""Interface between request processing and whatever displays it."""

from __future__ import annotations

from typing import Protocol

from .context import Outcome, RequestContext
from .errors import UpstreamError


class Reporter(Protocol):
    def request_started(self, ctx: RequestContext) -> None: ...

    def request_finished(self, ctx: RequestContext, outcome: Outcome) -> None: ...

    def phase_changed(self, ctx: RequestContext) -> None: ...

    def progress(self, ctx: RequestContext) -> None:
        """Called for every relayed stream line; implementations throttle."""

    def retrying(
        self,
        ctx: RequestContext,
        model: str,
        attempt: int,
        max_attempts: int,
        delay: float,
        error: UpstreamError,
    ) -> None: ...

    def model_failed(
        self,
        ctx: RequestContext,
        model: str,
        error: UpstreamError,
        cooldown: float | None,
        next_model: str | None,
    ) -> None: ...

    def all_models_cooling_down(self, ctx: RequestContext) -> None: ...

    def unknown_route(self, method: str, path: str) -> None: ...


class NullReporter:
    """Reporter that discards everything (tests, embedding)."""

    def request_started(self, ctx: RequestContext) -> None:
        pass

    def request_finished(self, ctx: RequestContext, outcome: Outcome) -> None:
        pass

    def phase_changed(self, ctx: RequestContext) -> None:
        pass

    def progress(self, ctx: RequestContext) -> None:
        pass

    def retrying(self, ctx, model, attempt, max_attempts, delay, error) -> None:
        pass

    def model_failed(self, ctx, model, error, cooldown, next_model) -> None:
        pass

    def all_models_cooling_down(self, ctx: RequestContext) -> None:
        pass

    def unknown_route(self, method: str, path: str) -> None:
        pass
