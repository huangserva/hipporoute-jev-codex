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


if __name__ == "__main__":
    unittest.main()
