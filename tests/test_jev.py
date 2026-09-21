import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from hipporoute.jev import JevClient, load_key, questions_for


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class JevTests(unittest.TestCase):
    def test_tier_question_classifies_pure_fanout_coordination_as_luna(self):
        instructions = questions_for(is_subagent=False)["tier"]["instructions"]

        self.assertIn("delegate subtasks", instructions)
        self.assertIn("aggregate their results", instructions)
        self.assertIn("gpt-5.6-luna", instructions)

    def test_subagent_questions_explicitly_judge_delegated_task(self):
        questions = questions_for(is_subagent=True)

        self.assertIn("delegated subtask", questions["tier"]["instructions"])
        self.assertIn("Do not inherit", questions["tier"]["instructions"])
        self.assertIn("delegated subtask", questions["depth"]["instructions"])

    def test_environment_key_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev.env"
            path.write_text('TYPESAFE_API_KEY="file-key"\n', encoding="utf-8")
            value = load_key({"TYPESAFE_API_KEY": "environment-key"}, path)
        self.assertEqual(value, "environment-key")

    def test_configured_key_file_wins_over_default_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = root / "configured.env"
            default = root / ".jev.env"
            configured.write_text("TYPESAFE_API_KEY=configured-key\n", encoding="utf-8")
            default.write_text("TYPESAFE_API_KEY=default-key\n", encoding="utf-8")

            value = load_key({}, configured, default_env_path=default)

        self.assertEqual(value, "configured-key")

    def test_missing_configured_key_file_falls_back_to_default_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default = root / ".jev.env"
            default.write_text("TYPESAFE_API_KEY=default-key\n", encoding="utf-8")

            value = load_key({}, root / "missing.env", default_env_path=default)

        self.assertEqual(value, "default-key")

    def test_two_retries_use_exponential_backoff(self):
        attempts = []
        delays = []

        def opener(request, timeout):
            attempts.append((request, timeout))
            if len(attempts) < 3:
                raise TimeoutError("temporary")
            return FakeResponse(json.dumps({"answers": {}}).encode())

        client = JevClient(
            key="secret",
            url="https://example.invalid/systemone",
            model="jev-latest",
            timeout_seconds=4,
            retries=2,
            backoff_seconds=0.25,
            opener=opener,
            sleeper=delays.append,
        )
        result = client.ask({"task": "test"})

        self.assertEqual(result, {"answers": {}})
        self.assertEqual(len(attempts), 3)
        self.assertEqual([timeout for _, timeout in attempts], [4, 4, 4])
        self.assertEqual(delays, [0.25, 0.5])
        self.assertEqual(json.loads(attempts[-1][0].data)["model"], "jev-latest")

    def test_non_429_http_4xx_is_not_retried(self):
        attempts = []
        delays = []
        error = urllib.error.HTTPError(
            "https://example.invalid/systemone", 401, "unauthorized", {}, None
        )

        def opener(request, timeout):
            attempts.append((request, timeout))
            raise error

        client = JevClient(
            key="secret",
            url="https://example.invalid/systemone",
            model="jev-latest",
            retries=2,
            opener=opener,
            sleeper=delays.append,
        )

        with self.assertRaises(urllib.error.HTTPError):
            client.ask({"task": "test"})

        self.assertEqual(len(attempts), 1)
        self.assertEqual(delays, [])
        error.close()

    def test_429_retry_after_is_honored_but_capped(self):
        attempts = []
        delays = []
        headers = Message()
        headers["Retry-After"] = "30"
        error = urllib.error.HTTPError(
            "https://example.invalid/systemone", 429, "busy", headers, None
        )

        def opener(request, timeout):
            attempts.append((request, timeout))
            if len(attempts) == 1:
                raise error
            return FakeResponse(json.dumps({"answers": {}}).encode())

        client = JevClient(
            key="secret",
            url="https://example.invalid/systemone",
            model="jev-latest",
            retries=2,
            backoff_seconds=0.25,
            retry_after_cap_seconds=1.5,
            opener=opener,
            sleeper=delays.append,
        )

        result = client.ask({"task": "test"})

        self.assertEqual(result, {"answers": {}})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(delays, [1.5])
        error.close()


if __name__ == "__main__":
    unittest.main()
