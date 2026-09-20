import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bench.run_long_parent import (
    generate_fixtures,
    install_fork_none_hook,
    long_parent_schedule,
    parent_context_before_children,
)


class LongParentScheduleTests(unittest.TestCase):
    def test_three_levels_three_modes_three_repeats_make_27_rotated_cells(self):
        levels = [{"id": name} for name in ("30k", "80k", "150k")]

        schedule = long_parent_schedule(levels, 3)

        self.assertEqual(len(schedule), 27)
        self.assertEqual({item["mode"] for item in schedule}, {"shadow_all", "live_all", "live_none"})
        self.assertTrue(all(
            sum(item["level"]["id"] == level and item["mode"] == mode for item in schedule) == 3
            for level in ("30k", "80k", "150k")
            for mode in ("shadow_all", "live_all", "live_none")
        ))
        first_modes = [schedule[index]["mode"] for index in range(0, len(schedule), 3)]
        self.assertEqual(first_modes[:3], ["shadow_all", "live_all", "live_none"])


class FixtureAndHookTests(unittest.TestCase):
    def test_fixture_generation_is_deterministic_and_exact_size(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            left_paths = generate_fixtures(Path(left), count=3, chars_per_file=2048)
            right_paths = generate_fixtures(Path(right), count=3, chars_per_file=2048)

            self.assertEqual([p.read_bytes() for p in left_paths], [p.read_bytes() for p in right_paths])
            self.assertTrue(all(path.stat().st_size >= 2048 for path in left_paths))
            self.assertEqual([path.name for path in left_paths], [
                "context-01.txt", "context-02.txt", "context-03.txt"
            ])

    def test_hook_changes_only_fork_turns_and_matches_spawn_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path, script_path = install_fork_none_hook(root)
            config = json.loads(hooks_path.read_text())
            entry = config["hooks"]["PreToolUse"][0]
            self.assertEqual(entry["matcher"], ".*spawn_agent")
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "collaborationspawn_agent",
                "tool_input": {
                    "task_name": "docstring_policy",
                    "message": "encrypted",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "medium",
                    "fork_turns": "all",
                },
            }
            completed = subprocess.run(
                [sys.executable, str(script_path)],
                input=json.dumps(payload),
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            output = json.loads(completed.stdout)
            updated = output["hookSpecificOutput"]["updatedInput"]
            self.assertEqual(updated["fork_turns"], "none")
            self.assertEqual(updated["model"], "gpt-5.6-sol")
            self.assertEqual(updated["reasoning_effort"], "medium")
            self.assertEqual(updated["task_name"], "docstring_policy")


class ContextMetricTests(unittest.TestCase):
    def test_parent_context_is_last_completed_parent_before_first_child_started(self):
        rows = [
            {
                "thread_id": "parent", "parent_thread_id": None,
                "response_completed": True, "response_finished_monotonic_ns": 100,
                "request_started_monotonic_ns": 10,
                "usage": {"input_tokens": 30000},
            },
            {
                "thread_id": "parent", "parent_thread_id": None,
                "response_completed": True, "response_finished_monotonic_ns": 300,
                "request_started_monotonic_ns": 200,
                "usage": {"input_tokens": 80000},
            },
            {
                "thread_id": "child", "parent_thread_id": "parent",
                "event": "subagent_first", "request_started_monotonic_ns": 250,
                "response_finished_monotonic_ns": 400,
                "usage": {"input_tokens": 90000},
            },
        ]

        self.assertEqual(parent_context_before_children(rows, "parent"), 30000)


if __name__ == "__main__":
    unittest.main()
