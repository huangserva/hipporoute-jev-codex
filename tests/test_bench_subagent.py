import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bench.run import BACKTEST_PRICES, balanced_schedule
from bench.run_subagent import (
    aggregate_subagent_results,
    codex_thread_id,
    filter_thread_tree,
    thread_metrics,
    selected_schedule,
)


class SubagentScheduleTests(unittest.TestCase):
    def test_live_only_schedule_runs_each_repeat_once(self):
        tasks = [{"id": "s2"}]

        schedule = selected_schedule(tasks, 6, ("live",))

        self.assertEqual(len(schedule), 6)
        self.assertEqual([item["repeat"] for item in schedule], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(item["mode"] == "live" for item in schedule))

    def test_read_only_fanout_uses_sandbox_safe_test_modules(self):
        root = Path(__file__).resolve().parents[1]
        tasks = json.loads((root / "bench" / "tasks-subagent.json").read_text(encoding="utf-8"))
        task = next(item for item in tasks if item["id"] == "s2_parallel_tests")

        self.assertNotIn("test_server", task["prompt"])
        self.assertIn("test_jev", task["prompt"])
        self.assertIn("test_config", task["prompt"])

    def test_script_entrypoint_can_import_bench_package(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "bench/run_subagent.py", "--help"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_four_tasks_two_modes_three_repeats_make_24_sessions(self):
        tasks = [{"id": f"s{index}"} for index in range(1, 5)]

        schedule = balanced_schedule(tasks, 3)

        self.assertEqual(len(schedule), 24)
        self.assertEqual(sum(item["mode"] == "shadow" for item in schedule), 12)
        self.assertEqual(sum(item["mode"] == "live" for item in schedule), 12)


class ThreadMetricTests(unittest.TestCase):
    def test_reads_root_thread_id_from_codex_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.jsonl"
            path.write_text(
                '{"type":"turn.started"}\n'
                '{"type":"thread.started","thread_id":"root-thread"}\n',
                encoding="utf-8",
            )

            self.assertEqual(codex_thread_id(path), "root-thread")

    def test_filters_unrelated_concurrent_sessions_but_keeps_nested_descendants(self):
        rows = [
            {"thread_id": "other", "parent_thread_id": None},
            {"thread_id": "root", "parent_thread_id": None},
            {"thread_id": "child", "parent_thread_id": "root"},
            {"thread_id": "grandchild", "parent_thread_id": "child"},
            {"thread_id": "other-child", "parent_thread_id": "other"},
            {"thread_id": "root", "parent_thread_id": None},
        ]

        filtered = filter_thread_tree(rows, "root")

        self.assertEqual(
            [row["thread_id"] for row in filtered],
            ["root", "child", "grandchild", "root"],
        )

    def test_groups_parent_and_children_with_independent_costs(self):
        rows = [
            {
                "thread_id": "parent",
                "parent_thread_id": None,
                "event": "first_request",
                "model": "gpt-6-astra",
                "would": {"model": "gpt-6-astra"},
                "upstream_model": "gpt-6-astra",
                "response_completed": True,
                "usage": {"input_tokens": 100, "cached_tokens": 40, "output_tokens": 10},
                "jev": {"tier": {"choice": "gpt-6-astra", "confidence": 0.8}},
                "jev_ms": 700,
            },
            {
                "thread_id": "child",
                "parent_thread_id": "parent",
                "event": "subagent_first",
                "model": "gpt-5.6-luna",
                "would": None,
                "agent_name": "/root/fix_typo",
                "delegation_source": "agent_name_fallback",
                "upstream_model": "gpt-5.6-luna",
                "response_completed": True,
                "usage": {"input_tokens": 200, "cached_tokens": 100, "output_tokens": 20},
                "jev": {"tier": {"choice": "gpt-5.6-luna", "confidence": 0.9}},
                "jev_ms": 800,
            },
        ]

        metrics = thread_metrics(rows, "live", BACKTEST_PRICES)

        self.assertEqual(len(metrics), 2)
        parent = next(item for item in metrics if item["role"] == "parent")
        child = next(item for item in metrics if item["role"] == "subagent")
        self.assertEqual(parent["policy_model"], "gpt-6-astra")
        self.assertEqual(parent["upstream_model_counts"], {"gpt-6-astra": 1})
        self.assertEqual(child["agent_name"], "/root/fix_typo")
        self.assertEqual(child["policy_model"], "gpt-5.6-luna")
        self.assertEqual(child["jev_tier_confidence"], 0.9)
        self.assertGreater(parent["cost_usd"], child["cost_usd"])


class SubagentAggregateTests(unittest.TestCase):
    def test_aggregates_parent_and_child_costs_by_mode(self):
        results = []
        for mode, parent_cost, child_cost in (
            ("shadow", 2.0, 3.0),
            ("live", 2.0, 1.0),
        ):
            for repeat in range(1, 4):
                results.append(
                    {
                        "task_id": "s1",
                        "mode": mode,
                        "repeat": repeat,
                        "passed": True,
                        "infrastructure_failure": False,
                        "wall_ms": repeat * 1000,
                        "threads": [
                            {"role": "parent", "cost_usd": parent_cost, "missing_completed": 0},
                            {"role": "subagent", "cost_usd": child_cost, "missing_completed": 0},
                        ],
                    }
                )

        summary = aggregate_subagent_results(results)

        self.assertEqual(summary["modes"]["shadow"]["parent_cost_usd"], 6.0)
        self.assertEqual(summary["modes"]["shadow"]["subagent_cost_usd"], 9.0)
        self.assertEqual(summary["modes"]["live"]["subagent_cost_usd"], 3.0)
        self.assertAlmostEqual(summary["subagent_savings_percent"], 66.66666666666667)
        self.assertEqual(summary["modes"]["live"]["pass_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
import json
