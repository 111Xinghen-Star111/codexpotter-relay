"""Unit tests for the hook collector and installer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from relay.db import Database
from relay.hooks import (
    ALL_EVENTS,
    MANAGED_BLOCK_END,
    MANAGED_BLOCK_START,
    REQUIRED_EVENTS,
    HookInstaller,
    app_server_hook_events,
    collect_hook_event,
    hooks_observed,
    run_hook_command,
)


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary.name) / "relay.db")
        self.database.migrate()

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()

    def test_hook_event_is_stored(self) -> None:
        stored = collect_hook_event(
            self.database,
            event="sessionStart",
            payload={"threadId": "t1", "eventKey": "k1"},
            chain_id="ch_1",
        )
        self.assertTrue(stored)
        self.assertEqual(hooks_observed(self.database, chain_id="ch_1"), ["sessionStart"])

    def test_redelivery_is_idempotent(self) -> None:
        payload = {"threadId": "t1", "eventKey": "k1"}
        self.assertTrue(
            collect_hook_event(self.database, event="stop", payload=payload, chain_id="ch_1")
        )
        self.assertFalse(
            collect_hook_event(self.database, event="stop", payload=payload, chain_id="ch_1")
        )

    def test_command_never_fails_on_bad_input(self) -> None:
        self.assertEqual(
            run_hook_command(event="postToolUse", database=self.database, stdin_text="{oops"),
            0,
        )
        self.assertEqual(
            run_hook_command(event="postToolUse", database=self.database, stdin_text="[1,2]"),
            0,
        )

    def test_command_records_json_payload(self) -> None:
        payload = json.dumps({"threadId": "t9", "chainId": "ch_9"})
        code = run_hook_command(event="preCompact", database=self.database, stdin_text=payload)
        self.assertEqual(code, 0)
        observed = hooks_observed(self.database, chain_id="ch_9")
        self.assertEqual(observed, ["preCompact"])


class NotificationHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary.name) / "relay.db")
        self.database.migrate()

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()

    def test_app_server_hook_notifications_are_recognised(self) -> None:
        self.database.append_event(
            event_key="a",
            kind="hook_completed",
            source="appserver",
            payload={"eventName": "preCompact", "status": "completed"},
            chain_id="ch_1",
        )
        self.assertEqual(app_server_hook_events(self.database, chain_id="ch_1"), ["preCompact"])


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.config_file = self.root / "config.toml"
        self.config_file.write_text('model = "test-model"\n', encoding="utf-8")
        self.installer = HookInstaller(
            python_executable="python",
            database_path=self.root / "relay.db",
            config_file=self.config_file,
            validate=False,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_block_covers_required_events(self) -> None:
        block = self.installer.render_block()
        for event in REQUIRED_EVENTS:
            self.assertIn(f"[hooks.{event}]", block)

    def test_install_adds_block_and_backup(self) -> None:
        result = self.installer.install()
        self.assertTrue(result["installed"])
        content = self.config_file.read_text(encoding="utf-8")
        self.assertIn(MANAGED_BLOCK_START, content)
        self.assertIn('model = "test-model"', content)
        self.assertTrue(self.installer.backup_file.exists())

    def test_install_is_idempotent(self) -> None:
        self.installer.install()
        self.installer.install()
        content = self.config_file.read_text(encoding="utf-8")
        self.assertEqual(content.count(MANAGED_BLOCK_START), 1)
        self.assertEqual(content.count(MANAGED_BLOCK_END), 1)

    def test_uninstall_restores_original_content(self) -> None:
        self.installer.install()
        result = self.installer.uninstall()
        self.assertTrue(result["removed"])
        content = self.config_file.read_text(encoding="utf-8")
        self.assertNotIn(MANAGED_BLOCK_START, content)
        self.assertEqual(content.strip(), 'model = "test-model"')

    def test_uninstall_without_block_is_a_noop(self) -> None:
        result = self.installer.uninstall()
        self.assertFalse(result["removed"])

    def test_all_events_are_unique(self) -> None:
        self.assertEqual(len(ALL_EVENTS), len(set(ALL_EVENTS)))


if __name__ == "__main__":
    unittest.main()
