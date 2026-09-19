import json
import tempfile
import unittest
from pathlib import Path

from bench.run import (
    BACKTEST_PRICES,
    aggregate_results,
    apply_preparation,
    balanced_schedule,
    calculate_usage_cost,
    copy_workspace,
    evaluate_checks,
    is_infrastructure_failure,
    read_jsonl_since,
    temporary_codex_config,
)


class ScheduleTests(unittest.TestCase):
    def test_three_repeats_of_two_modes_are_balanced_and_alternating(self):
        tasks = [{"id": "m1"}, {"id": "r1"}]
        schedule = balanced_schedule(tasks, 3)

        self.assertEqual(len(schedule), 12)
        self.assertEqual(sum(item["mode"] == "shadow" for item in schedule), 6)
        self.assertEqual(sum(item["mode"] == "live" for item in schedule), 6)
        pairs = [schedule[index : index + 2] for index in range(0, len(schedule), 2)]
        self.assertTrue(all(pair[0]["task"]["id"] == pair[1]["task"]["id"] for pair in pairs))
        self.assertEqual([pair[0]["mode"] for pair in pairs], ["shadow", "live", "live", "shadow", "shadow", "live"])


class UsageTests(unittest.TestCase):
    def test_cost_uses_uncached_cached_and_output_prices(self):
        rows = [
            {
                "response_completed": True,
                "upstream_model": "gpt-6-astra",
                "usage": {"input_tokens": 100, "cached_tokens": 40, "output_tokens": 10},
            },
            {
                "response_completed": False,
                "upstream_model": None,
                "usage": {"input_tokens": None, "cached_tokens": None, "output_tokens": None},
            },
        ]
        result = calculate_usage_cost(rows, BACKTEST_PRICES)

        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["cached_tokens"], 40)
        self.assertEqual(result["output_tokens"], 10)
        self.assertEqual(result["missing_completed"], 1)
        self.assertTrue(result["cost_is_lower_bound"])
        self.assertAlmostEqual(result["cost_usd"], 0.00114)


class FixtureAndCheckTests(unittest.TestCase):
    def test_workspace_copy_excludes_local_codex_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            (source / ".codex").mkdir(parents=True)
            (source / ".codex" / "hooks.json").write_text("{}", encoding="utf-8")
            (source / "pkg").mkdir()
            (source / "pkg" / "safe.py").write_text("pass\n", encoding="utf-8")

            copy_workspace(source, destination)

            self.assertTrue((destination / "pkg" / "safe.py").exists())
            self.assertFalse((destination / ".codex").exists())

    def test_preparation_replaces_exact_expected_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sample.txt"
            path.write_text("good good", encoding="utf-8")
            apply_preparation(
                root,
                [{"type": "replace", "path": "sample.txt", "find": "good", "replace": "bad", "count": 1}],
            )
            self.assertEqual(path.read_text(encoding="utf-8"), "bad good")

    def test_checks_cover_content_docstring_command_output_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text('def f():\n    """docs"""\n    return 1\n', encoding="utf-8")
            (root / "report.txt").write_text("fixed", encoding="utf-8")
            (root / "analysis.md").write_text("threshold: 20,000 token", encoding="utf-8")
            (root / "python-lines.txt").write_text("3 pkg/a.py\n", encoding="utf-8")
            checks = [
                {"type": "file_contains_all", "path": "report.txt", "values": ["fixed"]},
                {"type": "function_docstring", "path": "pkg/a.py", "function": "f"},
                {"type": "command", "argv": ["python3", "-c", "raise SystemExit(0)"]},
                {"type": "output_regex", "pattern": "tests.*passed"},
                {"type": "file_regex_all", "path": "analysis.md", "patterns": [r"20[,]?000", "token"]},
                {"type": "python_line_manifest", "paths": ["pkg"]},
            ]
            passed, details = evaluate_checks(root, checks, "All tests passed")
            self.assertTrue(passed, details)


class PersistenceTests(unittest.TestCase):
    def test_jsonl_slice_starts_at_byte_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            path.write_text('{"old":1}\n', encoding="utf-8")
            offset = path.stat().st_size
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"new":2}\n')
            self.assertEqual(read_jsonl_since(path, offset), [{"new": 2}])

    def test_codex_config_is_restored_even_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            backup = Path(tmp) / "backup.toml"
            original = 'model = "gpt-5.6-sol"\n[projects."/tmp"]\ntrust_level = "trusted"\n'
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                with temporary_codex_config(path, backup, "http://127.0.0.1:4319/v1"):
                    changed = path.read_text(encoding="utf-8")
                    self.assertIn('model_provider = "codex-jev-router-bench"', changed)
                    raise RuntimeError("stop")
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(backup.read_text(encoding="utf-8"), original)


class FailureAndAggregateTests(unittest.TestCase):
    def test_only_infrastructure_failures_are_retryable(self):
        healthy = [{"event": "first_request", "gate": "apply", "status": 200}]
        self.assertFalse(is_infrastructure_failure(0, healthy))
        self.assertTrue(is_infrastructure_failure(1, healthy))
        self.assertTrue(is_infrastructure_failure(0, []))
        self.assertTrue(is_infrastructure_failure(0, [{"event": "first_request", "gate": "no_key", "status": 200}]))

    def test_aggregate_reports_group_savings_and_pass_rate(self):
        rows = []
        for category, task_id in (("mechanical", "m1"), ("reasoning", "r1")):
            for mode, costs in (("shadow", [2.0, 2.0, 2.0]), ("live", [1.0, 1.0, 1.0])):
                for repeat, cost in enumerate(costs, 1):
                    rows.append(
                        {
                            "task_id": task_id,
                            "category": category,
                            "mode": mode,
                            "repeat": repeat,
                            "cost_usd": cost,
                            "input_tokens": 100,
                            "cached_tokens": 40,
                            "output_tokens": 10,
                            "request_count": 2,
                            "completed_request_count": 2,
                            "missing_completed": 0,
                            "wall_ms": 1000 * repeat,
                            "jev_ms": 100 * repeat,
                            "jev_tier_choice": "gpt-6-astra" if task_id == "r1" else "gpt-5.6-luna",
                            "policy_model": "gpt-5.6-sol" if task_id == "r1" else "gpt-5.6-luna",
                            "expected_model": "gpt-6-astra" if task_id == "r1" else "gpt-5.6-luna",
                            "passed": repeat != 3,
                            "infrastructure_failure": False,
                        }
                    )
        summary = aggregate_results(rows)
        self.assertEqual(summary["groups"]["mechanical"]["savings_percent"], 50.0)
        self.assertEqual(summary["groups"]["reasoning"]["savings_percent"], 50.0)
        self.assertAlmostEqual(summary["modes"]["live"]["pass_rate"], 2 / 3)
        self.assertEqual(summary["modes"]["live"]["usage"]["input_tokens"], 600)
        self.assertEqual(summary["modes"]["live"]["requests"], 12)
        self.assertEqual(summary["modes"]["live"]["missing_completed"], 0)
        self.assertEqual(summary["modes"]["live"]["wall_ms"]["median"], 2000)
        self.assertEqual(summary["modes"]["live"]["jev_ms"]["median"], 200)
        self.assertEqual(summary["jev_mismatches"], [])
        self.assertEqual(summary["policy_mismatches"], ["r1"])


if __name__ == "__main__":
    unittest.main()
