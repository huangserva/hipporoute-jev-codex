import tempfile
import unittest
from pathlib import Path

from codex_jev_router.config import load_config
from codex_jev_router.policy import ASTRA, LUNA, SOL


class ConfigTests(unittest.TestCase):
    def test_defaults_match_phase_one_policy(self):
        config = load_config(None)
        self.assertEqual((config.listen_host, config.listen_port), ("127.0.0.1", 4319))
        self.assertEqual(config.upstream_mode, "direct")
        self.assertEqual(config.downgrade_max_context_tokens, 20_000)
        self.assertEqual(config.switch_budget_usd, 0.25)
        self.assertEqual(config.state_ttl_seconds, 86_400)
        self.assertEqual(config.state_gc_interval_seconds, 300)
        self.assertEqual(config.prices[ASTRA].cache_write, 12.50)
        self.assertEqual(config.prices[SOL].cache_read, 0.40)
        self.assertEqual(config.prices[LUNA].cache_write, 0.25)

    def test_toml_overrides_paths_and_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[server]
port = 9999
[upstream]
mode = "caller_edge"
caller_edge_url = "http://127.0.0.1:4202"
[paths]
state = "./state.json"
[routing]
state_ttl_seconds = 7200
state_gc_interval_seconds = 60
""",
                encoding="utf-8",
            )
            config = load_config(path)

        self.assertEqual(config.listen_port, 9999)
        self.assertEqual(config.upstream_mode, "caller_edge")
        self.assertEqual(config.state_path, Path("./state.json"))
        self.assertEqual(config.state_ttl_seconds, 7200)
        self.assertEqual(config.state_gc_interval_seconds, 60)


if __name__ == "__main__":
    unittest.main()
