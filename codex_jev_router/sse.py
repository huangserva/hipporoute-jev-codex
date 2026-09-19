"""Responses SSE assembly and usage extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _events(raw: bytes):
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def assemble_sse(raw: bytes) -> dict[str, Any] | None:
    final = None
    error = None
    items = {}
    for event in _events(raw):
        event_type = event.get("type")
        if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            items[event.get("output_index") or 0] = event["item"]
        elif event_type == "response.completed" and isinstance(event.get("response"), dict):
            final = event["response"]
        elif event_type in ("response.failed", "error"):
            error = event
    if final is not None:
        if not final.get("output") and items:
            final["output"] = [items[index] for index in sorted(items)]
        return final
    if error is not None:
        return {"error": error}
    return None


@dataclass
class Usage:
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None


class SSEUsageTracker:
    def __init__(self) -> None:
        self._buffer = b""
        self.usage = Usage()
        self.response_model: str | None = None

    def feed(self, raw: bytes) -> None:
        self._buffer += raw
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._process_line(line.rstrip(b"\r"))

    def finish(self) -> None:
        if self._buffer:
            self._process_line(self._buffer.rstrip(b"\r"))
            self._buffer = b""

    def _process_line(self, raw_line: bytes) -> None:
        if not raw_line.startswith(b"data:"):
            return
        chunk = raw_line[5:].strip()
        if not chunk or chunk == b"[DONE]":
            return
        try:
            event = json.loads(chunk.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(event, dict) or event.get("type") != "response.completed":
            return
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        model = response.get("model")
        if isinstance(model, str):
            self.response_model = model
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("input_tokens_details")
        details = details if isinstance(details, dict) else {}
        input_tokens = usage.get("input_tokens")
        cached_tokens = usage.get("cached_tokens", details.get("cached_tokens"))
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int):
            self.usage.input_tokens = input_tokens
        if isinstance(cached_tokens, int):
            self.usage.cached_tokens = cached_tokens
        if isinstance(output_tokens, int):
            self.usage.output_tokens = output_tokens
