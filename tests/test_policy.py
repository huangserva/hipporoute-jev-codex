import json
import unittest
from pathlib import Path

from codex_jev_router.policy import (
    ASTRA,
    LUNA,
    SOL,
    Price,
    ThreadSnapshot,
    candidate_from_jev,
    choose_event,
    estimate_context_tokens,
    extract_new_task_delegation,
    extract_spawn_delegations,
    coordination_hint,
    inspect_request,
    resolve_identity,
    switch_gate,
)


ROOT = "01a0-root"
CHILD = "01a0-child"
TURN_1 = "01a0-turn-1"
TURN_2 = "01a0-turn-2"
FIXTURES = Path(__file__).with_name("fixtures")


def compaction_fixture():
    with open(FIXTURES / "codex-0.155.1-compaction-request.json", encoding="utf-8") as handle:
        return json.load(handle)


def payload(thread_id=ROOT, turn_id=TURN_1, input_items=None):
    return {
        "input": input_items
        or [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Do work"}]}],
        "client_metadata": {"thread_id": thread_id, "turn_id": turn_id},
    }


def headers(thread_id=ROOT, turn_id=TURN_1, **extra):
    values = {
        "thread-id": thread_id,
        "x-codex-turn-metadata": json.dumps(
            {"thread_id": thread_id, "turn_id": turn_id, "thread_source": "user"}
        ),
    }
    values.update(extra)
    return values


class IdentityTests(unittest.TestCase):
    def test_header_wins_when_all_thread_sources_agree(self):
        identity = resolve_identity(headers(), payload())
        self.assertEqual(identity.thread_id, ROOT)
        self.assertEqual(identity.turn_id, TURN_1)
        self.assertFalse(identity.conflict)

    def test_falls_back_to_client_metadata_then_turn_metadata(self):
        from_body = resolve_identity({}, payload())
        self.assertEqual(from_body.thread_id, ROOT)

        only_turn_metadata = resolve_identity(
            {"x-codex-turn-metadata": json.dumps({"thread_id": ROOT, "turn_id": TURN_1})},
            {"input": []},
        )
        self.assertEqual(only_turn_metadata.thread_id, ROOT)

    def test_mismatched_thread_sources_are_a_conflict(self):
        identity = resolve_identity(headers(), payload(thread_id="different"))
        self.assertTrue(identity.conflict)
        self.assertEqual(identity.thread_id, ROOT)

    def test_subagent_fixture_extracts_parent_and_source(self):
        child_headers = headers(
            thread_id=CHILD,
            turn_id="child-turn",
            **{
                "x-openai-subagent": "collab_spawn",
                "x-codex-parent-thread-id": ROOT,
                "x-codex-turn-metadata": json.dumps(
                    {
                        "thread_id": CHILD,
                        "turn_id": "child-turn",
                        "parent_thread_id": ROOT,
                        "parent_turn_id": TURN_1,
                        "agent_name": "/root/child",
                        "thread_source": "subagent",
                        "subagent_kind": "thread_spawn",
                    }
                ),
            },
        )
        identity = resolve_identity(child_headers, payload(CHILD, "child-turn"))
        self.assertTrue(identity.is_subagent)
        self.assertEqual(identity.parent_thread_id, ROOT)
        self.assertEqual(identity.agent_name, "/root/child")
        self.assertEqual(identity.subagent_kind, "thread_spawn")
        self.assertFalse(identity.conflict)

    def test_caller_edge_falls_back_to_prompt_cache_and_latest_user_message_id(self):
        routed = {
            "prompt_cache_key": ROOT,
            "input": [
                {
                    "id": "msg-user-turn-1",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Do work"}],
                }
            ],
        }

        identity = resolve_identity({}, routed)

        self.assertEqual(identity.thread_id, ROOT)
        self.assertEqual(identity.turn_id, "msg-user-turn-1")
        self.assertEqual(identity.sources, {"prompt_cache_key": ROOT})
        self.assertFalse(identity.is_subagent)

    def test_caller_edge_gives_new_task_a_child_identity_under_prompt_cache_root(self):
        routed = {
            "prompt_cache_key": ROOT,
            "input": [
                {
                    "id": "msg-user-turn-1",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Coordinate work"}],
                },
                {
                    "id": "msg-child-new-task",
                    "type": "agent_message",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Message Type: NEW_TASK\n"
                                "Task name: /root/docstring_policy\n"
                                "Sender: /root\nPayload:\nAdd a docstring."
                            ),
                        }
                    ],
                },
            ],
        }

        identity = resolve_identity({}, routed)

        self.assertEqual(identity.thread_id, f"{ROOT}:subagent:msg-child-new-task")
        self.assertEqual(identity.parent_thread_id, ROOT)
        self.assertEqual(identity.agent_name, "/root/docstring_policy")
        self.assertEqual(identity.subagent_kind, "thread_spawn")
        self.assertTrue(identity.is_subagent)


