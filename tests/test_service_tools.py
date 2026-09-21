from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ConfigureCodexTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script("configure_codex", "configure_codex.py")

    def test_enable_backs_up_and_sets_provider_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            state = root / "enable-state.json"
            original = 'model = "gpt-6-astra"\nmodel_provider = "openai"\n\n[projects."/tmp"]\ntrust_level = "trusted"\n'
            config.write_text(original, encoding="utf-8")

            first = self.module.enable_config(
                config, state, backup_dir=root, timestamp="20260921-010203"
            )
            second = self.module.enable_config(
                config, state, backup_dir=root, timestamp="20260921-999999"
            )

            enabled = config.read_text(encoding="utf-8")
            self.assertEqual(enabled.count('model_provider = "codex-jev-router"'), 1)
            self.assertEqual(enabled.count("[model_providers.codex-jev-router]"), 1)
            self.assertIn('base_url = "http://127.0.0.1:4319/v1"', enabled)
            self.assertIn('requires_openai_auth = true', enabled)
            self.assertEqual(first["backup_path"], second["backup_path"])
            self.assertEqual(len(list(root.glob("config.toml.backup-codex-jev-router-*"))), 1)
            self.assertEqual(Path(first["backup_path"]).read_text(encoding="utf-8"), original)

    def test_restore_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            state = root / "enable-state.json"
            original = 'model_provider = "openai"\n'
            config.write_text(original, encoding="utf-8")
            self.module.enable_config(config, state, backup_dir=root, timestamp="20260921-010203")

            first = self.module.restore_config(config, state)
            second = self.module.restore_config(config, state)

            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertTrue(first["restored"])
            self.assertFalse(second["restored"])

    def test_restore_refuses_to_overwrite_manual_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            state = root / "enable-state.json"
            config.write_text('model_provider = "openai"\n', encoding="utf-8")
            result = self.module.enable_config(
                config, state, backup_dir=root, timestamp="20260921-010203"
            )
            config.write_text(config.read_text(encoding="utf-8") + "# manual\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "changed since enable"):
                self.module.restore_config(config, state)

            self.assertTrue(state.exists())
            self.assertEqual(json.loads(state.read_text())["backup_path"], result["backup_path"])


class ServiceScriptTests(unittest.TestCase):
    def _script(self, name: str) -> str:
        return (ROOT / "scripts" / name).read_text(encoding="utf-8")

    def test_install_service_contains_required_launchd_contract(self):
        script = self._script("install-service.sh")
        for required in (
            "com.jev.codex-jev-router",
            "<key>RunAtLoad</key><true/>",
            "<key>KeepAlive</key><true/>",
            "codex-jev-router.out.log",
            "codex-jev-router.err.log",
            "HTTPS_PROXY",
            "http://127.0.0.1:7897",
            "NO_PROXY",
            '"jev_key":true',
        ):
            self.assertIn(required, script)
        self.assertNotIn("TYPESAFE_API_KEY", script)

    def test_service_uses_caller_edge_config_without_embedding_secret(self):
        config = (ROOT / "config.service.toml").read_text(encoding="utf-8")
        installer = self._script("install-service.sh")
        self.assertIn('mode = "caller_edge"', config)
        self.assertIn('caller_edge_url = "http://127.0.0.1:4202"', config)
        self.assertIn('caller_secret_path = "~/.codex/codex-router/caller-secret"', config)
        self.assertNotIn("/_codex-router/", config)
        self.assertIn("--config", installer)
        self.assertIn("config.service.toml", installer)

    def test_install_service_persists_gui_loopback_proxy_bypass(self):
        script = self._script("install-service.sh")
        self.assertIn("com.jev.codex-router-loopback-env", script)
        self.assertIn("launchctl", script)
        self.assertIn("setenv", script)
        self.assertIn("localhost,127.0.0.1,::1", script)

    def test_enable_checks_both_services_before_publishing_picker_entry(self):
        script = self._script("enable.sh")
        router_health = script.index("check_codex_router_health")
        jev_health = script.index("check_jev_router_health")
        shadow = script.index('touch "$SHADOW_PATH"')
        provider = script.index("providers generic")
        picker = script.index("picker set jev/auto show")
        self.assertLess(router_health, shadow)
        self.assertLess(jev_health, shadow)
        self.assertLess(shadow, provider)
        self.assertLess(provider, picker)
        self.assertIn("install-service.sh", script)
        self.assertNotIn("configure_codex.py", script)

    def test_disable_hides_picker_and_disables_provider_without_editing_codex_config(self):
        script = self._script("disable.sh")
        self.assertIn("picker set jev/auto hide", script)
        self.assertIn("providers generic disable jev", script)
        self.assertNotIn("configure_codex.py", script)
        self.assertNotIn('rm -f "$SHADOW_PATH"', script)
        self.assertIn("--stop-service", script)
        self.assertIn("launchctl bootout", script)

    def test_watchdog_only_restarts_unhealthy_service(self):
        script = self._script("watchdog.sh")
        self.assertIn("/health", script)
        self.assertIn("launchctl kickstart -k", script)
        self.assertIn("nohup", script)
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:7897", script)
        self.assertNotIn("TYPESAFE_API_KEY", script)


class CodexRouterCatalogTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script("codex_router_catalog", "codex_router_catalog.py")

    def test_upsert_preserves_other_models_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user-models.json"
            path.write_text(
                json.dumps({"version": 1, "models": [{"slug": "other/model", "listed": True}]}),
                encoding="utf-8",
            )

            self.module.upsert(path)
            first = path.read_text(encoding="utf-8")
            self.module.upsert(path)
            second = path.read_text(encoding="utf-8")

            document = json.loads(second)
            self.assertEqual(first, second)
            self.assertEqual([item["slug"] for item in document["models"]], ["other/model", "jev/auto"])
            route = document["models"][-1]
            self.assertEqual(route["gatewayModel"], "jev-auto")
            self.assertEqual(route["upstreamModel"], "auto")
            self.assertEqual(route["provider"], "jev")
            self.assertEqual(route["displayName"], "Codex + Jev Router")
            self.assertEqual(
                [item["effort"] for item in route["reasoningLevels"]],
                ["low", "medium", "high", "xhigh", "max"],
            )


class ShadowReportTests(unittest.TestCase):
    def test_daily_report_groups_roots_reprices_usage_and_marks_missing(self):
        module = load_script("shadow_report", "shadow-report.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            rows = [
                {
                    "at": "2026-09-20T09:00:00+0800",
                    "thread_id": "parent",
                    "parent_thread_id": None,
                    "event": "first_request",
                    "gate": "apply",
                    "reason": "jev",
                    "consulted_jev": True,
                    "jev_ms": 1000,
                    "jev": {"tier": {"choice": "gpt-5.6-luna", "confidence": 0.8}},
                    "shadow": True,
                    "model": "gpt-6-astra",
                    "would": {"model": "gpt-5.6-luna"},
                    "upstream_model": "gpt-6-astra",
                    "response_completed": True,
                    "usage": {"input_tokens": 100, "cached_tokens": 0, "output_tokens": 10},
                },
                {
                    "at": "2026-09-20T09:01:00+0800",
                    "thread_id": "parent",
                    "event": "tool_continuation",
                    "gate": "sticky",
                    "consulted_jev": False,
                    "jev_ms": None,
                    "jev": None,
                    "shadow": True,
                    "model": "gpt-6-astra",
                    "would": {"model": "gpt-5.6-luna"},
                    "upstream_model": "gpt-6-astra",
                    "response_completed": True,
                    "usage": {"input_tokens": 100, "cached_tokens": 50, "output_tokens": 10},
                },
                {
                    "at": "2026-09-20T09:02:00+0800",
                    "thread_id": "child",
                    "parent_thread_id": "parent",
                    "event": "subagent_first",
                    "gate": "apply",
                    "reason": "low_confidence",
                    "consulted_jev": True,
                    "jev_ms": 3000,
                    "jev": {"tier": {"choice": "gpt-5.6-luna", "confidence": 0.4}},
                    "shadow": True,
                    "model": "gpt-6-astra",
                    "would": {"model": "gpt-5.6-sol"},
                    "upstream_model": "gpt-6-astra",
                    "response_completed": False,
                    "usage": {"input_tokens": None, "cached_tokens": None, "output_tokens": None},
                },
                {
                    "at": "2026-09-20T10:00:00+0800",
                    "thread_id": "error-root",
                    "event": "first_request",
                    "gate": "jev_error:TimeoutError",
                    "reason": "jev_error",
                    "consulted_jev": True,
                    "jev_ms": 5000,
                    "jev": None,
                    "shadow": True,
                    "model": "gpt-6-astra",
                    "would": {"model": "gpt-6-astra"},
                    "upstream_model": "gpt-6-astra",
                    "response_completed": True,
                    "usage": {"input_tokens": 50, "cached_tokens": 0, "output_tokens": 5},
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            report = module.build_report([path])
            day = report["days"]["2026-09-20"]

            self.assertEqual(day["sessions"], 2)
            self.assertEqual(day["threads"], 3)
            self.assertEqual(day["decisions"], 3)
            self.assertEqual(day["would_tiers"], {"astra": 1, "luna": 1, "sol": 1})
            self.assertEqual(day["raw_jev_tiers"], {"luna": 2})
            self.assertEqual(day["confidence_bins"]["0.35-0.50"], 1)
            self.assertEqual(day["confidence_bins"]["0.75-1.00"], 1)
            self.assertEqual(day["low_confidence_fallbacks"], 1)
            self.assertEqual(day["jev_errors"], 1)
            self.assertAlmostEqual(day["jev_error_rate"], 1 / 3)
            self.assertEqual(day["jev_latency_ms"]["median"], 3000)
            self.assertEqual(day["usage_rows_missing"], 1)
            self.assertGreater(day["actual_cost_usd"], day["would_cost_usd"])
            self.assertGreater(day["projected_savings_usd"], 0)

if __name__ == "__main__":
    unittest.main()
