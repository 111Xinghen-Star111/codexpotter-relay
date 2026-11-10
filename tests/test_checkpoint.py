"""Unit tests for the handoff package and git helpers."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from relay.checkpoint import (
    CheckpointError,
    GitRepository,
    HandoffPackage,
    all_tasks_done,
    build_handoff_markdown,
    default_state,
    ensure_scaffold,
    handoff_paths,
    next_pending_task,
    read_package,
    validate_state,
    write_package,
)
from relay.handoff import parse_marker


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = default_state(
            chain_id="ch_1",
            goal="build a feature",
            tasks=[
                {"id": "T1", "title": "first"},
                {"id": "T2", "title": "second"},
            ],
            acceptance_commands=["python -m unittest"],
        )

    def test_default_state_is_valid(self) -> None:
        validate_state(self.state)
        self.assertEqual(self.state["next_task_id"], "T1")

    def test_next_pending_task_prefers_in_progress(self) -> None:
        self.state["tasks"][1]["status"] = "in_progress"
        self.assertEqual(next_pending_task(self.state)["id"], "T2")

    def test_all_tasks_done(self) -> None:
        self.assertFalse(all_tasks_done(self.state))
        for task in self.state["tasks"]:
            task["status"] = "done"
        self.assertTrue(all_tasks_done(self.state))

    def test_invalid_status_is_rejected(self) -> None:
        self.state["tasks"][0]["status"] = "finished"
        with self.assertRaises(CheckpointError):
            validate_state(self.state)

    def test_duplicate_task_ids_are_rejected(self) -> None:
        self.state["tasks"][1]["id"] = "T1"
        with self.assertRaises(CheckpointError):
            validate_state(self.state)

    def test_unknown_next_task_is_rejected(self) -> None:
        self.state["next_task_id"] = "T9"
        with self.assertRaises(CheckpointError):
            validate_state(self.state)


class PackageTests(unittest.TestCase):
    def test_write_then_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = default_state(
                chain_id="ch_1", goal="goal", tasks=[{"id": "T1", "title": "t"}]
            )
            package = HandoffPackage(handoff_md="# Objective\n", state=state)
            handoff_path, state_path = write_package(root, package)
            self.assertTrue(handoff_path.is_file())
            self.assertTrue(state_path.is_file())
            loaded = read_package(root)
            self.assertEqual(loaded.state["chain_id"], "ch_1")
            self.assertIn("# Objective", loaded.handoff_md)

    def test_scaffold_adds_gitignore_entry_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ensure_scaffold(root)
            ensure_scaffold(root)
            content = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(content.count(".relay/"), 1)

    def test_corrupt_json_raises_checkpoint_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = default_state(chain_id="ch_1", goal="g", tasks=[{"id": "T1", "title": "t"}])
            write_package(root, HandoffPackage(handoff_md="# x", state=state))
            _, state_path = handoff_paths(root)
            state_path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CheckpointError):
                read_package(root)

    def test_missing_files_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CheckpointError):
                read_package(Path(directory))


class HandoffMarkdownTests(unittest.TestCase):
    def test_required_sections_and_protocol_marker_are_present(self) -> None:
        state = default_state(
            chain_id="ch_1", goal="goal", tasks=[{"id": "T1", "title": "task one"}]
        )
        markdown = build_handoff_markdown(
            chain_id="ch_1",
            goal="goal",
            original_request="goal",
            context_notes="- note",
            state=state,
            git_snapshot=GitSnapshotStub(),
            handoff_sequence=2,
        )
        self.assertIn("# Objective", markdown)
        self.assertIn("## Original User Request", markdown)
        self.assertIn("::relay(done)", markdown)
        self.assertIn("handoff #3", markdown.lower().replace("session #3", "handoff #3"))

    def test_marker_parser(self) -> None:
        self.assertEqual(parse_marker("done ::relay(progress)"), "progress")
        self.assertEqual(parse_marker("::RELAY(DONE)"), "done")
        self.assertIsNone(parse_marker("no marker here"))


class GitSnapshotStub:
    branch = "relay/ch_1"
    head = "abc1234"
    dirty = False
    status_text = ""
    has_commits = True


class GitRepositoryTests(unittest.TestCase):
    def test_init_commit_and_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = GitRepository(root)
            git.init()
            git.configure_identity()
            (root / "a.txt").write_text("hello", encoding="utf-8")
            head = git.commit_all("feat: first")
            self.assertIsNotNone(head)
            git.ensure_branch("relay/ch_1")
            snapshot = git.snapshot()
            self.assertEqual(snapshot.branch, "relay/ch_1")
            self.assertFalse(snapshot.dirty)

    def test_commit_all_returns_none_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = GitRepository(root)
            git.init()
            git.configure_identity()
            (root / "a.txt").write_text("hello", encoding="utf-8")
            git.commit_all("first")
            self.assertIsNone(git.commit_all("second"))

    def test_snapshot_reports_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = GitRepository(root)
            git.init()
            git.configure_identity()
            (root / "a.txt").write_text("hello", encoding="utf-8")
            git.commit_all("first")
            (root / "b.txt").write_text("changed", encoding="utf-8")
            self.assertTrue(git.snapshot().dirty)

    def test_ensure_identity_makes_commit_possible_without_global_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = GitRepository(root)
            git.init()
            # Simulate a machine with no git identity at all.
            for key in ("user.name", "user.email"):
                subprocess.run(
                    [git._git, "config", "--local", "--unset", key],  # noqa: SLF001
                    cwd=str(root),
                    capture_output=True,
                    check=False,
                )
            self.assertTrue(git.ensure_identity())
            self.assertFalse(git.ensure_identity())
            (root / "a.txt").write_text("hello", encoding="utf-8")
            self.assertIsNotNone(git.commit_all("first"))

    def test_unborn_branch_snapshot_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = GitRepository(root)
            git.init()
            snapshot = git.snapshot()
            self.assertIsNone(snapshot.head)
            self.assertFalse(snapshot.has_commits)
            self.assertTrue(snapshot.branch)

    def test_git_calls_scope_safe_directory_to_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            git = GitRepository(Path(directory))
            command = git._command(["--version"])  # noqa: SLF001
            self.assertIn("safe.directory", " ".join(command))
            self.assertTrue(any(str(Path(directory)) in part for part in command))


if __name__ == "__main__":
    unittest.main()
