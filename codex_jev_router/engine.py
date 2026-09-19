"""Stateful orchestration for the four authorized routing decision points."""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .config import RouterConfig
from .jev import JevClient, load_key
from .policy import (
    ASTRA,
    SOL,
    RequestFacts,
    RouteCandidate,
    SwitchGate,
    ThreadIdentity,
    ThreadSnapshot,
    candidate_from_jev,
    choose_event,
    estimate_context_tokens,
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

    def _make_jev(self, key: str) -> JevClient:
        return JevClient(
            key=key,
            url=self.config.jev_url,
            model=self.config.jev_model,
            timeout_seconds=self.config.jev_timeout_seconds,
            retries=self.config.jev_retries,
            backoff_seconds=self.config.jev_backoff_seconds,
        )

    def _timestamp(self) -> str:
        return self.now().isoformat().replace("+00:00", "Z")

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
    def _jev_state(facts: RequestFacts) -> dict[str, Any]:
        state: dict[str, Any] = {
            "task": facts.task[:500],
            "signals": {
                "n_items": facts.n_items,
                "has_image": facts.has_image,
                "tool_history": facts.has_tool_history,
            },
            "step": {"type": facts.step_type},
        }
        if facts.previous_assistant:
            state["previous_assistant"] = facts.previous_assistant[-240:]
        return state

    def _query_candidate(
        self, facts: RequestFacts
    ) -> tuple[RouteCandidate, bool, int | None, dict[str, Any] | None]:
        key = self.key_loader()
        if not key:
            return RouteCandidate(ASTRA, "medium", "default", "no_key"), False, None, None
        started = self.monotonic()
        try:
            response = self.jev_factory(key).ask(self._jev_state(facts))
            answers = response.get("answers") if isinstance(response, dict) else None
            answers = answers if isinstance(answers, dict) else {}
            tier_answer = answers.get("tier") if isinstance(answers.get("tier"), dict) else {}
            depth_answer = answers.get("depth") if isinstance(answers.get("depth"), dict) else {}
            candidate = candidate_from_jev(
                tier_answer.get("choice"),
                depth_answer.get("choice"),
                tier_answer.get("confidence"),
                self.config.confidence_gate,
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
            return candidate, True, int(round((self.monotonic() - started) * 1000)), observation
        except Exception as exc:
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
        )

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

        stored = self.store.get(identity.thread_id)
        context_tokens = (
            stored.last_context_tokens
            if stored is not None and stored.last_context_tokens > 0
            else estimate_context_tokens(body_chars, self.config.chars_per_token)
        )
        facts = inspect_request(payload)
        event = choose_event(identity, self._snapshot(stored), facts)

        if self.exists(self.config.off_path):
            return self._direct_decision(identity, event=event, gate="off", context_tokens=context_tokens)

        if event in ("tool_continuation", "reuse") and stored is not None:
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
            policy_route, consulted, jev_ms, jev = self._query_candidate(facts)
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

        state = ThreadState(
            model=policy_route.model,
            effort=policy_route.effort,
            service_tier=policy_route.service_tier,
            last_context_tokens=context_tokens,
            last_turn_id=identity.turn_id,
            pending_free_reroute=pending_free_reroute,
            parent_thread_id=identity.parent_thread_id,
            decided_at=self._timestamp(),
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
        )

    def record_usage(self, thread_id: str | None, context_tokens: int | None) -> None:
        if thread_id and isinstance(context_tokens, int) and context_tokens >= 0:
            self.store.update_usage(thread_id, context_tokens)
