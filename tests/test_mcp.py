"""Tests for the MCP server that backs the Codex plugin."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from relay.checkpoint import GitRepository
from relay.config import RelayConfig
from relay.controller import RelayController
from relay.db import Database
from relay.mcp import McpServer, RelayToolbox

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}


class ToolboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name) / "project"
        self.root.mkdir(parents=True, exist_ok=True)
        git = GitRepository(self.root)
        git.init()
        git.configure_identity()
        self.database = Database(Path(self._temporary.name) / "relay.db")
        self.database.migrate()
        self.toolbox = RelayToolbox(config=RelayConfig(), database=self.database)

    def tearDown(self) -> None:
        self.database.close()
        self._temporary.cleanup()

    def _chain(self) -> str:
        chain = RelayController.create_chain(
            database=self.database,
            project_root=self.root,
            goal="mcp scenario",
            tasks=[{"id": "T1", "title": "only task"}],
            acceptance_commands=[],
            config=RelayConfig(),
            mode="production",
        )
        return chain.id


class ToolListTests(ToolboxTestCase):
    def test_all_required_tools_are_exposed(self) -> None:
        names = {tool["name"] for tool in self.toolbox.tools()}
        self.assertEqual(
            names,
            {
                "relay_start",
                "relay_status",
                "relay_chains",
                "relay_report",
                "relay_checkpoint",
                "relay_add_requirement",
                "relay_pause",
                "relay_resume",
                "relay_stop",
                "relay_recover",
                "relay_doctor",
            },
        )

    def test_read_only_tools_are_annotated(self) -> None:
        by_name = {tool["name"]: tool for tool in self.toolbox.tools()}
        self.assertTrue(by_name["relay_status"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["relay_start"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["relay_stop"]["annotations"]["destructiveHint"])

    def test_start_requires_goal_and_project(self) -> None:
        schema = next(t for t in self.toolbox.tools() if t["name"] == "relay_start")["inputSchema"]
        self.assertEqual(set(schema["required"]), {"goal", "project"})


class ToolCallTests(ToolboxTestCase):
    def test_chains_lists_existing_chain(self) -> None:
        chain_id = self._chain()
        result = self.toolbox.call("relay_chains", {})
        self.assertFalse(result.is_error)
        self.assertIn(chain_id, result.text)

    def test_status_reports_state_and_tasks(self) -> None:
        chain_id = self._chain()
        result = self.toolbox.call("relay_status", {"chain": chain_id})
        self.assertFalse(result.is_error)
        self.assertIn("pending", result.text)
        self.assertIn("T1", result.text)

    def test_unknown_tool_is_an_error_not_a_crash(self) -> None:
        result = self.toolbox.call("relay_nope", {})
        self.assertTrue(result.is_error)
        self.assertIn("unknown relay tool", result.text)

    def test_start_rejects_relative_project(self) -> None:
        result = self.toolbox.call("relay_start", {"goal": "x", "project": "relative/path"})
        self.assertTrue(result.is_error)
        self.assertIn("absolute", result.text)

    def test_missing_chain_is_reported(self) -> None:
        result = self.toolbox.call("relay_status", {"chain": "ch_missing"})
        self.assertTrue(result.is_error)


class ProtocolTests(ToolboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = McpServer(self.toolbox)

    def test_initialize_echoes_supported_version(self) -> None:
        result = self.server.dispatch("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "codexpotter-relay")
        self.assertIn("tools", result["capabilities"])

    def test_initialize_falls_back_for_unknown_version(self) -> None:
        result = self.server.dispatch("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(result["protocolVersion"], "2025-11-25")

    def test_unknown_method_returns_none(self) -> None:
        self.assertIsNone(self.server.dispatch("resources/subscribe", {}))

    def test_stdio_loop_answers_requests_only(self) -> None:
        payload = "\n".join(
            [
                json.dumps(INITIALIZE),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                "{not json",
                "",
            ]
        )
        output = io.StringIO()
        self.server.serve(stdin=io.StringIO(payload), stdout=output)
        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)  # initialize, tools/list, parse error
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["id"], 2)
        self.assertEqual(lines[2]["error"]["code"], -32700)

    def test_tool_call_through_protocol(self) -> None:
        chain_id = self._chain()
        result = self.server.dispatch(
            "tools/call", {"name": "relay_status", "arguments": {"chain": chain_id}}
        )
        self.assertIn("content", result)
        self.assertIn(chain_id, result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
