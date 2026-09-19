import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_jev_router.config import load_config
from codex_jev_router.engine import RouterEngine
from codex_jev_router.policy import ASTRA, LUNA, SOL
from codex_jev_router.state import ThreadState, ThreadStateStore


ROOT = "thread-root"
TURN_1 = "turn-1"
TURN_2 = "turn-2"


def request(turn=TURN_1, thread=ROOT, items=None):
    body = {
        "input": items
        or [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Do work"}]}],
        "client_metadata": {"thread_id": thread, "turn_id": turn},
    }
    meta = {"thread_id": thread, "turn_id": turn, "thread_source": "user"}
    headers = {"thread-id": thread, "x-codex-turn-metadata": json.dumps(meta)}
    return headers, body


class FakeJev:
    def __init__(self, answers=None, error=None):
        self.answers = list(answers or [])
        self.error = error
        self.states = []

    def ask(self, state):
        self.states.append(state)
        if self.error:
            raise self.error
        return self.answers.pop(0)


class StateStoreTests(unittest.TestCase):
    def test_state_is_atomically_persisted_and_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            store = ThreadStateStore(path)
            store.put(
                ROOT,
                ThreadState(
                    model=SOL,
                    effort="high",
                    service_tier="default",
                    last_context_tokens=1234,
                    last_turn_id=TURN_1,
                    pending_free_reroute=True,
                    parent_thread_id=None,
                    decided_at="2026-09-19T00:00:00Z",
                ),
            )
            reloaded = ThreadStateStore(path).get(ROOT)

        self.assertEqual(reloaded.model, SOL)
        self.assertEqual(reloaded.last_context_tokens, 1234)
        self.assertTrue(reloaded.pending_free_reroute)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = replace(
            load_config(None),
            state_path=root / "threads.json",
            decision_log_path=root / "decisions.jsonl",
            off_path=root / "router.off",
            shadow_path=root / "router.shadow",
        )
        self.store = ThreadStateStore(self.config.state_path)

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, fake=None, key="key", key_counter=None, monotonic=None):
        def key_loader():
            if key_counter is not None:
                key_counter.append(1)
            return key

        kwargs = {}
        if monotonic is not None:
            kwargs["monotonic"] = monotonic
        return RouterEngine(
            self.config,
            self.store,
            key_loader=key_loader,
            jev_factory=(lambda _key: fake),
            **kwargs,
        )

    def test_successful_jev_decision_records_apply_gate_and_latency(self):
        fake = FakeJev(
            answers=[{"answers": {"tier": {"choice": LUNA, "confidence": 0.9}, "depth": {"choice": "low"}}}]
        )
        ticks = iter((10.0, 11.25))
        engine = self.engine(fake=fake, monotonic=lambda: next(ticks))
        headers, body = request()

        decision = engine.decide(headers, body, len(json.dumps(body)))

        self.assertEqual(decision.gate, "apply")
        self.assertEqual(decision.reason, "jev")
        self.assertEqual(decision.jev_ms, 1250)

    def test_no_key_fails_open_and_pins_astra_medium(self):
        engine = self.engine(fake=None, key="")
        headers, body = request()
        decision = engine.decide(headers, body, len(json.dumps(body)))

        self.assertEqual((decision.model, decision.effort, decision.gate), (ASTRA, "medium", "no_key"))
        self.assertEqual(decision.event, "first_request")
        self.assertFalse(decision.consulted_jev)
        self.assertEqual(self.store.get(ROOT).model, ASTRA)

    def test_same_turn_tool_continuation_reuses_without_loading_key(self):
        key_calls = []
        engine = self.engine(fake=None, key="", key_counter=key_calls)
        headers, body = request()
        engine.decide(headers, body, len(json.dumps(body)))
        tool_headers, tool_body = request(
            items=[{"type": "function_call_output", "call_id": "c1", "output": "ok"}]
        )
        decision = engine.decide(tool_headers, tool_body, len(json.dumps(tool_body)))

        self.assertEqual(decision.event, "tool_continuation")
        self.assertEqual(decision.gate, "sticky")
        self.assertEqual(len(key_calls), 1)

    def test_compaction_pins_sol_high_and_next_turn_reroutes_for_free(self):
        fake = FakeJev(
            answers=[{"answers": {"tier": {"choice": LUNA, "confidence": 0.9}, "depth": {"choice": "low"}}}]
        )
        engine = self.engine(fake=fake)
        checkpoint_headers, checkpoint = request(
            items=[
                {
                    "type": "message",
                    "role": "user",
                    "content": "You are creating a lossy continuation checkpoint now",
                }
            ]
        )
        compact = engine.decide(checkpoint_headers, checkpoint, len(json.dumps(checkpoint)))
        engine.record_usage(ROOT, 100_000)
        next_headers, next_body = request(turn=TURN_2)
        rerouted = engine.decide(next_headers, next_body, len(json.dumps(next_body)))

        self.assertEqual((compact.model, compact.effort, compact.gate), (SOL, "high", "apply"))
        self.assertEqual(compact.reason, "compaction")
        self.assertEqual(rerouted.event, "free_reroute")
        self.assertEqual((rerouted.model, rerouted.effort), (LUNA, "max"))
        self.assertEqual(len(fake.states), 1)
        self.assertFalse(self.store.get(ROOT).pending_free_reroute)

    def test_new_turn_rejects_large_context_downgrade(self):
        fake = FakeJev(
            answers=[
                {"answers": {"tier": {"choice": ASTRA, "confidence": 0.9}, "depth": {"choice": "high"}}},
                {"answers": {"tier": {"choice": SOL, "confidence": 0.9}, "depth": {"choice": "medium"}}},
            ]
        )
        engine = self.engine(fake=fake)
        first_headers, first_body = request()
        engine.decide(first_headers, first_body, len(json.dumps(first_body)))
        engine.record_usage(ROOT, 20_001)
        second_headers, second_body = request(turn=TURN_2)
        decision = engine.decide(second_headers, second_body, len(json.dumps(second_body)))

        self.assertEqual(decision.event, "new_user_turn")
        self.assertEqual(decision.model, ASTRA)
        self.assertEqual(decision.gate, "hold")
        self.assertEqual(decision.reason, "downgrade_context_limit")
        self.assertEqual(self.store.get(ROOT).last_turn_id, TURN_2)

    def test_jev_error_fails_open(self):
        engine = self.engine(fake=FakeJev(error=TimeoutError("late")))
        headers, body = request()
        decision = engine.decide(headers, body, len(json.dumps(body)))
        self.assertEqual((decision.model, decision.effort), (ASTRA, "medium"))
        self.assertEqual(decision.gate, "jev_error:TimeoutError")

    def test_identity_conflict_fails_open_without_state(self):
        engine = self.engine(fake=None, key="")
        headers, body = request()
        body["client_metadata"]["thread_id"] = "different"
        decision = engine.decide(headers, body, len(json.dumps(body)))
        self.assertEqual(decision.gate, "identity_conflict")
        self.assertEqual(decision.model, ASTRA)
        self.assertIsNone(self.store.get(ROOT))

    def test_shadow_records_would_but_serves_astra(self):
        self.config.shadow_path.touch()
        fake = FakeJev(
            answers=[{"answers": {"tier": {"choice": LUNA, "confidence": 0.9}, "depth": {"choice": "low"}}}]
        )
        engine = self.engine(fake=fake)
        headers, body = request()
        decision = engine.decide(headers, body, len(json.dumps(body)))
        self.assertEqual((decision.model, decision.effort, decision.gate), (ASTRA, "medium", "apply"))
        self.assertTrue(decision.shadow)
        self.assertEqual(decision.would["model"], LUNA)
        self.assertEqual(self.store.get(ROOT).model, LUNA)

    def test_kill_switch_skips_key_and_jev(self):
        self.config.off_path.touch()
        key_calls = []
        fake = FakeJev(error=AssertionError("must not be called"))
        engine = self.engine(fake=fake, key_counter=key_calls)
        headers, body = request()
        decision = engine.decide(headers, body, len(json.dumps(body)))
        self.assertEqual((decision.model, decision.effort, decision.gate), (ASTRA, "medium", "off"))
        self.assertEqual(key_calls, [])


if __name__ == "__main__":
    unittest.main()
