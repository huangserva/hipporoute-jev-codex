import json
import tempfile
import unittest
from pathlib import Path

from codex_jev_router.jev import JevClient, load_key


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
    def test_environment_key_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev.env"
            path.write_text('TYPESAFE_API_KEY="file-key"\n', encoding="utf-8")
            value = load_key({"TYPESAFE_API_KEY": "environment-key"}, path)
        self.assertEqual(value, "environment-key")

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


if __name__ == "__main__":
    unittest.main()
