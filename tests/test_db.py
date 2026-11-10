"""Unit tests for the SQLite persistence layer."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from relay.db import (
    CHAIN_TRANSITIONS,
    Database,
    DatabaseError,
    TransitionError,
)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name) / "relay.db"
        self.database = Database(self.path)
        self.database.migrate()
        self.chain = self.database.create_chain(
            chain_id="ch_test",
            project_root=Path(self._temporary.name) / "project",
            goal="test goal",
            mode="test",
            work_branch="relay/ch_test",
            config_json="{}",
            test_level="mock",
            simulated=True,
        )

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()


class MigrationTests(DatabaseTestCase):
    def test_migrate_is_idempotent(self) -> None:
        first = self.database.migrate()
        second = self.database.migrate()
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 1)

    def test_wal_mode_is_enabled(self) -> None:
        row = self.database._connection().execute("PRAGMA journal_mode").fetchone()  # noqa: SLF001
        self.assertEqual(row[0].lower(), "wal")


class ChainStateTests(DatabaseTestCase):
    def test_transition_allows_documented_path(self) -> None:
        chain = self.database.transition_chain("ch_test", "running")
        self.assertEqual(chain.state, "running")
        chain = self.database.transition_chain("ch_test", "handoff")
        self.assertEqual(chain.state, "handoff")
        chain = self.database.transition_chain("ch_test", "running")
        self.assertEqual(chain.state, "running")

    def test_transition_rejects_illegal_jump(self) -> None:
        self.database.transition_chain("ch_test", "running")
        with self.assertRaises(TransitionError):
            self.database.transition_chain("ch_test", "pending")

    def test_terminal_states_have_no_exit(self) -> None:
        self.assertEqual(CHAIN_TRANSITIONS["complete"], frozenset())
        self.assertEqual(CHAIN_TRANSITIONS["failed"], frozenset())

    def test_update_rejects_unknown_column(self) -> None:
        with self.assertRaises(DatabaseError):
            self.database.update_chain("ch_test", nonsense=1)


class EventIdempotencyTests(DatabaseTestCase):
    def test_same_event_key_is_stored_once(self) -> None:
        first = self.database.append_event(
            event_key="k1", kind="turn_completed", source="controller", payload={"a": 1},
            chain_id="ch_test",
        )
        second = self.database.append_event(
            event_key="k1", kind="turn_completed", source="controller", payload={"a": 2},
            chain_id="ch_test",
        )
        self.assertTrue(first)
        self.assertFalse(second)
        events = self.database.list_events(chain_id="ch_test")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"], {"a": 1})


class CheckpointTests(DatabaseTestCase):
    def _write(self, version_note: str = "first"):
        return self.database.create_checkpoint(
            chain_id="ch_test",
            reason=version_note,
            handoff_md="# handoff",
            state_json='{"schema": 1}',
            git_head="abc123",
            git_branch="relay/ch_test",
            git_dirty=False,
            git_status_text="",
            session_id="s1",
        )

    def test_versions_increment(self) -> None:
        first = self._write()
        second = self._write("second")
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)

    def test_checksum_detects_tampering(self) -> None:
        checkpoint = self._write()
        self.assertTrue(checkpoint.is_intact())
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE checkpoints SET handoff_md = ? WHERE id = ?",
                ("# tampered", checkpoint.id),
            )
        tampered = self.database.get_checkpoint(checkpoint.id)
        self.assertFalse(tampered.is_intact())

    def test_latest_valid_checkpoint_skips_broken_rows(self) -> None:
        self._write()
        good = self._write("good")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE checkpoints SET state_json = ? WHERE id = ?",
                ('{"broken": true}', good.id),
            )
        latest_valid = self.database.latest_valid_checkpoint("ch_test")
        self.assertIsNotNone(latest_valid)
        self.assertEqual(latest_valid.version, 1)


class SessionTests(DatabaseTestCase):
    def test_main_ordinals_are_unique(self) -> None:
        self.database.create_session(
            session_id="s1", chain_id="ch_test", role="main", ordinal=1
        )
        with self.assertRaises(Exception):
            self.database.create_session(
                session_id="s2", chain_id="ch_test", role="main", ordinal=1
            )

    def test_subagent_sessions_have_no_ordinal(self) -> None:
        self.database.create_session(
            session_id="sub1", chain_id="ch_test", role="subagent", parent_session_id="s1"
        )
        self.database.create_session(
            session_id="sub2", chain_id="ch_test", role="subagent", parent_session_id="s1"
        )
        self.assertEqual(len(self.database.list_sessions("ch_test")), 2)


class LockTests(DatabaseTestCase):
    def test_lock_is_exclusive_for_other_pids(self) -> None:
        self.assertTrue(self.database.acquire_lock("ch_test", pid=os.getpid()))
        owner = self.database.lock_owner("ch_test")
        self.assertEqual(owner["owner_pid"], os.getpid())

    def test_stale_lock_from_dead_pid_is_reclaimed(self) -> None:
        self.assertTrue(self.database.acquire_lock("ch_test", pid=999_999))
        # A pid that does not exist is stale, so the lock can be taken over.
        self.assertTrue(self.database.acquire_lock("ch_test", pid=os.getpid()))
        self.assertEqual(self.database.lock_owner("ch_test")["owner_pid"], os.getpid())

    def test_release_only_removes_own_lock(self) -> None:
        self.database.acquire_lock("ch_test", pid=999_999)
        self.database.release_lock("ch_test", pid=os.getpid())
        self.assertIsNotNone(self.database.lock_owner("ch_test"))


class ModelRequestTests(DatabaseTestCase):
    def test_counts_split_real_and_simulated(self) -> None:
        self.database.record_model_request(chain_id="ch_test", session_id="s1", simulated=True)
        self.database.record_model_request(chain_id="ch_test", session_id="s1", simulated=False)
        self.assertEqual(self.database.count_model_requests("ch_test"), 2)
        self.assertEqual(self.database.count_model_requests("ch_test", simulated=True), 1)
        self.assertEqual(self.database.count_model_requests("ch_test", simulated=False), 1)


class TaskLedgerTests(DatabaseTestCase):
    def test_upsert_updates_status(self) -> None:
        self.database.upsert_task(
            chain_id="ch_test", task_id="T1", title="first", status="pending"
        )
        self.database.upsert_task(
            chain_id="ch_test",
            task_id="T1",
            title="first",
            status="done",
            evidence="ok",
            verified=True,
        )
        tasks = self.database.list_tasks("ch_test")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "done")
        self.assertTrue(tasks[0]["verified"])

    def test_done_without_evidence_is_not_verified(self) -> None:
        self.database.upsert_task(
            chain_id="ch_test", task_id="T1", title="first", status="done"
        )
        tasks = self.database.list_tasks("ch_test")
        self.assertEqual(tasks[0]["status"], "done")
        self.assertFalse(tasks[0]["verified"])


if __name__ == "__main__":
    unittest.main()
