"""Thread-safe persistent per-thread routing state."""

from __future__ import annotations

import json
import os
import sys
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
    def __init__(self, path: str | Path, *, flush_interval_seconds: float = 2.0) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._flush_lock = threading.Lock()
        self._threads: dict[str, ThreadState] = {}
        self._dirty_generation = 0
        self._flushed_generation = 0
        self._closed = False
        self._flush_interval_seconds = max(0.001, float(flush_interval_seconds))
        self._load()
        self._worker = threading.Thread(
            target=self._flush_loop,
            name="codex-jev-state-flush",
            daemon=True,
        )
        self._worker.start()

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
        with self._condition:
            self._threads[thread_id] = state
            self._mark_dirty_locked()

    def update(self, thread_id: str, update: Callable[[ThreadState], ThreadState]) -> ThreadState | None:
        with self._condition:
            current = self._threads.get(thread_id)
            if current is None:
                return None
            changed = update(current)
            self._threads[thread_id] = changed
            self._mark_dirty_locked()
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
        with self._condition:
            expired = []
            for thread_id, state in self._threads.items():
                active = self._timestamp(state.last_active_at or state.decided_at)
                if active is not None and active < cutoff:
                    expired.append(thread_id)
            for thread_id in expired:
                del self._threads[thread_id]
            if expired:
                self._mark_dirty_locked()
            return sorted(expired)

    def _mark_dirty_locked(self) -> None:
        self._dirty_generation += 1

    def _snapshot_locked(self) -> dict[str, object]:
        return {
            "version": 1,
            "threads": {
                thread_id: asdict(state)
                for thread_id, state in sorted(self._threads.items())
            },
        }

    def _write_snapshot(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def flush(self) -> bool:
        """Persist one consistent snapshot without holding the state lock during I/O."""

        with self._flush_lock:
            with self._lock:
                generation = self._dirty_generation
                if generation <= self._flushed_generation:
                    return False
                payload = self._snapshot_locked()
            self._write_snapshot(payload)
            with self._lock:
                self._flushed_generation = max(self._flushed_generation, generation)
            return True

    def _flush_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait(timeout=self._flush_interval_seconds)
                if self._closed:
                    return
            try:
                self.flush()
            except OSError as exc:
                print(
                    f"[codex-jev-router] state background flush failed: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )

    def close(self) -> None:
        with self._condition:
            first_close = not self._closed
            self._closed = True
            self._condition.notify_all()
        if first_close and self._worker is not threading.current_thread():
            self._worker.join()
        self.flush()
