"""Server-Sent Events helpers for the Anthropic Messages stream format."""

from __future__ import annotations

import json
from typing import Any

from .context import Phase, RequestContext

# Deltas are by far the most frequent events and carry nothing we track,
# so they are recognised by prefix and never JSON-decoded.
_DELTA_PREFIXES = ('data: {"type":"content_block_delta"', 'data:{"type":"content_block_delta"')


def parse_data_line(line: str) -> dict[str, Any] | None:
    """Decode the JSON payload of an SSE ``data:`` line."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def encode_sse_error(message: str, error_type: str = "api_error") -> bytes:
    payload = json.dumps(
        {"type": "error", "error": {"type": error_type, "message": message}},
        ensure_ascii=False,
    )
    return f"event: error\ndata: {payload}\n\n".encode()


class StreamObserver:
    """Watches relayed SSE lines: usage, current phase, errors, completion."""

    def __init__(self, ctx: RequestContext) -> None:
        self.ctx = ctx
        self.message_stopped = False
        self.stop_reason: str | None = None
        self.error: dict[str, Any] | None = None

    def feed(self, line: str) -> bool:
        """Process one line; returns True when the request phase changed."""
        if line.startswith(_DELTA_PREFIXES):
            return False

        data = parse_data_line(line)
        if data is None:
            return False

        ctx = self.ctx
        before = (ctx.phase, ctx.phase_detail)
        event = data.get("type")

        if event == "message_start":
            message = data.get("message") or {}
            ctx.usage.update(message.get("usage"))
            ctx.set_phase(Phase.STREAMING)
        elif event == "content_block_start":
            block = data.get("content_block") or {}
            kind = block.get("type")
            if kind in ("thinking", "redacted_thinking"):
                ctx.set_phase(Phase.THINKING)
            elif kind in ("tool_use", "server_tool_use"):
                ctx.set_phase(Phase.TOOL, str(block.get("name") or ""))
            else:
                ctx.set_phase(Phase.WRITING)
        elif event == "message_delta":
            ctx.usage.update(data.get("usage"))
            delta = data.get("delta") or {}
            self.stop_reason = delta.get("stop_reason") or self.stop_reason
        elif event == "message_stop":
            self.message_stopped = True
        elif event == "error":
            error = data.get("error")
            self.error = error if isinstance(error, dict) else {"message": str(error)}

        return (ctx.phase, ctx.phase_detail) != before

    @property
    def error_message(self) -> str | None:
        if self.error is None:
            return None
        message = self.error.get("message") or self.error.get("type") or "stream error"
        return str(message)
