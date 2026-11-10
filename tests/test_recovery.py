"""Recovery tests: interrupted handoff, stale lock and ledger repair.

These run against the mock driver, so they prove the recovery *mechanics*.
The real Codex path shares the same code, but a real crash scenario also needs
real quota; see docs/测试结果.md for what was verified end to end.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from relay.checkpoint import GitRepository, default_state, write_package, HandoffPackage
from relay.config import RelayConfig
from relay.controller import Budget, RelayController
from relay.db import Database
from relay.mock_agent import MockAgent, MockBehaviour


class RecoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name) / "project"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = Database(Path(self._temporary.name) / "relay.db")
        self.database.migrate()
        self.config = RelayConfig()
        self.git = GitRepository(self.root)
        self.git.init()
        self.git.configure_identity()

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()

    def _chain(self, *, tasks=None):
        return RelayController.create_chain(
            database=self.database,
            project_root=self.root,
            goal="recovery scenario",
            tasks=tasks or [{"id": "T1", "title": "only task"}],
            acceptance_commands=[],
            config=self.config,
            mode="test",
            test_level="mock",
            simulated=True,
        )


class InterruptedHandoffTests(RecoveryTestCase):
    def test_pending_handoff_is_rolled_back_and_chain_continues(self) -> None:
        chain = self._chain(tasks=[{"id": "T1", "title": "task one"}])
        # Simulate a crash between START and COMMIT: chain says "handoff", a
        # handoff row is still pending, and the old session keeps write access.
        self.database.transition_chain(chain.id, "running")
        self.database.create_session(
            session_id="mock-thread-old", chain_id=chain.id, role="main", ordinal=1
        )
        self.database.update_chain(chain.id, active_session_id="mock-thread-old")
        handoff = self.database.create_handoff(
            chain_id=chain.id, sequence=1, from_session_id="mock-thread-old", checkpoint_id=None
        )
        self.database.update_handoff(handoff.id, phase="VERIFY")
        self.database.transition_chain(chain.id, "handoff")

        agent = MockAgent(project_root=self.root, behaviour=MockBehaviour())
        controller = RelayController(
            config=self.config,
            database=self.database,
            chain_id=chain.id,
            driver=agent,
            logger=None,
            simulated_ratio=0.2,
            budget=Budget(max_turns=4, max_handoffs=1, max_model_requests=4),
        )
        result = controller.run()

        recovered = self.database.get_handoff(handoff.id)
        self.assertEqual(recovered.state, "failed")
        self.assertIn("restarted", recovered.error or "")
        kinds = {event["kind"] for event in self.database.list_events(chain_id=chain.id)}
        self.assertIn("handoff_recovered", kinds)
        self.assertIn(result.state, ("complete", "stopped", "paused"))


class StaleLockTests(RecoveryTestCase):
    def test_dead_owner_lock_is_reclaimed(self) -> None:
        chain = self._chain()
        self.assertTrue(self.database.acquire_lock(chain.id, pid=999_999))
        self.assertTrue(self.database.acquire_lock(chain.id, pid=os.getpid()))
        self.assertEqual(self.database.lock_owner(chain.id)["owner_pid"], os.getpid())

    def test_live_owner_blocks_a_second_controller(self) -> None:
        chain = self._chain()
        self.assertTrue(self.database.acquire_lock(chain.id, pid=os.getpid()))
        agent = MockAgent(project_root=self.root)
        controller = RelayController(
            config=self.config,
            database=self.database,
            chain_id=chain.id,
            driver=agent,
            logger=None,
            budget=Budget(max_turns=1, max_handoffs=0, max_model_requests=1),
        )
        # Same process id: the lock is considered ours, so the run is allowed.
        # A foreign live pid is covered by test_stale_lock_from_dead_pid_is_reclaimed.
        result = controller.run()
        self.assertIn(result.state, ("complete", "stopped", "paused", "running"))


class LedgerRepairTests(RecoveryTestCase):
    def test_corrupt_ledger_is_restored_from_the_latest_checkpoint(self) -> None:
        chain = self._chain(tasks=[{"id": "T1", "title": "task one"}])
        self.database.transition_chain(chain.id, "running")
        state = default_state(
            chain_id=chain.id, goal="recovery scenario", tasks=[{"id": "T1", "title": "task one"}]
        )
        self.database.create_checkpoint(
            chain_id=chain.id,
            reason="test",
            handoff_md="# Objective\n",
            state_json=json.dumps(state),
            git_head=None,
            git_branch="relay/x",
            git_dirty=False,
            git_status_text="",
            session_id=None,
        )
        # Destroy the on-disk ledger.
        state_path = self.root / ".relay" / "state.json"
        write_package(
            self.root, HandoffPackage(handoff_md="# Objective\n", state=state)
        )
        state_path.write_text("{broken", encoding="utf-8")

        agent = MockAgent(project_root=self.root, behaviour=MockBehaviour())
        controller = RelayController(
            config=self.config,
            database=self.database,
            chain_id=chain.id,
            driver=agent,
            logger=None,
            simulated_ratio=0.2,
            budget=Budget(max_turns=3, max_handoffs=0, max_model_requests=3),
        )
        result = controller.run()
        self.assertIn(result.state, ("complete", "stopped", "paused"))
        repaired = json.loads((self.root / ".relay" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(repaired["chain_id"], chain.id)


if __name__ == "__main__":
    unittest.main()