class RequestClassificationTests(unittest.TestCase):
    def test_explicit_parallel_subagent_task_is_coordination_only(self):
        hint = coordination_hint(
            "使用原生 collaboration spawn_agent 并行启动恰好三个只读子 agent，"
            "等待结果后由父线程汇总。"
        )

        self.assertEqual(hint, "fanout_coordinator")

    def test_ordinary_implementation_task_has_no_coordination_hint(self):
        self.assertIsNone(coordination_hint("修复 policy.py 的边界条件并运行测试"))

    def test_extracts_plaintext_new_task_payload(self):
        body = payload(
            thread_id=CHILD,
            input_items=[
                {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": "/root/docstring_policy",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Message Type: NEW_TASK\n"
                                "Task name: /root/docstring_policy\n"
                                "Sender: /root\n"
                                "Payload:\nAdd a docstring to inspect_request."
                            ),
                        }
                    ],
                }
            ],
        )

        delegation = extract_new_task_delegation(body)

        self.assertEqual(delegation.agent_name, "/root/docstring_policy")
        self.assertEqual(delegation.task, "Add a docstring to inspect_request.")
        self.assertEqual(delegation.source, "new_task_payload")

    def test_extracts_plaintext_spawn_message(self):
        body = payload(
            input_items=[
                {
                    "type": "function_call",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "arguments": json.dumps(
                        {
                            "task_name": "docstring_policy",
                            "fork_turns": "all",
                            "message": "Add a docstring to inspect_request.",
                        }
                    ),
                }
            ]
        )

        delegations = extract_spawn_delegations(body)

        self.assertEqual(len(delegations), 1)
        self.assertEqual(delegations[0].agent_name, "docstring_policy")
        self.assertEqual(delegations[0].task, "Add a docstring to inspect_request.")
        self.assertEqual(delegations[0].source, "spawn_message")

    def test_rejects_encrypted_spawn_and_new_task_payload(self):
        encrypted = "gAAAAABqrxBBLGpHo6ztlszHswqbch"
        body = payload(
            thread_id=CHILD,
            input_items=[
                {
                    "type": "function_call",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "arguments": json.dumps(
                        {"task_name": "delegation_probe", "fork_turns": "all", "message": encrypted}
                    ),
                },
                {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": "/root/delegation_probe",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Message Type: NEW_TASK\nTask name: /root/delegation_probe\n"
                                "Sender: /root\nPayload:\n"
                            ),
                        },
                        {"type": "encrypted_content", "encrypted_content": encrypted},
                    ],
                },
            ],
        )

        spawn = extract_spawn_delegations(body)[0]
        new_task = extract_new_task_delegation(body)

        self.assertIsNone(spawn.task)
        self.assertEqual(spawn.source, "spawn_message_encrypted")
        self.assertIsNone(new_task.task)
        self.assertEqual(new_task.source, "new_task_encrypted")

    def test_tool_output_is_a_tool_step(self):
        facts = inspect_request(
            payload(
                input_items=[
                    {"type": "message", "role": "user", "content": "Do work"},
                    {"type": "function_call_output", "call_id": "c1", "output": "ok"},
                ]
            )
        )
        self.assertEqual(facts.step_type, "tool_step")

    def test_custom_tool_output_is_a_tool_step(self):
        facts = inspect_request(
            payload(input_items=[{"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"}])
        )
        self.assertEqual(facts.step_type, "tool_step")

    def test_real_codex_compaction_fixture_is_detected_from_metadata(self):
        captured = compaction_fixture()
        identity = resolve_identity(captured["headers"], captured["body"])
        facts = inspect_request(captured["body"], identity.metadata)

        self.assertTrue(facts.is_compaction)
        self.assertEqual(facts.request_kind, "compaction")
        self.assertEqual(facts.compaction["strategy"], "memento")

    def test_explicit_turn_metadata_preempts_prompt_fallback(self):
        captured = compaction_fixture()
        metadata = {"request_kind": "turn"}

        facts = inspect_request(captured["body"], metadata)

        self.assertFalse(facts.is_compaction)

    def test_real_checkpoint_prefix_is_fallback_without_metadata(self):
        captured = compaction_fixture()
        self.assertTrue(inspect_request(captured["body"]).is_compaction)


