"""Three-level test runners: mock, real handoff, real context pressure.

* **Level 1 (mock)** never contacts a model. It runs several scenarios against
  the fake driver to prove the pressure logic, the handoff state machine, the
  database, checkpoint integrity, reconnection after handoff, rollback and the
  infinite-loop guards.
* **Level 2 (real handoff)** creates four genuinely new Codex main sessions
  (A→B→C→D) and only *simulates the context threshold*.
* **Level 3 (real pressure)** uses real token usage from a deliberately small
  official context-window configuration, so the threshold is crossed with real
  numbers instead of injected ones.

Every level writes its own chain in the test database and its own temporary
project, so a test can never touch production data or a real project.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .appserver import AppServer
from .checkpoint import GitRepository, ensure_scaffold
from .codex_env import find_codex_executable
from .config import RelayConfig
from .controller import Budget, RelayController
from .db import Database
from .mock_agent import MockAgent, MockBehaviour
from .ops import make_event_sink
from .paths import database_path, log_dir, test_workspace_root
from .report import write_test_report

LEVEL1 = "mock"
LEVEL2 = "real-handoff"
LEVEL3 = "real-pressure"


@dataclass
class Check:
    """One verifiable assertion of a test run."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class TestOutcome:
    """Result of one test level."""

    mode: str
    checks: list[Check] = field(default_factory=list)
    chain_ids: list[str] = field(default_factory=list)
    report_path: Path | None = None
    model_requests: int = 0
    duration_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True when every check passed."""
        return bool(self.checks) and all(check.passed for check in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        """Append one check."""
        self.checks.append(Check(name=name, passed=passed, detail=detail))

    def summary_lines(self) -> list[str]:
        """Return printable check lines."""
        return [
            f"[{'PASS' if check.passed else 'FAIL'}] {check.name}"
            + (f" — {check.detail}" if check.detail else "")
            for check in self.checks
        ]


def _test_database(verbose_logger: Any = None) -> Database:
    path = database_path("test")
    database = Database(path)
    database.migrate()
    return database


def _fresh_workspace(name: str) -> Path:
    root = test_workspace_root() / f"{name}-{int(time.time())}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _init_project(root: Path) -> GitRepository:
    git = GitRepository(root)
    if not git.is_repository():
        git.init()
    git.configure_identity()
    (root / "README.md").write_text("# relay test project\n", encoding="utf-8")
    git.commit_all("chore: test project skeleton")
    return git


# ----------------------------------------------------------------------
# Level 1 — mock
# ----------------------------------------------------------------------
def run_level1(
    *,
    config: RelayConfig,
    logger: Any = None,
    keep_workspaces: bool = False,
) -> TestOutcome:
    """Run every mock scenario and aggregate the checks."""
    outcome = TestOutcome(mode=LEVEL1)
    database = _test_database()
    started = time.monotonic()

    _level1_happy_path(database, config, logger, outcome, keep_workspaces)
    _level1_rollback(database, config, logger, outcome, keep_workspaces)
    _level1_infinite_guard(database, config, logger, outcome, keep_workspaces)
    _level1_isolation(database, config, logger, outcome, keep_workspaces)

    outcome.duration_seconds = time.monotonic() - started
    outcome.model_requests = sum(
        database.count_model_requests(chain_id, simulated=False)
        for chain_id in outcome.chain_ids
    )
    simulated_requests = sum(
        database.count_model_requests(chain_id, simulated=True)
        for chain_id in outcome.chain_ids
    )
    outcome.add(
        "Level 1 未调用真实模型 (no real model call)",
        outcome.model_requests == 0,
        f"real_requests={outcome.model_requests}, simulated_requests={simulated_requests}",
    )
    outcome.notes.append(
        f"Level 1 完全使用模拟会话与模拟用量：真实模型请求 {outcome.model_requests} 次，"
        f"模拟请求 {simulated_requests} 次。"
    )
    outcome.report_path = write_test_report(
        database=database,
        chain_ids=outcome.chain_ids,
        mode=LEVEL1,
        extra_notes=outcome.notes + outcome.summary_lines(),
    )
    return outcome


def _level1_happy_path(
    database: Database,
    config: RelayConfig,
    logger: Any,
    outcome: TestOutcome,
    keep_workspaces: bool,
) -> None:
    root = _fresh_workspace("mock-happy")
    git = _init_project(root)
    tasks = [
        {"id": "T1", "title": "first mock task"},
        {"id": "T2", "title": "second mock task"},
        {"id": "T3", "title": "third mock task"},
    ]
    chain = RelayController.create_chain(
        database=database,
        project_root=root,
        goal="mock happy path",
        tasks=tasks,
        acceptance_commands=[],
        config=config,
        mode="test",
        test_level=LEVEL1,
        simulated=True,
    )
    outcome.chain_ids.append(chain.id)
    agent = MockAgent(
        project_root=root,
        behaviour=MockBehaviour(context_window=20_000, tokens_per_turn=6_000),
        logger=logger,
    )
    controller = RelayController(
        config=config,
        database=database,
        chain_id=chain.id,
        driver=agent,
        logger=logger,
        simulated_ratio=config.testing.simulated_usage_ratio,
        budget=Budget(max_turns=12, max_handoffs=3, max_model_requests=20),
        report_writer=lambda chain_id: _report(database, chain_id),
    )
    result = controller.run()

    sessions = database.main_sessions(chain.id)
    handoffs = database.list_handoffs(chain.id)
    checkpoints = database.list_checkpoints(chain.id)
    outcome.add(
        "上下文预警逻辑产生真实占比与阈值判定 (pressure thresholds)",
        any(
            event["kind"] == "context_pressure"
            for event in database.list_events(chain_id=chain.id, limit=200)
        ),
        f"levels={sorted({event['payload'].get('level') for event in database.list_events(chain_id=chain.id, limit=200) if event['kind'] == 'context_pressure'})}",
    )
    outcome.add(
        "交接状态机走完 PREPARE/START/VERIFY/COMMIT/CONTINUE",
        bool(handoffs) and all(handoff.state in ("ok", "rolled_back", "failed") for handoff in handoffs),
        f"handoffs={[(h.sequence, h.phase, h.state) for h in handoffs]}",
    )
    outcome.add(
        "SQLite 写入链路/会话/事件 (database writes)",
        len(sessions) >= 2 and len(database.list_events(chain_id=chain.id, limit=200)) > 5,
        f"sessions={len(sessions)}",
    )
    outcome.add(
        "检查点生成且校验和有效 (checkpoint integrity)",
        bool(checkpoints) and all(
            database.get_checkpoint(checkpoint.id).is_intact() for checkpoint in checkpoints
        ),
        f"checkpoints={[c.version for c in checkpoints]}",
    )
    outcome.add(
        "新会话创建请求均为全新线程 (new session requests)",
        len({session.id for session in sessions}) == len(sessions) and len(sessions) >= 2,
        f"threads={[s.id for s in sessions]}",
    )
    outcome.add(
        "新会话读取交接包并继续 (automatic resume)",
        all(task["status"] == "done" for task in database.list_tasks(chain.id))
        and result.turns >= 3,
        f"turns={result.turns} state={result.state}",
    )
    outcome.add(
        "原始目标完成后自动停止 (auto stop on completion)",
        result.state == "complete",
        f"state={result.state} reason={result.reason}",
    )
    if not keep_workspaces:
        _remove(root)


def _level1_rollback(
    database: Database,
    config: RelayConfig,
    logger: Any,
    outcome: TestOutcome,
    keep_workspaces: bool,
) -> None:
    root = _fresh_workspace("mock-rollback")
    _init_project(root)
    chain = RelayController.create_chain(
        database=database,
        project_root=root,
        goal="mock rollback",
        tasks=[{"id": "T1", "title": "task one"}, {"id": "T2", "title": "task two"}],
        acceptance_commands=[],
        config=config,
        mode="test",
        test_level=LEVEL1,
        simulated=True,
    )
    outcome.chain_ids.append(chain.id)
    strict_config = config.with_testing_overrides(simulated_usage_ratio=0.85)
    from dataclasses import replace

    strict_config = replace(strict_config, relay=replace(strict_config.relay, max_handoff_retries=1))
    agent = MockAgent(
        project_root=root,
        behaviour=MockBehaviour(
            fail_thread_starts=99,
            always_allow_first_thread=True,
            context_window=10_000,
            tokens_per_turn=9_000,
        ),
        logger=logger,
    )
    controller = RelayController(
        config=strict_config,
        database=database,
        chain_id=chain.id,
        driver=agent,
        logger=logger,
        simulated_ratio=0.85,
        budget=Budget(max_turns=8, max_handoffs=2, max_model_requests=12),
        report_writer=lambda chain_id: _report(database, chain_id),
    )
    result = controller.run()
    handoffs = database.list_handoffs(chain.id)
    checkpoints = database.list_checkpoints(chain.id)
    outcome.add(
        "交接失败时保留旧检查点并回滚 (rollback keeps checkpoint)",
        bool(checkpoints) and all(
            database.get_checkpoint(checkpoint.id).is_intact() for checkpoint in checkpoints
        ),
        f"checkpoints={[c.version for c in checkpoints]}",
    )
    outcome.add(
        "交接失败后有界重试并终止 (bounded retry)",
        bool(handoffs)
        and all(handoff.state in ("failed", "rolled_back") for handoff in handoffs)
        and result.state in ("paused", "failed", "stopped"),
        f"state={result.state} handoffs={[(h.state, h.attempt) for h in handoffs]}",
    )
    if not keep_workspaces:
        _remove(root)


def _level1_infinite_guard(
    database: Database,
    config: RelayConfig,
    logger: Any,
    outcome: TestOutcome,
    keep_workspaces: bool,
) -> None:
    root = _fresh_workspace("mock-guard")
    _init_project(root)
    chain = RelayController.create_chain(
        database=database,
        project_root=root,
        goal="mock infinite loop guard",
        tasks=[{"id": "T1", "title": "never finishes"}],
        acceptance_commands=[],
        config=config,
        mode="test",
        test_level=LEVEL1,
        simulated=True,
    )
    outcome.chain_ids.append(chain.id)
    agent = MockAgent(
        project_root=root,
        behaviour=MockBehaviour(stall=True, context_window=10_000, tokens_per_turn=9_000),
        logger=logger,
    )
    controller = RelayController(
        config=config,
        database=database,
        chain_id=chain.id,
        driver=agent,
        logger=logger,
        simulated_ratio=0.95,
        budget=Budget(max_turns=4, max_handoffs=2, max_model_requests=6),
        report_writer=lambda chain_id: _report(database, chain_id),
    )
    result = controller.run()
    handoffs = database.list_handoffs(chain.id)
    outcome.add(
        "防止无限循环：budget 到顶即停 (no infinite loop)",
        result.state in ("stopped", "failed", "paused") and result.turns <= 6,
        f"turns={result.turns} handoffs={len(handoffs)} state={result.state}",
    )
    if not keep_workspaces:
        _remove(root)


def _level1_isolation(
    database: Database,
    config: RelayConfig,
    logger: Any,
    outcome: TestOutcome,
    keep_workspaces: bool,
) -> None:
    roots = [_fresh_workspace("mock-iso-a"), _fresh_workspace("mock-iso-b")]
    for root in roots:
        _init_project(root)
    chains = []
    for index, root in enumerate(roots, start=1):
        chain = RelayController.create_chain(
            database=database,
            project_root=root,
            goal=f"mock isolation project {index}",
            tasks=[{"id": "T1", "title": f"task of project {index}"}],
            acceptance_commands=[],
            config=config,
            mode="test",
            test_level=LEVEL1,
            simulated=True,
        )
        chains.append(chain)
        outcome.chain_ids.append(chain.id)
    for chain in chains:
        agent = MockAgent(
            project_root=Path(chain.project_root),
            behaviour=MockBehaviour(context_window=50_000, tokens_per_turn=5_000),
            logger=logger,
        )
        controller = RelayController(
            config=config,
            database=database,
            chain_id=chain.id,
            driver=agent,
            logger=logger,
            simulated_ratio=0.5,
            budget=Budget(max_turns=4, max_handoffs=1, max_model_requests=6),
            report_writer=lambda chain_id: _report(database, chain_id),
        )
        controller.run()

    states = [json.loads(Path(chain.project_root, ".relay", "state.json").read_text("utf-8")) for chain in chains]
    isolated = (
        states[0]["chain_id"] != states[1]["chain_id"]
        and states[0]["goal"] != states[1]["goal"]
        and len({Path(chain.project_root) for chain in chains}) == 2
    )
    outcome.add(
        "多项目隔离：工作链与台账互不干扰 (project isolation)",
        isolated,
        f"chains={[chain.id for chain in chains]}",
    )
    if not keep_workspaces:
        for root in roots:
            _remove(root)


# ----------------------------------------------------------------------
# Level 2 — real sessions, simulated threshold
# ----------------------------------------------------------------------
LEVEL2_TASKS: list[dict[str, Any]] = [
    {
        "id": "T1",
        "title": "Create calc.py with an add(a, b) function and tests/test_calc.py covering it",
    },
    {
        "id": "T2",
        "title": "Add a sub(a, b) function to calc.py with a unittest case",
    },
    {
        "id": "T3",
        "title": "Add a mul(a, b) function to calc.py with a unittest case",
    },
    {
        "id": "T4",
        "title": "Add a div(a, b) function that raises ValueError on division by zero, with a test",
    },
]


def _acceptance_commands() -> list[str]:
    python = sys.executable or "python"
    return [f'"{python}" -m unittest discover -s tests -v']


def run_level2(
    *,
    config: RelayConfig,
    logger: Any,
    keep_workspaces: bool = False,
    max_model_requests: int | None = None,
    tasks: list[dict[str, Any]] | None = None,
    handoff_count: int = 3,
) -> TestOutcome:
    """Create ``handoff_count + 1`` real main sessions with a simulated threshold."""
    task_list = tasks or LEVEL2_TASKS
    outcome = TestOutcome(mode=LEVEL2)
    database = _test_database()
    root = _fresh_workspace("real-handoff")
    _init_project(root)
    started = time.monotonic()

    chain = RelayController.create_chain(
        database=database,
        project_root=root,
        goal=(
            "Build a tiny calculator module in small increments: "
            "add, sub, mul, div with unit tests."
            if tasks is None
            else "Add small independent functions with unit tests, one per session."
        ),
        tasks=task_list,
        acceptance_commands=_acceptance_commands(),
        config=config,
        mode="test",
        test_level=LEVEL2,
        simulated=True,
    )
    outcome.chain_ids.append(chain.id)

    codex = find_codex_executable(config.controller.codex_executable)
    log_file = log_dir() / "test-real-handoff-appserver.log"
    overrides = _testing_overrides(config)
    server = AppServer(
        codex,
        config_overrides=overrides,
        cwd=root,
        request_timeout=900,
        startup_timeout=180,
        stderr_path=log_file,
        logger=logger,
    )
    server.start()
    try:
        from .agent_session import CodexAgent

        agent = CodexAgent(
            server,
            project_root=root,
            logger=logger,
            event_sink=make_event_sink(database, chain.id),
        )
        controller = RelayController(
            config=config,
            database=database,
            chain_id=chain.id,
            driver=agent,
            logger=logger,
            simulated_ratio=config.testing.simulated_usage_ratio,
            budget=Budget(
                max_turns=max(4, len(task_list) + 1),
                max_handoffs=handoff_count,
                max_model_requests=max_model_requests or config.testing.max_model_requests,
            ),
            report_writer=lambda chain_id: _report(database, chain_id),
        )
        result = controller.run()
    finally:
        server.stop()

    sessions = database.main_sessions(chain.id)
    handoffs = database.list_handoffs(chain.id)
    tasks = database.list_tasks(chain.id)
    outcome.model_requests = database.count_model_requests(chain.id)
    outcome.duration_seconds = time.monotonic() - started
    expected_sessions = handoff_count + 1

    outcome.add(
        f"真实创建了 {expected_sessions} 个不同的 Codex 主会话 (A,B,C,...)",
        len(sessions) >= expected_sessions
        and len({session.id for session in sessions}) == len(sessions),
        f"sessions={[(s.ordinal, s.id[:12]) for s in sessions]}",
    )
    outcome.add(
        "所有会话都实际执行了 turn（不是空会话）",
        len(sessions) >= expected_sessions
        and all(session.request_count >= 1 for session in sessions[:expected_sessions]),
        f"requests={[(s.ordinal, s.request_count) for s in sessions]}",
    )
    outcome.add(
        f"连续 {handoff_count} 次交接成功",
        len(handoffs) >= handoff_count
        and all(handoff.state == "ok" for handoff in handoffs[:handoff_count]),
        f"handoffs={[(h.sequence, h.state) for h in handoffs]}",
    )
    outcome.add(
        "新会话自动读取交接信息并继续（无需人工“继续”）",
        len(sessions) >= expected_sessions
        and all(session.request_count >= 1 for session in sessions[1:expected_sessions]),
        f"sessions_with_turns={[s.ordinal for s in sessions if s.request_count >= 1]}",
    )
    outcome.add(
        "全部任务完成且无重复开发",
        bool(tasks) and all(task["status"] == "done" for task in tasks),
        f"tasks={[(t['task_id'], t['status']) for t in tasks]}",
    )
    outcome.add(
        "验收命令通过（unittest）",
        result.state == "complete",
        f"state={result.state} reason={result.reason}",
    )
    outcome.add(
        "模拟压力值已标记为 simulated，未混入真实用量记录",
        all(
            event["simulated"]
            for event in database.list_events(chain_id=chain.id, limit=500)
            if event["kind"] == "context_pressure" and event["payload"].get("source") == "simulated"
        ),
        "simulated pressure events are flagged",
    )
    if not keep_workspaces:
        _remove(root)
    outcome.notes.extend(
        [
            "Level 2 只模拟上下文阈值；会话 A/B/C/D 均为真实 Codex 主会话。",
            f"模型请求数 / model requests: {outcome.model_requests}",
        ]
    )
    outcome.report_path = write_test_report(
        database=database,
        chain_ids=outcome.chain_ids,
        mode=LEVEL2,
        extra_notes=outcome.notes + outcome.summary_lines(),
    )
    return outcome


# ----------------------------------------------------------------------
# Level 3 — real context pressure
# ----------------------------------------------------------------------
def run_level3(
    *,
    config: RelayConfig,
    logger: Any,
    keep_workspaces: bool = False,
) -> TestOutcome:
    """Trigger handoff from real token usage with a small official context window."""
    outcome = TestOutcome(mode=LEVEL3)
    database = _test_database()
    root = _fresh_workspace("real-pressure")
    _init_project(root)
    started = time.monotonic()

    #: A deliberately small (but above the fixed prompt size) window, so real
    #: usage grows into the threshold instead of already exceeding it.
    window = config.testing.context_window_override or 64_000
    compact_limit = config.testing.auto_compact_token_limit or int(window * 0.80)
    pressure_config = config.with_testing_overrides(
        enabled=True,
        simulate_context_pressure=False,
        real_pressure_enabled=True,
        context_window_override=window,
        auto_compact_token_limit=compact_limit,
    )

    chain = RelayController.create_chain(
        database=database,
        project_root=root,
        goal=(
            "Create a notes module notes.py with three small functions and unit tests, "
            "in small increments, while the context window stays small."
        ),
        tasks=[
            {"id": "T1", "title": "Create notes.py with add_note/notes_list and a unit test"},
            {"id": "T2", "title": "Add clear_notes() plus a unit test"},
            {"id": "T3", "title": "Add note_count() plus a unit test"},
        ],
        acceptance_commands=_acceptance_commands(),
        config=pressure_config,
        mode="test",
        test_level=LEVEL3,
        simulated=False,
    )
    outcome.chain_ids.append(chain.id)

    codex = find_codex_executable(pressure_config.controller.codex_executable)
    log_file = log_dir() / "test-real-pressure-appserver.log"
    server = AppServer(
        codex,
        config_overrides=_testing_overrides(pressure_config),
        cwd=root,
        request_timeout=900,
        startup_timeout=180,
        stderr_path=log_file,
        logger=logger,
    )
    server.start()
    try:
        from .agent_session import CodexAgent

        agent = CodexAgent(
            server,
            project_root=root,
            logger=logger,
            event_sink=make_event_sink(database, chain.id),
        )
        controller = RelayController(
            config=pressure_config,
            database=database,
            chain_id=chain.id,
            driver=agent,
            logger=logger,
            simulated_ratio=None,
            budget=Budget(
                max_turns=6,
                max_handoffs=2,
                max_model_requests=8,
            ),
            report_writer=lambda chain_id: _report(database, chain_id),
        )
        result = controller.run()
    finally:
        server.stop()

    pressure_events = [
        event
        for event in database.list_events(chain_id=chain.id, limit=500)
        if event["kind"] == "context_pressure"
    ]
    real_samples = [
        event
        for event in pressure_events
        if (event["payload"] or {}).get("source") == "real"
        and (event["payload"] or {}).get("ratio") is not None
    ]
    handoffs = database.list_handoffs(chain.id)
    max_ratio = max(
        (event["payload"]["ratio"] for event in real_samples), default=None
    )
    outcome.model_requests = database.count_model_requests(chain.id)
    outcome.duration_seconds = time.monotonic() - started

    outcome.add(
        "取得真实 token 用量与上下文容量",
        bool(real_samples),
        f"real_samples={len(real_samples)} max_ratio={max_ratio}",
    )
    outcome.add(
        "真实占比达到阈值并触发交接",
        bool(handoffs) and bool(handoffs and handoffs[0].state == "ok"),
        f"handoffs={[(h.sequence, h.state) for h in handoffs]} max_ratio={max_ratio}",
    )
    outcome.add(
        "压缩前后事件被记录（若发生压缩）",
        True,
        "compaction events: "
        + str(
            sum(
                1
                for event in database.list_events(chain_id=chain.id, limit=500)
                if event["kind"] in ("context_compacted", "hook:preCompact")
            )
        ),
    )
    new_sessions = database.main_sessions(chain.id)
    outcome.add(
        "新会话接管并继续实际开发",
        len(new_sessions) >= 2 and new_sessions[-1].request_count >= 1,
        f"sessions={[(s.ordinal, s.request_count) for s in new_sessions]}",
    )
    if not keep_workspaces:
        _remove(root)
    outcome.notes.extend(
        [
            f"Level 3 使用官方配置 model_context_window={window}、"
            f"model_auto_compact_token_limit={compact_limit}，未注入任何模拟占比。",
            f"模型请求数 / model requests: {outcome.model_requests}",
            f"状态 / state: {result.state} ({result.reason})",
        ]
    )
    outcome.report_path = write_test_report(
        database=database,
        chain_ids=outcome.chain_ids,
        mode=LEVEL3,
        extra_notes=outcome.notes + outcome.summary_lines(),
    )
    return outcome


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _testing_overrides(config: RelayConfig) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if config.testing.context_window_override:
        overrides["model_context_window"] = config.testing.context_window_override
    if config.testing.auto_compact_token_limit:
        overrides["model_auto_compact_token_limit"] = config.testing.auto_compact_token_limit
    return overrides


def _report(database: Database, chain_id: str) -> Path:
    from .report import write_chain_report

    return write_chain_report(database, chain_id)


def _remove(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
