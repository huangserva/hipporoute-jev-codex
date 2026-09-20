"""Stateful orchestration for the four authorized routing decision points."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .config import RouterConfig
from .jev import JevClient, load_key, questions_for
from .policy import (
    ASTRA,
    SOL,
    RequestFacts,
    RouteCandidate,
    SpawnDelegation,
    SwitchGate,
    ThreadIdentity,
    ThreadSnapshot,
    candidate_from_jev,
    choose_event,
    estimate_context_tokens,
    extract_new_task_delegation,
    extract_spawn_delegations,
    inspect_request,
    resolve_identity,
    switch_gate,
)
from .state import ThreadState, ThreadStateStore


@dataclass(frozen=True)
class Decision:
    model: str
    effort: str
    service_tier: str
    gate: str
    event: str
    consulted_jev: bool
    identity: ThreadIdentity
    context_tokens: int
    reason: str
    cost: SwitchGate | None = None
    would: dict[str, Any] | None = None
    jev_ms: int | None = None
    shadow: bool = False
    jev: dict[str, Any] | None = None
    parent_tier: str | None = None
    agent_name: str | None = None
    subagent_kind: str | None = None
    delegation_source: str | None = None
    routing_task: str = ""


class RouterEngine:
    def __init__(
        self,
        config: RouterConfig,
        store: ThreadStateStore,
        *,
        key_loader: Callable[[], str] = load_key,
        jev_factory: Callable[[str], Any] | None = None,
        exists: Callable[[os.PathLike[str] | str], bool] = os.path.exists,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.store = store
        self.key_loader = key_loader
        self.jev_factory = jev_factory or self._make_jev
        self.exists = exists
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self._thread_locks_guard = threading.Lock()
        self._thread_locks: dict[str, threading.RLock] = {}
        self._gc_lock = threading.Lock()
        self._last_gc_at = 0.0
        self._delegation_lock = threading.Lock()
        self._delegations: dict[tuple[str, str], tuple[SpawnDelegation, float]] = {}
        self._jev_circuit_lock = threading.Lock()
        self._jev_consecutive_failures = 0
        self._jev_circuit_open_until = 0.0

    def _make_jev(self, key: str) -> JevClient:
        return JevClient(
            key=key,
            url=self.config.jev_url,
            model=self.config.jev_model,
            timeout_seconds=self.config.jev_timeout_seconds,
            retries=self.config.jev_retries,
            backoff_seconds=self.config.jev_backoff_seconds,
            retry_after_cap_seconds=self.config.jev_retry_after_cap_seconds,
        )

    def _jev_circuit_is_open(self) -> bool:
        with self._jev_circuit_lock:
            if self._jev_circuit_open_until <= 0:
                return False
            if self.monotonic() < self._jev_circuit_open_until:
                return True
            self._jev_circuit_open_until = 0.0
            return False

    def _record_jev_success(self) -> None:
        with self._jev_circuit_lock:
            self._jev_consecutive_failures = 0
            self._jev_circuit_open_until = 0.0

    def _record_jev_failure(self) -> None:
        with self._jev_circuit_lock:
            self._jev_consecutive_failures += 1
            threshold = max(1, self.config.jev_circuit_failure_threshold)
            if self._jev_consecutive_failures >= threshold:
                self._jev_circuit_open_until = self.monotonic() + max(
                    0.0,
                    self.config.jev_circuit_open_seconds,
                )

    def _timestamp(self) -> str:
        return self.now().isoformat().replace("+00:00", "Z")

    def _thread_lock(self, thread_id: str) -> threading.RLock:
        with self._thread_locks_guard:
            return self._thread_locks.setdefault(thread_id, threading.RLock())

    def _maybe_gc(self) -> None:
        current = self.monotonic()
        if current - self._last_gc_at < self.config.state_gc_interval_seconds:
            return
        with self._gc_lock:
            if current - self._last_gc_at < self.config.state_gc_interval_seconds:
                return
            expired_threads = self.store.gc_expired(
                self.now(), ttl_seconds=self.config.state_ttl_seconds
            )
            with self._thread_locks_guard:
                for thread_id in expired_threads:
                    lock = self._thread_locks.get(thread_id)
                    if lock is None or not lock.acquire(blocking=False):
                        continue
                    try:
                        if self.store.get(thread_id) is None:
                            self._thread_locks.pop(thread_id, None)
                    finally:
                        lock.release()
            with self._delegation_lock:
                expired = [key for key, (_, deadline) in self._delegations.items() if deadline < current]
                for key in expired:
                    del self._delegations[key]
            self._last_gc_at = current

    @staticmethod
    def _agent_key(agent_name: str | None) -> str | None:
        if not agent_name:
            return None
        return agent_name.strip("/").rsplit("/", 1)[-1] or None

    def record_delegations(
        self,
        parent_thread_id: str | None,
        delegations: list[SpawnDelegation],
    ) -> None:
        if not parent_thread_id or not delegations:
            return
        deadline = self.monotonic() + self.config.state_ttl_seconds
        with self._delegation_lock:
            for delegation in delegations:
                key = self._agent_key(delegation.agent_name)
                if key and delegation.task:
                    self._delegations[(parent_thread_id, key)] = (delegation, deadline)

    def _cached_delegation(
        self,
        parent_thread_id: str | None,
        agent_name: str | None,
    ) -> SpawnDelegation | None:
        key = self._agent_key(agent_name)
        if not parent_thread_id or not key:
            return None
        with self._delegation_lock:
            found = self._delegations.get((parent_thread_id, key))
            if found is None:
                return None
            delegation, deadline = found
            if deadline < self.monotonic():
                del self._delegations[(parent_thread_id, key)]
                return None
            return delegation

    @staticmethod
    def _snapshot(state: ThreadState | None) -> ThreadSnapshot | None:
        if state is None:
            return None
        return ThreadSnapshot(
            model=state.model,
            effort=state.effort,
            last_context_tokens=state.last_context_tokens,
            last_turn_id=state.last_turn_id,
            pending_free_reroute=state.pending_free_reroute,
            parent_thread_id=state.parent_thread_id,
        )

    @staticmethod
    def _jev_state(
        facts: RequestFacts,
        routing_task: str,
        delegation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "task": routing_task[:500],
            "signals": {
                "n_items": facts.n_items,
                "has_image": facts.has_image,
                "tool_history": facts.has_tool_history,
            },
            "step": {"type": facts.step_type},
        }
        if facts.previous_assistant:
            state["previous_assistant"] = facts.previous_assistant[-240:]
        if delegation is not None:
            state["delegation"] = delegation
        return state

    def _query_candidate(
        self,
        facts: RequestFacts,
        routing_task: str,
        delegation: dict[str, Any] | None = None,
    ) -> tuple[RouteCandidate, bool, int | None, dict[str, Any] | None]:
        if self._jev_circuit_is_open():
            return RouteCandidate(ASTRA, "medium", "default", "jev_circuit_open"), False, None, None
        try:
            key = self.key_loader()
        except Exception as exc:
            return (
                RouteCandidate(ASTRA, "medium", "default", f"jev_error:{type(exc).__name__}"),
                False,
                None,
                None,
            )
        if not key:
            return RouteCandidate(ASTRA, "medium", "default", "no_key"), False, None, None
        started = self.monotonic()
        try:
            response = self.jev_factory(key).ask(
                self._jev_state(facts, routing_task, delegation),
                questions=questions_for(is_subagent=delegation is not None),
            )
            answers = response.get("answers") if isinstance(response, dict) else None
            answers = answers if isinstance(answers, dict) else {}
            tier_answer = answers.get("tier") if isinstance(answers.get("tier"), dict) else {}
            depth_answer = answers.get("depth") if isinstance(answers.get("depth"), dict) else {}
            candidate = candidate_from_jev(
                tier_answer.get("choice"),
                depth_answer.get("choice"),
                tier_answer.get("confidence"),
                self.config.confidence_gate,
                self.config.luna_effort,
            )
            observation = {
                "tier": {
                    "choice": tier_answer.get("choice"),
                    "confidence": tier_answer.get("confidence"),
                },
                "depth": {
                    "choice": depth_answer.get("choice"),
                    "confidence": depth_answer.get("confidence"),
                },
            }
            self._record_jev_success()
            return candidate, True, int(round((self.monotonic() - started) * 1000)), observation
        except Exception as exc:
            self._record_jev_failure()
            return (
                RouteCandidate(
                    ASTRA,
                    "medium",
                    "default",
                    f"jev_error:{type(exc).__name__}",
                ),
                True,
                int(round((self.monotonic() - started) * 1000)),
                None,
            )

    def _direct_decision(
        self,
        identity: ThreadIdentity,
        *,
        event: str,
        gate: str,
        context_tokens: int = 0,
    ) -> Decision:
        return Decision(
            ASTRA,
            "medium",
            "default",
            gate,
            event,
            False,
            identity,
            context_tokens,
            reason=gate,
            agent_name=identity.agent_name,
            subagent_kind=identity.subagent_kind,
        )

    def _routing_context(
        self,
        identity: ThreadIdentity,
        facts: RequestFacts,
        payload: Mapping[str, Any],
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        if not identity.is_subagent:
            return facts.task, None, None, None
        parent = self.store.get(identity.parent_thread_id) if identity.parent_thread_id else None
        parent_tier = parent.model if parent is not None else None
        new_task = extract_new_task_delegation(payload)
        agent_name = identity.agent_name or (new_task.agent_name if new_task is not None else None)
        if new_task is not None and new_task.task:
            routing_task = new_task.task
            source = new_task.source
        elif cached := self._cached_delegation(identity.parent_thread_id, agent_name):
            routing_task = cached.task or ""
            source = cached.source
        else:
            parent_task = parent.routing_task if parent is not None and parent.routing_task else facts.task
            routing_task = (
                f"Delegated subtask agent: {agent_name or 'unknown'}. "
                "The exact delegation payload is encrypted and unavailable. "
                f"Parent task context: {parent_task}"
            )
            source = "agent_name_fallback"
        delegation = {
            "is_subagent": True,
            "parent_tier": parent_tier,
            "agent_name": agent_name,
            "subagent_kind": identity.subagent_kind,
            "source": source,
        }
        return routing_task, source, parent_tier, delegation

    def decide(
        self,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        body_chars: int,
    ) -> Decision:
        identity = resolve_identity(headers, payload)
        if identity.conflict:
            return self._direct_decision(identity, event="identity_error", gate="identity_conflict")
        if not identity.thread_id:
            return self._direct_decision(identity, event="identity_error", gate="missing_thread_id")

        self._maybe_gc()
        with self._thread_lock(identity.thread_id):
            if not identity.is_subagent:
                self.record_delegations(identity.thread_id, extract_spawn_delegations(payload))
            return self._decide_locked(identity, payload, body_chars)

    def _decide_locked(
        self,
        identity: ThreadIdentity,
        payload: Mapping[str, Any],
        body_chars: int,
    ) -> Decision:
        stored = self.store.get(identity.thread_id)
        context_tokens = (
            stored.last_context_tokens
            if stored is not None and stored.last_context_tokens > 0
            else estimate_context_tokens(body_chars, self.config.chars_per_token)
        )
        facts = inspect_request(payload, identity.metadata)
        routing_task, delegation_source, parent_tier, delegation = self._routing_context(
            identity,
            facts,
            payload,
        )
        event = choose_event(identity, self._snapshot(stored), facts)

        if self.exists(self.config.off_path):
            if stored is not None:
                self.store.touch(identity.thread_id, self._timestamp())
            return self._direct_decision(identity, event=event, gate="off", context_tokens=context_tokens)

        if event in ("tool_continuation", "reuse") and stored is not None:
            stored = self.store.touch(identity.thread_id, self._timestamp()) or stored
            decision = Decision(
                stored.model,
                stored.effort,
                stored.service_tier,
                "sticky",
                event,
                False,
                identity,
                context_tokens,
                reason="sticky",
                parent_tier=stored.parent_tier,
                agent_name=stored.agent_name,
                subagent_kind=stored.subagent_kind,
                delegation_source=stored.delegation_source,
                routing_task=stored.routing_task,
            )
            return self._shadow(decision, stored)

        if event == "compaction":
            policy_route = RouteCandidate(SOL, "high", "default", "compaction")
            pending_free_reroute = True
            consulted = False
            jev_ms = None
            jev = None
            cost = None
            gate = "apply"
            reason = "compaction"
        else:
            policy_route, consulted, jev_ms, jev = self._query_candidate(
                facts,
                routing_task,
                delegation,
            )
            pending_free_reroute = False
            cost = None
            reason = policy_route.gate
            gate = "apply" if policy_route.gate in ("jev", "low_confidence") else policy_route.gate
            if event == "new_user_turn" and stored is not None and (
                policy_route.model != stored.model or policy_route.effort != stored.effort
            ):
                cost = switch_gate(
                    context_tokens,
                    stored.model,
                    policy_route.model,
                    self.config.prices,
                    self.config.switch_budget_usd,
                    self.config.downgrade_max_context_tokens,
                )
                if not cost.allowed:
                    policy_route = RouteCandidate(
                        stored.model,
                        stored.effort,
                        stored.service_tier,
                        f"cost_hold:{cost.reason}",
                    )
                    gate = "hold"
                    reason = cost.reason

        timestamp = self._timestamp()
        state = ThreadState(
            model=policy_route.model,
            effort=policy_route.effort,
            service_tier=policy_route.service_tier,
            last_context_tokens=context_tokens,
            last_turn_id=identity.turn_id,
            pending_free_reroute=pending_free_reroute,
            parent_thread_id=identity.parent_thread_id,
            decided_at=timestamp,
            parent_tier=parent_tier,
            agent_name=identity.agent_name,
            subagent_kind=identity.subagent_kind,
            delegation_task=routing_task if identity.is_subagent else None,
            delegation_source=delegation_source,
            routing_task=routing_task,
            last_active_at=timestamp,
        )
        self.store.put(identity.thread_id, state)
        decision = Decision(
            policy_route.model,
            policy_route.effort,
            policy_route.service_tier,
            gate,
            event,
            consulted,
            identity,
            context_tokens,
            reason=reason,
            cost=cost,
            jev_ms=jev_ms,
            jev=jev,
            parent_tier=parent_tier,
            agent_name=identity.agent_name,
            subagent_kind=identity.subagent_kind,
            delegation_source=delegation_source,
            routing_task=routing_task,
        )
        return self._shadow(decision, state)

    def _shadow(self, decision: Decision, policy_state: ThreadState) -> Decision:
        if not self.exists(self.config.shadow_path):
            return decision
        would = {
            "model": policy_state.model,
            "effort": policy_state.effort,
            "service_tier": policy_state.service_tier,
            "gate": decision.gate,
            "reason": decision.reason,
        }
        return Decision(
            ASTRA,
            "medium",
            "default",
            decision.gate,
            decision.event,
            decision.consulted_jev,
            decision.identity,
            decision.context_tokens,
            reason=decision.reason,
            cost=decision.cost,
            would=would,
            jev_ms=decision.jev_ms,
            shadow=True,
            jev=decision.jev,
            parent_tier=decision.parent_tier,
            agent_name=decision.agent_name,
            subagent_kind=decision.subagent_kind,
            delegation_source=decision.delegation_source,
            routing_task=decision.routing_task,
        )

    def record_usage(self, thread_id: str | None, context_tokens: int | None) -> None:
        if thread_id and isinstance(context_tokens, int) and context_tokens >= 0:
            self.store.update_usage(thread_id, context_tokens, at=self._timestamp())
