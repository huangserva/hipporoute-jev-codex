import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from codex_jev_router.config import load_config
from codex_jev_router.engine import RouterEngine
from codex_jev_router.policy import ASTRA, LUNA, SOL, SpawnDelegation
from codex_jev_router.state import ThreadState, ThreadStateStore


ROOT = "thread-root"
TURN_1 = "turn-1"
TURN_2 = "turn-2"
FIXTURES = Path(__file__).with_name("fixtures")


def compaction_request():
    with open(FIXTURES / "codex-0.155.1-compaction-request.json", encoding="utf-8") as handle:
        captured = json.load(handle)
    return captured["headers"], captured["body"]


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

    def ask(self, state, **kwargs):
        self.states.append(state)
        if self.error:
            raise self.error
        return self.answers.pop(0)


class StateStoreTests(unittest.TestCase):
    def test_state_is_atomically_persisted_and_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            store = ThreadStateStore(path, flush_interval_seconds=60)
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
            self.assertFalse(path.exists())
            store.flush()
            reloaded_store = ThreadStateStore(path, flush_interval_seconds=60)
            reloaded = reloaded_store.get(ROOT)
            store.close()
            reloaded_store.close()

        self.assertEqual(reloaded.model, SOL)
        self.assertEqual(reloaded.last_context_tokens, 1234)
        self.assertTrue(reloaded.pending_free_reroute)

    def test_multiple_updates_are_merged_into_one_explicit_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThreadStateStore(Path(tmp) / "threads.json", flush_interval_seconds=60)
            state = ThreadState(
                model=SOL,
                effort="high",
                service_tier="default",
                last_context_tokens=1,
                last_turn_id=TURN_1,
                pending_free_reroute=False,
                parent_thread_id=None,
                decided_at="2026-09-19T00:00:00Z",
            )
            with mock.patch.object(store, "_write_snapshot", wraps=store._write_snapshot) as write:
                store.put(ROOT, state)
                store.update_usage(ROOT, 2)
                store.touch(ROOT, "2026-09-19T00:00:01Z")
                self.assertEqual(write.call_count, 0)

                store.flush()

                self.assertEqual(write.call_count, 1)
            store.close()

    def test_background_worker_flushes_dirty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            store = ThreadStateStore(path, flush_interval_seconds=0.01)
            store.put(
                ROOT,
                ThreadState(
                    model=SOL,
                    effort="high",
                    service_tier="default",
                    last_context_tokens=1,
                    last_turn_id=TURN_1,
                    pending_free_reroute=False,
                    parent_thread_id=None,
                    decided_at="2026-09-19T00:00:00Z",
                ),
            )

            deadline = time.monotonic() + 1
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)

            store.close()
            self.assertTrue(path.exists())

    def test_close_force_flushes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "threads.json"
            store = ThreadStateStore(path, flush_interval_seconds=60)
            store.put(
                ROOT,
                ThreadState(
                    model=SOL,
                    effort="high",
                    service_tier="default",
                    last_context_tokens=7,
                    last_turn_id=TURN_1,
                    pending_free_reroute=False,
                    parent_thread_id=None,
                    decided_at="2026-09-19T00:00:00Z",
                ),
            )

            store.close()
            store.close()

            payload = json.loads(path.read_text())
            self.assertEqual(payload["threads"][ROOT]["last_context_tokens"], 7)

    def test_gc_removes_only_parseable_expired_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThreadStateStore(Path(tmp) / "threads.json", flush_interval_seconds=60)
            base = dict(
                model=SOL,
                effort="high",
                service_tier="default",
                last_context_tokens=1,
                last_turn_id=TURN_1,
                pending_free_reroute=False,
                parent_thread_id=None,
            )
            store.put(
                "expired",
                ThreadState(
                    **base,
                    decided_at="2026-09-18T00:00:00Z",
                    last_active_at="2026-09-18T00:00:00Z",
                ),
            )
            store.put(
                "active",
                ThreadState(
                    **base,
                    decided_at="2026-09-19T18:00:00Z",
                    last_active_at="2026-09-19T18:00:00Z",
                ),
            )
            store.put(
                "invalid",
                ThreadState(**base, decided_at="not-a-time", last_active_at="also-invalid"),
            )

            removed = store.gc_expired(
                datetime(2026, 9, 20, tzinfo=timezone.utc),
                ttl_seconds=86_400,
            )

            self.assertEqual(removed, ["expired"])
            self.assertIsNone(store.get("expired"))
            self.assertIsNotNone(store.get("active"))
            self.assertIsNotNone(store.get("invalid"))
            store.close()


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
        self.store.close()
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
        ticks = iter((0.0, 10.0, 11.25))
        engine = self.engine(fake=fake, monotonic=lambda: next(ticks))
        headers, body = request()

        decision = engine.decide(headers, body, len(json.dumps(body)))

        self.assertEqual(decision.gate, "apply")
        self.assertEqual(decision.reason, "jev")
        self.assertEqual(decision.jev_ms, 1250)
        self.assertEqual(
            decision.jev,
            {
                "tier": {"choice": LUNA, "confidence": 0.9},
                "depth": {"choice": "low", "confidence": None},
            },
        )

    def test_engine_applies_configured_luna_effort(self):
        self.config = replace(self.config, luna_effort="low")
        fake = FakeJev(
            answers=[
                {
                    "answers": {
                        "tier": {"choice": LUNA, "confidence": 0.9},
                        "depth": {"choice": "high"},
                    }
                }
            ]
        )
        engine = self.engine(fake=fake)
        headers, body = request()

        decision = engine.decide(headers, body, len(json.dumps(body)))

        self.assertEqual((decision.model, decision.effort), (LUNA, "low"))
        self.assertEqual(decision.service_tier, "priority")

    def test_parent_fanout_task_sends_coordination_hint_to_jev(self):
        fake = FakeJev(
            answers=[
                {
                    "answers": {
                        "tier": {"choice": LUNA, "confidence": 0.9},
                        "depth": {"choice": "low"},
                    }
                }
            ]
        )
        engine = self.engine(fake=fake)
        headers, body = request(
            items=[
                {
                    "type": "message",
                    "role": "user",
                    "content": (
                        "使用原生 collaboration spawn_agent 并行启动恰好三个只读子 agent，"
                        "等待结果后由父线程汇总。"
                    ),
                }
            ]
        )

        engine.decide(headers, body, len(json.dumps(body)))

        self.assertEqual(fake.states[0]["coordination_hint"], "fanout_coordinator")
        self.assertEqual(fake.states[0]["signals"]["coordination_hint"], "fanout_coordinator")

    def test_no_key_fails_open_and_pins_astra_medium(self):
        engine = self.engine(fake=None, key="")
        headers, body = request()
        decision = engine.decide(headers, body, len(json.dumps(body)))

        self.assertEqual((decision.model, decision.effort, decision.gate), (ASTRA, "medium", "no_key"))
        self.assertEqual(decision.event, "first_request")
        self.assertFalse(decision.consulted_jev)
        self.assertEqual(self.store.get(ROOT).model, ASTRA)

    def test_key_loader_exception_fails_open_without_calling_jev(self):
        fake = FakeJev(error=AssertionError("must not be called"))

        def broken_key_loader():
            raise OSError("unreadable key file")

        engine = RouterEngine(
            self.config,
            self.store,
            key_loader=broken_key_loader,
            jev_factory=lambda _key: fake,
        )
        headers, body = request()

        decision = engine.decide(headers, body, len(json.dumps(body)))

        self.assertEqual((decision.model, decision.effort), (ASTRA, "medium"))
        self.assertEqual(decision.gate, "jev_error:OSError")
        self.assertFalse(decision.consulted_jev)
        self.assertEqual(fake.states, [])

    def test_gc_removes_expired_thread_lock(self):
        now = [datetime(2026, 9, 20, tzinfo=timezone.utc)]
        clock = [100.0]
        config = replace(
            self.config,
            state_ttl_seconds=1,
            state_gc_interval_seconds=0,
        )
        engine = RouterEngine(
            config,
            self.store,
            key_loader=lambda: "",
            now=lambda: now[0],
            monotonic=lambda: clock[0],
        )
        headers, body = request(thread="expired-lock")
        engine.decide(headers, body, len(json.dumps(body)))
        self.assertIn("expired-lock", engine._thread_locks)

        now[0] = datetime(2026, 9, 21, tzinfo=timezone.utc)
        clock[0] += 2
        other_headers, other_body = request(thread="active-lock")
        engine.decide(other_headers, other_body, len(json.dumps(other_body)))

        self.assertNotIn("expired-lock", engine._thread_locks)
        self.assertIn("active-lock", engine._thread_locks)

    def test_subagent_uses_delegation_context_and_does_not_inherit_parent_model(self):
        parent_headers, parent_body = request()
        parent = self.engine(
            fake=FakeJev(
                answers=[
                    {
                        "answers": {
                            "tier": {"choice": ASTRA, "confidence": 0.9},
                            "depth": {"choice": "high"},
                        }
                    }
                ]
            )
        )
        parent.decide(parent_headers, parent_body, len(json.dumps(parent_body)))
        fake = FakeJev(
            answers=[
                {
                    "answers": {
                        "tier": {"choice": LUNA, "confidence": 0.95},
                        "depth": {"choice": "low"},
                    }
                }
            ]
        )
        engine = self.engine(fake=fake)
        child = "thread-child"
        metadata = {
            "thread_id": child,
            "turn_id": "child-turn",
            "parent_thread_id": ROOT,
            "agent_name": "/root/docstring_policy",
            "subagent_kind": "thread_spawn",
            "thread_source": "subagent",
        }
        child_headers = {
            "thread-id": child,
            "x-codex-parent-thread-id": ROOT,
            "x-openai-subagent": "collab_spawn",
            "x-codex-turn-metadata": json.dumps(metadata),
        }
        child_body = {
            "client_metadata": {"thread_id": child, "turn_id": "child-turn"},
            "input": [
                {"type": "message", "role": "user", "content": "Coordinate a hard migration."},
                {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": "/root/docstring_policy",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Message Type: NEW_TASK\nTask name: /root/docstring_policy\n"
                                "Sender: /root\nPayload:\n"
                            ),
                        },
                        {"type": "encrypted_content", "encrypted_content": "gAAAAAopaque"},
                    ],
                },
            ],
        }

        decision = engine.decide(child_headers, child_body, len(json.dumps(child_body)))

        self.assertEqual(decision.event, "subagent_first")
        self.assertEqual(decision.model, LUNA)
        self.assertEqual(decision.parent_tier, ASTRA)
        self.assertEqual(decision.agent_name, "/root/docstring_policy")
        self.assertEqual(decision.subagent_kind, "thread_spawn")
        self.assertEqual(decision.delegation_source, "agent_name_fallback")
        self.assertIn("docstring_policy", decision.routing_task)
        self.assertIn("Parent task context", decision.routing_task)
        self.assertTrue(fake.states[0]["delegation"]["is_subagent"])
        self.assertEqual(fake.states[0]["delegation"]["parent_tier"], ASTRA)
        state = self.store.get(child)
        self.assertEqual(state.model, LUNA)
        self.assertEqual(state.parent_tier, ASTRA)
        self.assertEqual(state.agent_name, "/root/docstring_policy")
        self.assertEqual(state.subagent_kind, "thread_spawn")

    def test_subagent_prefers_cached_plaintext_spawn_message(self):
        parent_headers, parent_body = request()
        parent = self.engine(
            fake=FakeJev(
                answers=[
                    {
                        "answers": {
                            "tier": {"choice": ASTRA, "confidence": 0.9},
                            "depth": {"choice": "high"},
                        }
                    }
                ]
            )
        )
        parent.decide(parent_headers, parent_body, len(json.dumps(parent_body)))
        fake = FakeJev(
            answers=[
                {
                    "answers": {
                        "tier": {"choice": LUNA, "confidence": 0.95},
                        "depth": {"choice": "low"},
                    }
                }
            ]
        )
        engine = self.engine(fake=fake)
        engine.record_delegations(
            ROOT,
            [SpawnDelegation("docstring_policy", "Add one precise docstring.", "spawn_message")],
        )
        child = "thread-child-cached"
        metadata = {
            "thread_id": child,
            "turn_id": "child-turn",
            "parent_thread_id": ROOT,
            "agent_name": "/root/docstring_policy",
            "subagent_kind": "thread_spawn",
            "thread_source": "subagent",
        }
        child_headers = {
            "thread-id": child,
            "x-codex-parent-thread-id": ROOT,
            "x-openai-subagent": "collab_spawn",
            "x-codex-turn-metadata": json.dumps(metadata),
        }
        child_body = {
            "client_metadata": {"thread_id": child, "turn_id": "child-turn"},
            "input": [{"type": "message", "role": "user", "content": "Hard parent task"}],
        }

        decision = engine.decide(child_headers, child_body, len(json.dumps(child_body)))

        self.assertEqual(decision.routing_task, "Add one precise docstring.")
        self.assertEqual(decision.delegation_source, "spawn_message")
        self.assertEqual(fake.states[0]["task"], "Add one precise docstring.")

    def test_concurrent_first_requests_for_same_thread_consult_jev_once(self):
        started = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()

        class BlockingJev:
            def __init__(self):
                self.calls = 0
                self.lock = threading.Lock()

            def ask(self, _state, **_kwargs):
                with self.lock:
                    self.calls += 1
                    call = self.calls
                if call == 1:
                    started.set()
                    release.wait(2)
                else:
                    second_entered.set()
                return {
                    "answers": {
                        "tier": {"choice": LUNA, "confidence": 0.9},
                        "depth": {"choice": "low"},
                    }
                }

        fake = BlockingJev()
        engine = self.engine(fake=fake)
        headers, body = request()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.decide, headers, body, len(json.dumps(body)))
            self.assertTrue(started.wait(1))
            second = pool.submit(engine.decide, headers, body, len(json.dumps(body)))
            second_entered.wait(0.2)
            release.set()
            decisions = [first.result(timeout=2), second.result(timeout=2)]

        self.assertEqual(fake.calls, 1)
        self.assertEqual(sorted(decision.event for decision in decisions), ["first_request", "reuse"])

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
        checkpoint_headers, checkpoint = compaction_request()
        compact = engine.decide(checkpoint_headers, checkpoint, len(json.dumps(checkpoint)))
        engine.record_usage("thread-redacted", 100_000)
        next_headers, next_body = request(turn=TURN_2, thread="thread-redacted")
        rerouted = engine.decide(next_headers, next_body, len(json.dumps(next_body)))

        self.assertEqual((compact.model, compact.effort, compact.gate), (SOL, "high", "apply"))
        self.assertEqual(compact.reason, "compaction")
        self.assertEqual(rerouted.event, "free_reroute")
        self.assertEqual((rerouted.model, rerouted.effort), (LUNA, "low"))
        self.assertEqual(len(fake.states), 1)
        self.assertFalse(self.store.get("thread-redacted").pending_free_reroute)

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

    def test_consecutive_jev_failures_open_circuit_until_cooldown(self):
        clock = [100.0]
        self.config = replace(
            self.config,
            jev_circuit_failure_threshold=2,
            jev_circuit_open_seconds=60.0,
        )
        key_calls = []
        fake = FakeJev(error=TimeoutError("late"))
        engine = self.engine(fake=fake, key_counter=key_calls, monotonic=lambda: clock[0])

        first_headers, first_body = request(thread="circuit-1")
        second_headers, second_body = request(thread="circuit-2")
        third_headers, third_body = request(thread="circuit-3")
        first = engine.decide(first_headers, first_body, len(json.dumps(first_body)))
        second = engine.decide(second_headers, second_body, len(json.dumps(second_body)))
        third = engine.decide(third_headers, third_body, len(json.dumps(third_body)))

        self.assertEqual(first.gate, "jev_error:TimeoutError")
        self.assertEqual(second.gate, "jev_error:TimeoutError")
        self.assertEqual(third.gate, "jev_circuit_open")
        self.assertFalse(third.consulted_jev)
        self.assertEqual(len(fake.states), 2)
        self.assertEqual(len(key_calls), 2)

        clock[0] += 61
        fourth_headers, fourth_body = request(thread="circuit-4")
        fourth = engine.decide(fourth_headers, fourth_body, len(json.dumps(fourth_body)))
        self.assertEqual(fourth.gate, "jev_error:TimeoutError")
        self.assertEqual(len(fake.states), 3)

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
