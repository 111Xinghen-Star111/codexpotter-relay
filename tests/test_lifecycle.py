"""Lifecycle tests: pause, resume and stop must stop model requests.

These run against the mock driver, so they assert the control logic without
spending quota. The requirement they protect is explicit: while the operator has
paused a chain, the controller must not issue further model requests.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay.checkpoint import GitRepository
from relay.config import RelayConfig
from relay.controller import Budget, RelayController
from relay.db import Database
from relay.mock_agent import MockAgent, MockBehaviour
from relay.ops import pause_chain, resume_chain, stop_chain


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name) / "project"
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = Database(Path(self._temporary.name) / "relay.db")
        self.database.migrate()
        self.config = RelayConfig()
        git = GitRepository(self.root)
        git.init()
        git.configure_identity()
        self.chain = RelayController.create_chain(
            database=self.database,
            project_root=self.root,
            goal="lifecycle scenario",
            tasks=[{"id": "T1", "title": "only task"}],
            acceptance_commands=[],
            config=self.config,
            mode="test",
            test_level="mock",
            simulated=True,
        )

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()

    def _controller(self, behaviour: MockBehaviour | None = None) -> RelayController:
        return RelayController(
            config=self.config,
            database=self.database,
            chain_id=self.chain.id,
            driver=MockAgent(project_root=self.root, behaviour=behaviour or MockBehaviour()),
            logger=None,
            simulated_ratio=0.2,
            budget=Budget(max_turns=4, max_handoffs=1, max_model_requests=4),
        )


class PauseTests(LifecycleTestCase):
    def test_paused_chain_issues_no_model_request(self) -> None:
        pause_chain(self.database, self.database.get_chain(self.chain.id))
        result = self._controller().run()
        self.assertEqual(result.state, "paused")
        self.assertEqual(result.turns, 0)
        self.assertEqual(self.database.count_model_requests(self.chain.id), 0)

    def test_resume_lets_the_chain_finish(self) -> None:
        pause_chain(self.database, self.database.get_chain(self.chain.id))
        self.assertEqual(self._controller().run().state, "paused")

        resume_chain(self.database, self.database.get_chain(self.chain.id))
        result = self._controller().run()
        self.assertIn(result.state, ("complete", "stopped"))
        self.assertGreaterEqual(self.database.count_model_requests(self.chain.id), 1)

    def test_resume_requires_a_paused_chain(self) -> None:
        with self.assertRaises(RuntimeError):
            resume_chain(self.database, self.database.get_chain(self.chain.id))


class StopTests(LifecycleTestCase):
    def test_stopped_chain_issues_no_model_request(self) -> None:
        stop_chain(self.database, self.database.get_chain(self.chain.id))
        result = self._controller().run()
        self.assertEqual(result.state, "stopped")
        self.assertEqual(self.database.count_model_requests(self.chain.id), 0)

    def test_stop_is_idempotent_on_terminal_chains(self) -> None:
        stop_chain(self.database, self.database.get_chain(self.chain.id))
        stopped = stop_chain(self.database, self.database.get_chain(self.chain.id))
        self.assertEqual(stopped.state, "stopped")


class BudgetGuardTests(LifecycleTestCase):
    def test_stalled_agent_stops_at_the_turn_budget(self) -> None:
        controller = self._controller(MockBehaviour(stall=True))
        result = controller.run()
        self.assertEqual(result.state, "stopped")
        self.assertLessEqual(result.turns, 4)


if __name__ == "__main__":
    unittest.main()
