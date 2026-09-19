"""Thread-safe persistent per-thread routing state."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
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
    parent_tier: str | None = None
    agent_name: str | None = None
    subagent_kind: str | None = None
    delegation_task: str | None = None
    delegation_source: str | None = None
    routing_task: str = ""
    last_active_at: str | None = None


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

    def touch(self, thread_id: str, at: str) -> ThreadState | None:
        return self.update(thread_id, lambda current: replace(current, last_active_at=at))

    def update_usage(
        self,
        thread_id: str,
        context_tokens: int,
        *,
        at: str | None = None,
    ) -> ThreadState | None:
        return self.update(
            thread_id,
            lambda current: replace(
                current,
                last_context_tokens=max(0, int(context_tokens)),
                last_active_at=at or current.last_active_at,
            ),
        )

    @staticmethod
    def _timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def gc_expired(self, now: datetime, *, ttl_seconds: int) -> list[str]:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(seconds=max(0, ttl_seconds))
        with self._lock:
            expired = []
            for thread_id, state in self._threads.items():
                active = self._timestamp(state.last_active_at or state.decided_at)
                if active is not None and active < cutoff:
                    expired.append(thread_id)
            for thread_id in expired:
                del self._threads[thread_id]
            if expired:
                self._save_locked()
            return sorted(expired)

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
