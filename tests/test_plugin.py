"""Tests for the Codex plugin packaging (marketplace + manifest rendering)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import plugin


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name) / "marketplace"
        self.paths = plugin.render_plugin(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_marketplace_manifest_lists_the_plugin(self) -> None:
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], plugin.MARKETPLACE_NAME)
        entry = manifest["plugins"][0]
        self.assertEqual(entry["name"], plugin.PLUGIN_NAME)
        self.assertEqual(entry["source"]["path"], f"./plugins/{plugin.PLUGIN_NAME}")

    def test_plugin_manifest_is_a_codex_plugin(self) -> None:
        manifest = json.loads((self.paths.plugin_root / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], plugin.PLUGIN_NAME)
        self.assertIn("com.openai", manifest["extensions"])
        interface = manifest["extensions"]["com.openai"]["interface"]
        self.assertIn("displayName", interface)
        self.assertTrue(manifest["extensions"]["com.openai"]["requires_local_executor"])

    def test_mcp_manifest_points_at_the_installed_interpreter(self) -> None:
        manifest = json.loads((self.paths.plugin_root / "mcp.json").read_text(encoding="utf-8"))
        server = manifest["mcpServers"][plugin.PLUGIN_NAME]
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"], ["-m", "relay.mcp"])
        self.assertEqual(server["type"], "stdio")
        self.assertIn("PYTHONPATH", server["env"])

    def test_skill_is_shipped_and_mentions_the_tools(self) -> None:
        skill = (self.paths.plugin_root / "skills" / "relay" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: relay", skill)
        self.assertIn("relay_start", skill)
        self.assertIn("relay_status", skill)

    def test_render_is_idempotent(self) -> None:
        before = (self.paths.plugin_root / "plugin.json").read_text(encoding="utf-8")
        plugin.render_plugin(self.root)
        after = (self.paths.plugin_root / "plugin.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.codex_home = Path(self._temporary.name) / "codex-home"
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self._patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        self._patch.start()
        self.marketplace = Path(self._temporary.name) / "marketplace"
        plugin.render_plugin(self.marketplace)

    def tearDown(self) -> None:
        self._patch.stop()
        self._temporary.cleanup()

    def test_not_registered_before_the_config_entry_exists(self) -> None:
        status = plugin.plugin_status(self.marketplace)
        self.assertTrue(status["rendered"])
        self.assertFalse(status["registered"])
        self.assertFalse(status["cache_present"])

    def test_registration_is_detected_from_the_config(self) -> None:
        config = self.codex_home / "config.toml"
        config.write_text(
            f'[plugins."{plugin.PLUGIN_NAME}@{plugin.MARKETPLACE_NAME}"]\nenabled = true\n',
            encoding="utf-8",
        )
        status = plugin.plugin_status(self.marketplace)
        self.assertTrue(status["registered"])

    def test_cache_presence_is_detected(self) -> None:
        cache = self.codex_home / "plugins" / "cache" / plugin.MARKETPLACE_NAME
        cache.mkdir(parents=True)
        self.assertTrue(plugin.plugin_status(self.marketplace)["cache_present"])


if __name__ == "__main__":
    unittest.main()
