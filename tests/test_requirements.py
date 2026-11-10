"""Mid-chain requirements: a later instruction must reach the next session."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay.checkpoint import GitRepository, read_package
from relay.config import RelayConfig
from relay.controller import RelayController
from relay.db import Database
from relay.mcp import RelayToolbox
from relay.ops import append_requirement


class RequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name) / "project"
        self.root.mkdir(parents=True, exist_ok=True)
        git = GitRepository(self.root)
        git.init()
        git.configure_identity()
        self.database = Database(Path(self._temporary.name) / "relay.db")
        self.database.migrate()
        self.chain = RelayController.create_chain(
            database=self.database,
            project_root=self.root,
            goal="original objective",
            tasks=[{"id": "T1", "title": "first"}],
            acceptance_commands=[],
            config=RelayConfig(),
            mode="production",
        )

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()

    def test_requirement_is_recorded_in_the_ledger(self) -> None:
        append_requirement(self.database, self.chain, text="also support --dry-run")
        state = read_package(self.root).state
        summaries = [
            (item.get("summary") if isinstance(item, dict) else item)
            for item in state["decisions"]
        ]
        self.assertTrue(any("also support --dry-run" in str(item) for item in summaries), summaries)

    def test_requirement_can_add_tasks_and_advance_the_next_task(self) -> None:
        state = read_package(self.root).state
        state["tasks"][0]["status"] = "done"
        state["next_task_id"] = None
        from relay.checkpoint import HandoffPackage, write_package

        write_package(self.root, HandoffPackage(handoff_md="# Objective\n", state=state))

        append_requirement(
            self.database,
            self.chain,
            text="add a dry-run test",
            tasks=[{"id": "T2", "title": "dry-run test"}],
        )
        updated = read_package(self.root).state
        self.assertEqual([t["id"] for t in updated["tasks"]], ["T1", "T2"])
        self.assertEqual(updated["next_task_id"], "T2")

    def test_duplicate_task_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            append_requirement(
                self.database,
                self.chain,
                text="dup",
                tasks=[{"id": "T1", "title": "again"}],
            )

    def test_empty_requirement_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            append_requirement(self.database, self.chain, text="   ")

    def test_checkpoint_is_written_with_the_requirement(self) -> None:
        checkpoint = append_requirement(self.database, self.chain, text="new constraint")
        self.assertEqual(checkpoint.reason, "requirement")
        self.assertTrue(checkpoint.is_intact())
        self.assertIn("new constraint", checkpoint.handoff_md)
        events = [event["kind"] for event in self.database.list_events(chain_id=self.chain.id)]
        self.assertIn("requirement_added", events)

    def test_next_session_reads_the_requirement_from_handoff(self) -> None:
        append_requirement(self.database, self.chain, text="must not touch the network")
        handoff = (self.root / ".relay" / "HANDOFF.md").read_text(encoding="utf-8")
        # The regenerated handoff carries the requirement for the next session.
        self.assertIn("must not touch the network", handoff)

    def test_mcp_tool_records_the_requirement(self) -> None:
        toolbox = RelayToolbox(config=RelayConfig(), database=self.database)
        result = toolbox.call(
            "relay_add_requirement",
            {"chain": self.chain.id, "text": "support windows paths"},
        )
        self.assertFalse(result.is_error)
        self.assertIn("requirement recorded", result.text)
        state = read_package(self.root).state
        self.assertTrue(any("support windows paths" in str(item) for item in state["decisions"]))


if __name__ == "__main__":
    unittest.main()
