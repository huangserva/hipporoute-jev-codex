"""Sentinel-gated, lossless upstream response chunk capture."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class RawStreamCapture:
    """Append timestamped upstream chunks to one permission-restricted JSONL."""

    def __init__(self, path: Path | None, request_id: str, thread_id: str) -> None:
        self.path = path
        self.request_id = request_id
        self.thread_id = thread_id
        self._lock = threading.Lock()
        self._chunk_index = 0
        self._closed = False

    @classmethod
    def start(
        cls,
        *,
        sentinel: Path,
        directory: Path,
        request_id: str,
        thread_id: str,
    ) -> "RawStreamCapture":
        if not Path(sentinel).is_file():
            return cls(None, request_id, thread_id)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")
        path = directory / f"{stamp}-{request_id}.jsonl"
        capture = cls(path, request_id, thread_id)
        capture.event("start")
        return capture

    @property
    def active(self) -> bool:
        return self.path is not None

    @property
    def relative_name(self) -> str | None:
        return self.path.name if self.path is not None else None

    def _append(self, row: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {
            "wall_time": datetime.now().astimezone().isoformat(timespec="microseconds"),
            "monotonic_ns": time.monotonic_ns(),
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            **row,
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self._lock:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, encoded)
            finally:
                os.close(descriptor)

    def chunk(self, data: bytes) -> None:
        if self.path is None:
            return
        self._chunk_index += 1
        self._append(
            {
                "event": "chunk",
                "chunk_index": self._chunk_index,
                "length": len(data),
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
        )

    def event(self, name: str, **fields: Any) -> None:
        self._append({"event": name, **fields})

    def close(self, *, response_completed: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self.event(
            "end",
            chunk_count=self._chunk_index,
            response_completed=response_completed,
        )