class DecisionTimingTests(unittest.TestCase):
    def test_first_main_thread_asks_jev(self):
        identity = resolve_identity(headers(), payload())
        self.assertEqual(choose_event(identity, None, inspect_request(payload())), "first_request")

    def test_first_subagent_asks_jev_independently(self):
        identity = resolve_identity(
            {
                **headers(CHILD, "child-turn"),
                "x-openai-subagent": "collab_spawn",
                "x-codex-parent-thread-id": ROOT,
            },
            payload(CHILD, "child-turn"),
        )
        self.assertEqual(choose_event(identity, None, inspect_request(payload(CHILD, "child-turn"))), "subagent_first")

    def test_new_turn_asks_jev(self):
        identity = resolve_identity(headers(turn_id=TURN_2), payload(turn_id=TURN_2))
        state = ThreadSnapshot(ASTRA, "medium", 1000, TURN_1, False, None)
        self.assertEqual(choose_event(identity, state, inspect_request(payload(turn_id=TURN_2))), "new_user_turn")

    def test_pending_free_reroute_is_distinct(self):
        identity = resolve_identity(headers(turn_id=TURN_2), payload(turn_id=TURN_2))
        state = ThreadSnapshot(SOL, "high", 100_000, TURN_1, True, None)
        self.assertEqual(choose_event(identity, state, inspect_request(payload(turn_id=TURN_2))), "free_reroute")

    def test_compaction_preempts_other_events(self):
        captured = compaction_fixture()
        identity = resolve_identity(captured["headers"], captured["body"])
        facts = inspect_request(captured["body"], identity.metadata)
        self.assertEqual(choose_event(identity, None, facts), "compaction")

    def test_same_turn_tool_continuation_never_asks_jev(self):
        tool_payload = payload(input_items=[{"type": "function_call_output", "output": "ok"}])
        identity = resolve_identity(headers(), tool_payload)
        state = ThreadSnapshot(SOL, "high", 1000, TURN_1, False, None)
        self.assertEqual(choose_event(identity, state, inspect_request(tool_payload)), "tool_continuation")


class CandidateAndCostTests(unittest.TestCase):
    def setUp(self):
        self.prices = {
            LUNA: Price(cache_read=0.02, cache_write=0.25),
            SOL: Price(cache_read=0.40, cache_write=5.00),
            ASTRA: Price(cache_read=1.00, cache_write=12.50),
        }

    def test_low_confidence_falls_back_to_sol(self):
        candidate = candidate_from_jev(LUNA, "low", 0.49, confidence_gate=0.5)
        self.assertEqual((candidate.model, candidate.effort, candidate.gate), (SOL, "low", "low_confidence"))

    def test_luna_defaults_to_low_effort(self):
        candidate = candidate_from_jev(LUNA, "low", 0.9, confidence_gate=0.5)
        self.assertEqual((candidate.model, candidate.effort), (LUNA, "low"))

    def test_luna_effort_can_be_overridden_without_changing_service_tier(self):
        candidate = candidate_from_jev(
            LUNA,
            "high",
            0.9,
            confidence_gate=0.5,
            luna_effort="medium",
        )
        self.assertEqual((candidate.model, candidate.effort), (LUNA, "medium"))
        self.assertEqual(candidate.service_tier, "priority")

    def test_downgrade_over_context_threshold_is_rejected(self):
        gate = switch_gate(20_001, ASTRA, SOL, self.prices, 0.25, 20_000)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "downgrade_context_limit")

    def test_switch_cost_difference_over_budget_is_rejected(self):
        gate = switch_gate(100_000, LUNA, ASTRA, self.prices, 0.25, 200_000)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "switch_budget")
        self.assertAlmostEqual(gate.switch_cost - gate.stay_cost, 1.248)

    def test_decimal_budget_equality_is_allowed(self):
        prices = {
            SOL: Price(cache_read=0.7, cache_write=1.0),
            ASTRA: Price(cache_read=1.0, cache_write=1.0),
        }
        gate = switch_gate(1_000_000, SOL, ASTRA, prices, 0.3, 2_000_000)
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "affordable")

    def test_unknown_model_is_a_safe_hold(self):
        gate = switch_gate(1000, SOL, "unknown", self.prices, 0.25, 20_000)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "unknown_model")
        self.assertIsNone(gate.stay_cost)
        self.assertIsNone(gate.switch_cost)

    def test_missing_price_is_a_safe_hold(self):
        gate = switch_gate(1000, SOL, ASTRA, {SOL: self.prices[SOL]}, 0.25, 20_000)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "missing_price")
        self.assertIsNone(gate.switch_cost)

    def test_affordable_switch_is_allowed(self):
        gate = switch_gate(20_000, ASTRA, SOL, self.prices, 0.25, 20_000)
        self.assertTrue(gate.allowed)

    def test_same_model_effort_change_skips_cache_rebuild_cost(self):
        gate = switch_gate(100_000, SOL, SOL, self.prices, 0.25, 200_000)
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "same_model")
        self.assertAlmostEqual(gate.stay_cost, 0.04)
        self.assertAlmostEqual(gate.switch_cost, 0.04)

    def test_character_estimate_uses_configured_divisor(self):
        self.assertEqual(estimate_context_tokens(281, 2.8), 101)


if __name__ == "__main__":
    unittest.main()
