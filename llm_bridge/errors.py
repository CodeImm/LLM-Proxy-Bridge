"""Upstream error classification, retry backoff and Anthropic-style error bodies."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

from .config import Settings


class ErrorKind(str, Enum):
    RATE = "rate"  # 429 / overloaded / quota → retry, then cooldown
    AUTH = "auth"  # 401 / 403 → retry (CPA may rotate accounts), long cooldown
    SKIP = "skip"  # model not found / unsupported → cooldown, next model
    TRANSIENT = "transient"  # 5xx / timeouts / dropped connections → retry
    FATAL = "fatal"  # client error (bad request) → return to client as is

    @property
    def retryable(self) -> bool:
        return self in (ErrorKind.RATE, ErrorKind.AUTH, ErrorKind.TRANSIENT)


RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

_RATE_TEXTS = (
    "overloaded",
    "rate limit",
    "capacity",
    "resource exhausted",
    "quota",
    "too many requests",
)
_AUTH_TEXTS = ("invalid_grant", "unauthorized", "unauthenticated", "invalid api key", "authentication")
_SKIP_TEXTS = (
    "not found",
    "not supported",
    "unknown model",
    "invalid model",
    "does not exist",
    "unknown provider",  # CPA: no credential serves this model (400/502)
    "model_not_found",
)
_TRANSIENT_TEXTS = (
    "timeout",
    "temporarily unavailable",
    "backend error",
    "internal error",
    "service unavailable",
)


def classify_error(status: int, message: str) -> ErrorKind:
    text = message.lower()

    if status in (401, 403) or any(t in text for t in _AUTH_TEXTS):
        return ErrorKind.AUTH
    if status == 404 or any(t in text for t in _SKIP_TEXTS):
        return ErrorKind.SKIP
    if status == 429 or any(t in text for t in _RATE_TEXTS):
        return ErrorKind.RATE
    if status in RETRYABLE_STATUS_CODES or any(t in text for t in _TRANSIENT_TEXTS):
        return ErrorKind.TRANSIENT
    return ErrorKind.FATAL


def cooldown_for(kind: ErrorKind, settings: Settings) -> float | None:
    return {
        ErrorKind.RATE: settings.cooldown_rate,
        ErrorKind.AUTH: settings.cooldown_auth,
        ErrorKind.SKIP: settings.cooldown_skip,
        ErrorKind.TRANSIENT: settings.cooldown_transient,
        ErrorKind.FATAL: None,
    }[kind]


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def backoff_delay(attempt: int, settings: Settings, retry_after: str | None = None) -> float:
    """Delay before retry number ``attempt`` (0-based), honouring ``Retry-After``."""
    hinted = parse_retry_after(retry_after)
    if hinted is not None:
        return min(hinted, settings.retry_max_delay)

    base = min(settings.retry_base_delay * (2**attempt), settings.retry_max_delay)
    return base + random.uniform(0, base * 0.2)


# ============================================================================
# Error bodies
# ============================================================================

_STATUS_TO_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}

_TYPE_TO_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "timeout_error": 504,
    "overloaded_error": 529,
}


def error_type_for_status(status: int) -> str:
    return _STATUS_TO_TYPE.get(status, "api_error")


def status_for_error_type(error_type: str | None) -> int:
    return _TYPE_TO_STATUS.get(error_type or "", 502)


def anthropic_error(message: str, *, status: int = 502, error_type: str | None = None) -> dict[str, Any]:
    """Error body in the format Anthropic clients (Claude Code) understand."""
    return {
        "type": "error",
        "error": {
            "type": error_type or error_type_for_status(status),
            "message": message,
        },
    }


def extract_error_message(content: bytes | str | None, *, limit: int = 300) -> str:
    """Best-effort human-readable message from an upstream error body."""
    if not content:
        return ""
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    try:
        body = json.loads(text)
    except ValueError:
        return " ".join(text.split())[:limit]

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:limit]
        if isinstance(error, str) and error:
            return error[:limit]
        message = body.get("message")
        if isinstance(message, str) and message:
            return message[:limit]
    return json.dumps(body, ensure_ascii=False)[:limit]


def error_body_from_upstream(content: bytes, status: int) -> dict[str, Any]:
    """Pass a JSON error object through untouched, wrap anything else."""
    try:
        body = json.loads(content)
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    message = extract_error_message(content) or f"upstream HTTP {status}"
    return anthropic_error(message, status=status)


# ============================================================================
# Attempt failures
# ============================================================================


class UpstreamError(Exception):
    """A single failed upstream attempt."""

    def __init__(
        self,
        kind: ErrorKind,
        status: int,
        message: str,
        *,
        body: dict[str, Any] | None = None,
        retry_after: str | None = None,
        timeout: bool = False,
        connect: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.message = message
        self.body = body
        self.retry_after = retry_after
        self.timeout = timeout
        self.connect = connect

    @property
    def label(self) -> str:
        """Short description for logs: ``HTTP 429 rate limit`` / ``timeout``."""
        if self.connect:
            return "нет соединения с CPA"
        if self.timeout:
            return "timeout"
        return f"HTTP {self.status}"

    def to_body(self) -> dict[str, Any]:
        return self.body or anthropic_error(self.message or self.label, status=self.status)
