"""Model routing: which upstream model chain serves a requested model."""

from __future__ import annotations

from collections.abc import Mapping

from .config import FALSE_VALUES, TRUE_VALUES, RoutingProfile, Settings

_PASSTHROUGH_POLICIES = frozenset({"passthrough", "raw", "direct", "none"})
_REWRITE_POLICIES = frozenset({"rewrite", "default", "policy"})


class Router:
    """Resolves a client model id into an ordered fallback chain."""

    def __init__(self, settings: Settings, profile: RoutingProfile) -> None:
        self.profile = profile
        self.passthrough_default = settings.passthrough_default
        self._haiku_sources = frozenset(m.lower() for m in settings.haiku_source_models)
        self._heavy_aliases = frozenset(m.lower() for m in settings.heavy_aliases)
        self._haiku_source_list = settings.haiku_source_models

    def chain(self, model: str, *, passthrough: bool) -> list[str]:
        if passthrough:
            return [model]

        lower = model.lower()

        if lower in self._haiku_sources or "haiku" in lower:
            return [self.profile.haiku_model]

        if lower in self._heavy_aliases or lower == "claude" or lower.startswith("claude-"):
            return list(self.profile.heavy_models)

        # Gemini and unknown models go through unchanged.
        return [model]

    def haiku_rewrite(self) -> dict[str, str]:
        return {model: self.profile.haiku_model for model in self._haiku_source_list}

    def resolve_passthrough(self, headers: Mapping[str, str]) -> bool:
        """Per-request override via headers, otherwise the global default.

        ``X-Bridge-Passthrough: 1|0`` wins over ``X-Bridge-Policy: passthrough|rewrite``.
        """
        flag = headers.get("x-bridge-passthrough", "").strip().lower()
        if flag in TRUE_VALUES:
            return True
        if flag in FALSE_VALUES:
            return False

        policy = headers.get("x-bridge-policy", "").strip().lower()
        if policy in _PASSTHROUGH_POLICIES:
            return True
        if policy in _REWRITE_POLICIES:
            return False

        return self.passthrough_default
