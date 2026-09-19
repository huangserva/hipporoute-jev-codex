import json
import unittest

from codex_jev_router.policy import (
    ASTRA,
    LUNA,
    SOL,
    Price,
    ThreadSnapshot,
    candidate_from_jev,
    choose_event,
    estimate_context_tokens,
    inspect_request,
    resolve_identity,
    switch_gate,
)


ROOT = "01a0-root"
CHILD = "01a0-child"
TURN_1 = "01a0-turn-1"
TURN_2 = "01a0-turn-2"


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
                        "thread_source": "subagent",
                        "subagent_kind": "thread_spawn",
                    }
                ),
            },
        )
        identity = resolve_identity(child_headers, payload(CHILD, "child-turn"))
        self.assertTrue(identity.is_subagent)
        self.assertEqual(identity.parent_thread_id, ROOT)
        self.assertFalse(identity.conflict)


class RequestClassificationTests(unittest.TestCase):
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

    def test_checkpoint_prefix_is_detected(self):
        facts = inspect_request(
            payload(
                input_items=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": "  You are creating a lossy continuation checkpoint for this thread",
                    }
                ]
            )
        )
        self.assertTrue(facts.is_compaction)


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
        checkpoint = payload(
            input_items=[{"type": "message", "role": "user", "content": "You are creating a lossy continuation checkpoint now"}]
        )
        identity = resolve_identity(headers(), checkpoint)
        self.assertEqual(choose_event(identity, None, inspect_request(checkpoint)), "compaction")

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

    def test_luna_is_always_max_effort(self):
        candidate = candidate_from_jev(LUNA, "low", 0.9, confidence_gate=0.5)
        self.assertEqual((candidate.model, candidate.effort), (LUNA, "max"))

    def test_downgrade_over_context_threshold_is_rejected(self):
        gate = switch_gate(20_001, ASTRA, SOL, self.prices, 0.25, 20_000)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "downgrade_context_limit")

    def test_switch_cost_difference_over_budget_is_rejected(self):
        gate = switch_gate(100_000, LUNA, ASTRA, self.prices, 0.25, 200_000)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "switch_budget")
        self.assertAlmostEqual(gate.switch_cost - gate.stay_cost, 1.248)

    def test_affordable_switch_is_allowed(self):
        gate = switch_gate(20_000, ASTRA, SOL, self.prices, 0.25, 20_000)
        self.assertTrue(gate.allowed)

    def test_character_estimate_uses_configured_divisor(self):
        self.assertEqual(estimate_context_tokens(281, 2.8), 101)


if __name__ == "__main__":
    unittest.main()
