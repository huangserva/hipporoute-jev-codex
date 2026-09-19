"""Thread-safe persistent per-thread routing state."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ThreadState:
    model: str
    effort: str
    service_tier: str
    last_context_tokens: int
    last_turn_id: str | None
    pending_free_reroute: bool
    parent_thread_id: str | None
    decided_at: str


class ThreadStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._threads: dict[str, ThreadState] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return
        raw_threads = data.get("threads") if isinstance(data, dict) else None
        if not isinstance(raw_threads, dict):
            return
        for thread_id, raw in raw_threads.items():
            try:
                self._threads[str(thread_id)] = ThreadState(**raw)
            except (TypeError, ValueError):
                continue

    def get(self, thread_id: str) -> ThreadState | None:
        with self._lock:
            return self._threads.get(thread_id)

    def put(self, thread_id: str, state: ThreadState) -> None:
        with self._lock:
            self._threads[thread_id] = state
            self._save_locked()

    def update(self, thread_id: str, update: Callable[[ThreadState], ThreadState]) -> ThreadState | None:
        with self._lock:
            current = self._threads.get(thread_id)
            if current is None:
                return None
            changed = update(current)
            self._threads[thread_id] = changed
            self._save_locked()
            return changed

    def update_usage(self, thread_id: str, context_tokens: int) -> ThreadState | None:
        return self.update(
            thread_id,
            lambda current: replace(current, last_context_tokens=max(0, int(context_tokens))),
        )

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = {
            "version": 1,
            "threads": {thread_id: asdict(state) for thread_id, state in sorted(self._threads.items())},
        }
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
