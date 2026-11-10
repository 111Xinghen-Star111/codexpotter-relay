"""Session naming must make relay sessions identifiable in Codex's session list."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay.checkpoint import GitRepository
from relay.config import RelayConfig
from relay.controller import Budget, RelayController
from relay.db import Database
from relay.mock_agent import MockAgent, MockBehaviour


class SessionNamingTests(unittest.TestCase):
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
        self.agent = MockAgent(
            project_root=self.root, behaviour=MockBehaviour(context_window=50_000, tokens_per_turn=5_000)
        )

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()

    def _run(self, *, tasks: list[dict], max_handoffs: int = 1) -> RelayController:
        chain = RelayController.create_chain(
            database=self.database,
            project_root=self.root,
            goal="naming scenario",
            tasks=tasks,
            acceptance_commands=[],
            config=self.config,
            mode="test",
            test_level="mock",
            simulated=True,
        )
        self.chain_id = chain.id
        controller = RelayController(
            config=self.config,
            database=self.database,
            chain_id=chain.id,
            driver=self.agent,
            logger=None,
            simulated_ratio=0.5,
            budget=Budget(max_turns=6, max_handoffs=max_handoffs, max_model_requests=6),
        )
        controller.run()
        return controller

    def test_first_session_gets_a_readable_label(self) -> None:
        self._run(tasks=[{"id": "T1", "title": "create the thing"}])
        thread_id = next(iter(self.agent.thread_name_history))
        first_label = self.agent.thread_name_history[thread_id][0]
        short = self.chain_id[3:11]
        self.assertTrue(first_label.startswith(f"Relay {short} #1"), first_label)
        self.assertIn("T1", first_label)

    def test_label_tracks_the_next_task(self) -> None:
        self._run(tasks=[{"id": "T1", "title": "first"}, {"id": "T2", "title": "second"}])
        thread_id = next(iter(self.agent.thread_name_history))
        history = self.agent.thread_name_history[thread_id]
        # The label is refreshed as the ledger advances, so T2 shows up mid-run.
        self.assertTrue(any("T2" in label for label in history), history)

    def test_git_metadata_is_attached_once(self) -> None:
        self._run(tasks=[{"id": "T1", "title": "only"}], max_handoffs=0)
        self.assertTrue(self.agent.thread_metadata, "git metadata should be attached once")
        first = next(iter(self.agent.thread_metadata.values()))
        self.assertIsNotNone(first["branch"])

    def test_naming_failure_does_not_break_the_run(self) -> None:
        def boom(thread_id: str, name: str) -> bool:
            raise RuntimeError("naming endpoint unavailable")

        self.agent.set_thread_name = boom  # type: ignore[method-assign]
        controller = self._run(tasks=[{"id": "T1", "title": "only"}], max_handoffs=0)
        chain = self.database.get_chain(self.chain_id)
        self.assertIn(chain.state, ("complete", "stopped", "paused"))
        self.assertTrue(controller._turns_run >= 1 or chain.state == "stopped")


if __name__ == "__main__":
    unittest.main()
