"""Unit tests for configuration parsing and validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay.config import (
    ConfigError,
    RelayConfig,
    RelaySettings,
    load_config,
    parse_config_text,
    write_default_config,
)


class ConfigParsingTests(unittest.TestCase):
    def test_documented_yaml_block_is_accepted(self) -> None:
        text = """
relay:
  context_threshold: 0.80
  checkpoint_threshold: 0.65
  auto_handoff: true

testing:
  enabled: false
  simulate_context_pressure: false
  simulated_usage_ratio: 0.80
  max_handoffs: 3
"""
        config = parse_config_text(text)
        self.assertAlmostEqual(config.relay.context_threshold, 0.80)
        self.assertAlmostEqual(config.relay.checkpoint_threshold, 0.65)
        self.assertTrue(config.relay.auto_handoff)
        self.assertFalse(config.testing.enabled)
        self.assertAlmostEqual(config.testing.simulated_usage_ratio, 0.80)
        self.assertEqual(config.testing.max_handoffs, 3)

    def test_toml_is_accepted(self) -> None:
        text = """
[relay]
context_threshold = 0.8
checkpoint_threshold = 0.6

[controller]
poll_interval_seconds = 2.5
"""
        config = parse_config_text(text)
        self.assertAlmostEqual(config.relay.context_threshold, 0.8)
        self.assertAlmostEqual(config.controller.poll_interval_seconds, 2.5)

    def test_thresholds_must_increase(self) -> None:
        text = """
[relay]
checkpoint_threshold = 0.9
prepare_threshold = 0.7
"""
        with self.assertRaises(ConfigError):
            parse_config_text(text)

    def test_unknown_key_is_rejected(self) -> None:
        text = """
[relay]
context_treshold = 0.8
"""
        with self.assertRaises(ConfigError) as context:
            parse_config_text(text)
        self.assertIn("unknown keys", str(context.exception))

    def test_unknown_section_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config_text("[nonsense]\nx = 1\n")

    def test_testing_requires_a_trigger(self) -> None:
        from relay.config import TestingSettings

        with self.assertRaises(ConfigError):
            RelayConfig().with_testing_overrides(enabled=True)
        with self.assertRaises(ConfigError):
            TestingSettings(enabled=True).validate()

    def test_simulated_ratio_bounds(self) -> None:
        with self.assertRaises(ConfigError):
            RelayConfig().with_testing_overrides(simulated_usage_ratio=1.5)

    def test_yaml_lists_are_rejected_explicitly(self) -> None:
        text = """
testing:
  - item
"""
        with self.assertRaises(ConfigError):
            parse_config_text(text)

    def test_default_config_template_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.toml"
            write_default_config(path)
            config = load_config(path)
            self.assertAlmostEqual(config.relay.checkpoint_threshold, 0.65)
            self.assertAlmostEqual(config.relay.prepare_threshold, 0.75)
            self.assertAlmostEqual(config.relay.context_threshold, 0.80)
            self.assertAlmostEqual(config.relay.urgent_threshold, 0.90)
            self.assertFalse(config.testing.enabled)

    def test_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory) / "does-not-exist.toml")
            self.assertEqual(config.relay, RelaySettings())


if __name__ == "__main__":
    unittest.main()
