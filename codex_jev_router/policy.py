"""Pure request classification and routing policy."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


LUNA = "gpt-5.6-luna"
SOL = "gpt-5.6-sol"
ASTRA = "gpt-6-astra"
TIERS = (LUNA, SOL, ASTRA)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
COMPACTION_PREFIX = "You are performing a CONTEXT CHECKPOINT COMPACTION"
TOOL_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")
TIER_RANK = {LUNA: 0, SOL: 1, ASTRA: 2}


@dataclass(frozen=True)
class ThreadIdentity:
    thread_id: str | None
    turn_id: str | None
    parent_thread_id: str | None
    parent_turn_id: str | None
    is_subagent: bool
    conflict: bool
    sources: dict[str, str]
    metadata: dict[str, Any]
    agent_name: str | None
    subagent_kind: str | None


@dataclass(frozen=True)
class RequestFacts:
    task: str
    previous_assistant: str
    step_type: str
    tool_output_tail: str
    tool_error: bool
    is_compaction: bool
    request_kind: str | None
    compaction: dict[str, Any]
    n_items: int
    has_image: bool
    has_tool_history: bool


@dataclass(frozen=True)
class SpawnDelegation:
    agent_name: str | None
    task: str | None
    source: str


@dataclass(frozen=True)
class ThreadSnapshot:
    model: str
    effort: str
    last_context_tokens: int
    last_turn_id: str | None
    pending_free_reroute: bool
    parent_thread_id: str | None


@dataclass(frozen=True)
class RouteCandidate:
    model: str
    effort: str
    service_tier: str
    gate: str


@dataclass(frozen=True)
class Price:
    cache_read: float
    cache_write: float
    input: float = 0.0
    output: float = 0.0


@dataclass(frozen=True)
class SwitchGate:
    allowed: bool
    reason: str
    stay_cost: float | None
    switch_cost: float | None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def resolve_identity(headers: Mapping[str, str], payload: Mapping[str, Any]) -> ThreadIdentity:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    client = payload.get("client_metadata")
    client = client if isinstance(client, dict) else {}
    metadata = _json_object(lowered.get("x-codex-turn-metadata"))
    if not metadata:
        metadata = _json_object(client.get("x-codex-turn-metadata"))

    sources = {}
    for label, value in (
        ("header", lowered.get("thread-id")),
        ("client_metadata", client.get("thread_id")),
        ("turn_metadata", metadata.get("thread_id")),
    ):
        if isinstance(value, str) and value:
            sources[label] = value
    thread_id = sources.get("header") or sources.get("client_metadata") or sources.get("turn_metadata")
    conflict = len(set(sources.values())) > 1

    parent_thread_id = (
        lowered.get("x-codex-parent-thread-id")
        or client.get("x-codex-parent-thread-id")
        or metadata.get("parent_thread_id")
    )
    turn_id = metadata.get("turn_id") or client.get("turn_id")
    parent_turn_id = metadata.get("parent_turn_id") or client.get("parent_turn_id")
    is_subagent = bool(
        lowered.get("x-openai-subagent") == "collab_spawn"
        or parent_thread_id
        or metadata.get("thread_source") == "subagent"
        or metadata.get("subagent_kind") == "thread_spawn"
    )
    return ThreadIdentity(
        thread_id=thread_id,
        turn_id=turn_id if isinstance(turn_id, str) else None,
        parent_thread_id=parent_thread_id if isinstance(parent_thread_id, str) else None,
        parent_turn_id=parent_turn_id if isinstance(parent_turn_id, str) else None,
        is_subagent=is_subagent,
        conflict=conflict,
        sources=sources,
        metadata=metadata,
        agent_name=metadata.get("agent_name") if isinstance(metadata.get("agent_name"), str) else None,
        subagent_kind=(
            metadata.get("subagent_kind") if isinstance(metadata.get("subagent_kind"), str) else None
        ),
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("input_text", "output_text") and isinstance(part.get("text"), str):
            pieces.append(part["text"])
    return "".join(pieces)


def _item_text(item: Mapping[str, Any]) -> str:
    if isinstance(item.get("output"), str):
        return item["output"]
    return _content_text(item.get("content"))


def _encrypted_message(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("gAAAAA")


def extract_spawn_delegations(payload: Mapping[str, Any]) -> list[SpawnDelegation]:
    raw_input = payload.get("input")
    items = raw_input if isinstance(raw_input, list) else []
    delegations = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        if item.get("name") != "spawn_agent":
            continue
        arguments = _json_object(item.get("arguments"))
        agent_name = arguments.get("task_name")
        agent_name = agent_name if isinstance(agent_name, str) and agent_name else None
        message = arguments.get("message")
        if _encrypted_message(message):
            task = None
            source = "spawn_message_encrypted"
        elif isinstance(message, str) and message.strip():
            task = message.strip()
            source = "spawn_message"
        else:
            task = None
            source = "spawn_message_missing"
        delegations.append(SpawnDelegation(agent_name, task, source))
    return delegations


def extract_new_task_delegation(payload: Mapping[str, Any]) -> SpawnDelegation | None:
    raw_input = payload.get("input")
    items = raw_input if isinstance(raw_input, list) else []
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = _item_text(item)
        if not text.lstrip().startswith("Message Type: NEW_TASK"):
            continue
        match = re.search(r"(?m)^Task name:\s*(\S.*?)\s*$", text)
        agent_name = match.group(1).strip() if match else None
        payload_match = re.search(r"(?ms)^Payload:\s*\n?(.*)\Z", text)
        task = payload_match.group(1).strip() if payload_match else ""
        content = item.get("content")
        encrypted = isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "encrypted_content" for part in content
        )
        if task:
            return SpawnDelegation(agent_name, task, "new_task_payload")
        return SpawnDelegation(
            agent_name,
            None,
            "new_task_encrypted" if encrypted else "new_task_missing",
        )
    return None


def inspect_request(
    payload: Mapping[str, Any], metadata: Mapping[str, Any] | None = None
) -> RequestFacts:
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        items: list[dict[str, Any]] = []
        task = raw_input
        previous = ""
        step_type = "user_turn"
    else:
        items = [item for item in (raw_input or []) if isinstance(item, dict)]
        task = ""
        previous = ""
        for item in reversed(items):
            role = item.get("role")
            if not task and role == "user":
                task = _item_text(item)
            if not previous and role == "assistant":
                previous = _item_text(item)
            if task and previous:
                break
        last = items[-1] if items else {}
        if last.get("type") in TOOL_OUTPUT_TYPES:
            step_type = "tool_step"
        elif last.get("role") == "user":
            step_type = "user_turn"
        else:
            step_type = "other"

    last_item = items[-1] if items else {}
    tool_text = _item_text(last_item) if last_item.get("type") in TOOL_OUTPUT_TYPES else ""
    lowered_tool_text = tool_text[-4000:].lower()
    error_words = ("traceback", "error", "failed", "assertion", "exception", "fatal", "panic")
    has_image = any("input_image" in json.dumps(item, ensure_ascii=False) or "image_url" in item for item in items)
    has_tool_history = any(item.get("type") in TOOL_OUTPUT_TYPES for item in items[-6:])
    turn_metadata = metadata if isinstance(metadata, Mapping) else {}
    request_kind = turn_metadata.get("request_kind")
    request_kind = request_kind if isinstance(request_kind, str) else None
    compaction = turn_metadata.get("compaction")
    compaction_details = compaction if isinstance(compaction, dict) else {}
    has_compaction_metadata = request_kind is not None or "compaction" in turn_metadata
    if request_kind == "compaction" or bool(compaction):
        is_compaction = True
    elif has_compaction_metadata:
        is_compaction = False
    else:
        is_compaction = task.lstrip().startswith(COMPACTION_PREFIX)
    return RequestFacts(
        task=task,
        previous_assistant=previous,
        step_type=step_type,
        tool_output_tail=tool_text[-520:],
        tool_error=any(word in lowered_tool_text for word in error_words),
        is_compaction=is_compaction,
        request_kind=request_kind,
        compaction=compaction_details,
        n_items=len(items),
        has_image=has_image,
        has_tool_history=has_tool_history,
    )


def choose_event(
    identity: ThreadIdentity, state: ThreadSnapshot | None, facts: RequestFacts
) -> str:
    if facts.is_compaction:
        return "compaction"
    if state is None:
        return "subagent_first" if identity.is_subagent else "first_request"
    if identity.turn_id != state.last_turn_id:
        return "free_reroute" if state.pending_free_reroute else "new_user_turn"
    if facts.step_type == "tool_step":
        return "tool_continuation"
    return "reuse"


def clamp_effort(depth: Any) -> str:
    return depth if depth in EFFORTS else "medium"


def candidate_from_jev(
    tier: Any,
    depth: Any,
    confidence: Any,
    confidence_gate: float = 0.5,
    luna_effort: str = "max",
) -> RouteCandidate:
    if tier not in TIERS or not isinstance(confidence, (int, float)):
        raise ValueError("invalid Jev tier answer")
    effort = clamp_effort(depth)
    if confidence < confidence_gate:
        return RouteCandidate(SOL, effort, "default", "low_confidence")
    if tier == LUNA:
        return RouteCandidate(LUNA, clamp_effort(luna_effort), "priority", "jev")
    return RouteCandidate(tier, effort, "default", "jev")


def estimate_context_tokens(body_chars: int, chars_per_token: float) -> int:
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be positive")
    return int(math.ceil(max(0, body_chars) / chars_per_token))


def switch_gate(
    context_tokens: int,
    current_model: str,
    target_model: str,
    prices: Mapping[str, Price],
    budget_usd: float,
    downgrade_max_context_tokens: int,
) -> SwitchGate:
    if current_model not in TIER_RANK or target_model not in TIER_RANK:
        return SwitchGate(False, "unknown_model", None, None)
    if current_model not in prices or target_model not in prices:
        return SwitchGate(False, "missing_price", None, None)
    token_factor = Decimal(context_tokens) / Decimal(1_000_000)
    stay_decimal = token_factor * Decimal(str(prices[current_model].cache_read))
    if current_model == target_model:
        same_model_cost = float(stay_decimal)
        return SwitchGate(True, "same_model", same_model_cost, same_model_cost)
    if TIER_RANK[target_model] < TIER_RANK[current_model] and context_tokens > downgrade_max_context_tokens:
        return SwitchGate(False, "downgrade_context_limit", 0.0, 0.0)
    switch_decimal = token_factor * Decimal(str(prices[target_model].cache_write))
    allowed = switch_decimal - stay_decimal <= Decimal(str(budget_usd))
    stay_cost = float(stay_decimal)
    switch_cost = float(switch_decimal)
    return SwitchGate(allowed, "affordable" if allowed else "switch_budget", stay_cost, switch_cost)
