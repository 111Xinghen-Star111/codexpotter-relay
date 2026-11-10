"""SQLite persistence for CodexPotter Relay.

The database is the authority for a work chain: which chain exists, which main
session currently owns write access, which checkpoint is the newest valid one,
and whether a handoff is mid-flight. Around that core sit Hooks/app-server
events, task results and a model-request counter used to enforce budgets.

Design notes:

* WAL journaling plus a busy timeout, so the app-server reader thread and the
  controller thread can both work without blocking each other.
* One connection per thread (SQLite connections are not thread-safe).
* A ``schema_migrations`` table drives forward-only migrations; every migration
  runs inside a transaction and records its description.
* ``events.event_key`` is unique, which makes hook redelivery idempotent.
* Checkpoints carry a SHA-256 checksum over their meaningful content, so a
  corrupted row can be detected instead of being resumed from.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

#: Chain states. ``handoff`` means a transition is in flight and no session owns
#: write access; ``paused`` and ``stopped`` are terminal for the automatic loop
#: until an operator resumes them.
CHAIN_STATES = (
    "pending",
    "running",
    "handoff",
    "paused",
    "complete",
    "stopped",
    "failed",
)

#: Allowed chain state transitions. Anything else raises, which keeps a crash
#: from silently producing an impossible state.
CHAIN_TRANSITIONS: dict[str, frozenset[str]] = {
    # ``pending -> paused`` lets an operator hold a freshly created chain before
    # the controller ever issues a model request.
    "pending": frozenset({"running", "paused", "stopped", "failed"}),
    "running": frozenset({"handoff", "paused", "stopped", "complete", "failed"}),
    "handoff": frozenset({"running", "failed", "stopped"}),
    # A paused chain may still be handed off deliberately: the operator keeps
    # control of write access without resuming automatic work.
    "paused": frozenset({"running", "handoff", "stopped", "failed"}),
    "complete": frozenset(),
    "stopped": frozenset(),
    "failed": frozenset(),
}

SESSION_STATES = ("starting", "active", "idle", "writing", "handed_off", "ended", "failed")

HANDOFF_PHASES = ("PREPARE", "START", "VERIFY", "COMMIT", "CONTINUE")
HANDOFF_STATES = ("pending", "ok", "failed", "rolled_back")


class DatabaseError(RuntimeError):
    """Raised for invariant violations detected by the persistence layer."""


class TransitionError(DatabaseError):
    """Raised when a chain state transition is not allowed."""


@dataclass(frozen=True)
class Migration:
    """One forward-only schema migration."""

    version: int
    description: str
    statements: Sequence[str]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="initial relay schema",
        statements=(
            """
            CREATE TABLE chains (
                id TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                goal TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('production', 'test')),
                test_level TEXT,
                state TEXT NOT NULL,
                active_session_id TEXT,
                work_branch TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                stop_reason TEXT,
                config_json TEXT NOT NULL,
                simulated INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                chain_id TEXT NOT NULL REFERENCES chains(id) ON DELETE CASCADE,
                ordinal INTEGER,
                role TEXT NOT NULL CHECK (role IN ('main', 'subagent')),
                parent_session_id TEXT,
                model TEXT,
                state TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                last_activity_at REAL,
                token_input INTEGER NOT NULL DEFAULT 0,
                token_cached INTEGER NOT NULL DEFAULT 0,
                token_output INTEGER NOT NULL DEFAULT 0,
                token_total INTEGER NOT NULL DEFAULT 0,
                context_window INTEGER,
                request_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE UNIQUE INDEX sessions_main_ordinal ON sessions(chain_id, ordinal) "
            "WHERE ordinal IS NOT NULL",
            "CREATE INDEX sessions_chain ON sessions(chain_id, role)",
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT,
                session_id TEXT,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                event_key TEXT NOT NULL UNIQUE,
                occurred_at REAL NOT NULL,
                payload_json TEXT NOT NULL,
                simulated INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX events_chain_time ON events(chain_id, occurred_at)",
            """
            CREATE TABLE checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT NOT NULL REFERENCES chains(id) ON DELETE CASCADE,
                session_id TEXT,
                version INTEGER NOT NULL,
                reason TEXT NOT NULL,
                git_head TEXT,
                git_branch TEXT,
                git_dirty INTEGER NOT NULL DEFAULT 0,
                git_status_text TEXT NOT NULL DEFAULT '',
                handoff_sha256 TEXT NOT NULL,
                handoff_md TEXT NOT NULL,
                state_json TEXT NOT NULL,
                checksum TEXT NOT NULL,
                verified_at REAL,
                created_at REAL NOT NULL,
                UNIQUE(chain_id, version)
            )
            """,
            """
            CREATE TABLE handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT NOT NULL REFERENCES chains(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                from_session_id TEXT,
                to_session_id TEXT,
                phase TEXT NOT NULL,
                state TEXT NOT NULL,
                checkpoint_id INTEGER,
                attempt INTEGER NOT NULL DEFAULT 1,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL,
                error TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                simulated INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chain_id, sequence)
            )
            """,
            """
            CREATE TABLE task_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT NOT NULL REFERENCES chains(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence TEXT,
                session_id TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                UNIQUE(chain_id, task_id)
            )
            """,
            """
            CREATE TABLE execution_locks (
                chain_id TEXT PRIMARY KEY REFERENCES chains(id) ON DELETE CASCADE,
                owner_pid INTEGER NOT NULL,
                owner_host TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE model_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT NOT NULL REFERENCES chains(id) ON DELETE CASCADE,
                session_id TEXT,
                kind TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                simulated INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX model_requests_chain ON model_requests(chain_id)",
        ),
    ),
    Migration(
        version=2,
        description="correct the simulated flag on real-session test requests",
        statements=(
            # Level 2/3 chains call the real model; only their *pressure signal*
            # is simulated. Older rows were flagged as simulated requests.
            """
            UPDATE model_requests
               SET simulated = 0
             WHERE chain_id IN (
                   SELECT id FROM chains
                    WHERE test_level IN ('real-handoff', 'real-pressure')
             )
            """,
        ),
    ),
)


def canonical_json(payload: Any) -> str:
    """Serialise ``payload`` deterministically for hashing and storage."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def checkpoint_checksum(
    *,
    chain_id: str,
    version: int,
    git_head: str | None,
    git_branch: str | None,
    handoff_sha256: str,
    state_json: str,
) -> str:
    """Compute the integrity checksum for a checkpoint row."""
    material = canonical_json(
        {
            "chain_id": chain_id,
            "version": version,
            "git_head": git_head,
            "git_branch": git_branch,
            "handoff_sha256": handoff_sha256,
            "state_json": state_json,
        }
    )
    return sha256_text(material)


@dataclass(frozen=True)
class Chain:
    """A work chain: one user objective driven across many main sessions."""

    id: str
    project_root: str
    goal: str
    mode: str
    test_level: str | None
    state: str
    active_session_id: str | None
    work_branch: str
    created_at: float
    updated_at: float
    completed_at: float | None
    stop_reason: str | None
    config_json: str
    simulated: bool


@dataclass(frozen=True)
class Session:
    """One Codex main session (a real thread) inside a chain."""

    id: str
    chain_id: str
    ordinal: int | None
    role: str
    parent_session_id: str | None
    model: str | None
    state: str
    started_at: float
    ended_at: float | None
    last_activity_at: float | None
    token_input: int
    token_cached: int
    token_output: int
    token_total: int
    context_window: int | None
    request_count: int
    metadata_json: str


@dataclass(frozen=True)
class Checkpoint:
    """An immutable, checksummed snapshot of the chain's resumable state."""

    id: int
    chain_id: str
    session_id: str | None
    version: int
    reason: str
    git_head: str | None
    git_branch: str | None
    git_dirty: bool
    git_status_text: str
    handoff_sha256: str
    handoff_md: str
    state_json: str
    checksum: str
    verified_at: float | None
    created_at: float

    def is_intact(self) -> bool:
        """Return True when the stored content still matches its checksums."""
        if sha256_text(self.handoff_md) != self.handoff_sha256:
            return False
        expected = checkpoint_checksum(
            chain_id=self.chain_id,
            version=self.version,
            git_head=self.git_head,
            git_branch=self.git_branch,
            handoff_sha256=self.handoff_sha256,
            state_json=self.state_json,
        )
        return expected == self.checksum


@dataclass(frozen=True)
class Handoff:
    """One PREPARE → START → VERIFY → COMMIT → CONTINUE transition."""

    id: int
    chain_id: str
    sequence: int
    from_session_id: str | None
    to_session_id: str | None
    phase: str
    state: str
    checkpoint_id: int | None
    attempt: int
    started_at: float
    updated_at: float
    finished_at: float | None
    error: str | None
    details_json: str
    simulated: bool


class Database:
    """Thread-safe SQLite access layer with migrations and chain invariants."""

    def __init__(self, path: Path, *, busy_timeout_seconds: float = 10.0) -> None:
        self.path = path
        self._busy_timeout = busy_timeout_seconds
        self._local = threading.local()
        self._migration_lock = threading.Lock()

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path),
                timeout=self._busy_timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={int(self._busy_timeout * 1000)}")
            self._local.connection = connection
        return connection

    def close(self) -> None:
        """Close the current thread's connection, if any."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside an immediate transaction."""
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    # ------------------------------------------------------------------
    # migrations
    # ------------------------------------------------------------------
    def migrate(self) -> int:
        """Apply pending migrations and return the resulting schema version."""
        with self._migration_lock:
            connection = self._connection()
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at REAL NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, description, applied_at) "
                        "VALUES (?, ?, ?)",
                        (migration.version, migration.description, time.time()),
                    )
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                else:
                    connection.execute("COMMIT")
            row = connection.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            return int(row["v"] or 0)

    def schema_version(self) -> int:
        """Return the schema version currently stored in the database."""
        row = self._connection().execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        return int(row["v"] or 0)

    # ------------------------------------------------------------------
    # chains
    # ------------------------------------------------------------------
    def create_chain(
        self,
        *,
        chain_id: str,
        project_root: Path,
        goal: str,
        mode: str,
        work_branch: str,
        config_json: str,
        test_level: str | None = None,
        simulated: bool = False,
    ) -> Chain:
        """Insert a new chain in state ``pending``."""
        if mode not in ("production", "test"):
            raise DatabaseError(f"invalid chain mode: {mode!r}")
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO chains (
                    id, project_root, goal, mode, test_level, state, active_session_id,
                    work_branch, created_at, updated_at, completed_at, stop_reason,
                    config_json, simulated
                ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    chain_id,
                    str(project_root),
                    goal,
                    mode,
                    test_level,
                    work_branch,
                    now,
                    now,
                    config_json,
                    1 if simulated else 0,
                ),
            )
        return self.get_chain(chain_id)

    def get_chain(self, chain_id: str) -> Chain:
        """Return one chain or raise ``KeyError``."""
        row = self._connection().execute(
            "SELECT * FROM chains WHERE id = ?", (chain_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"chain not found: {chain_id}")
        return _row_to_chain(row)

    def find_chain(self, chain_id: str) -> Chain | None:
        """Return one chain or ``None``."""
        row = self._connection().execute(
            "SELECT * FROM chains WHERE id = ?", (chain_id,)
        ).fetchone()
        return _row_to_chain(row) if row else None

    def list_chains(
        self,
        *,
        state: str | None = None,
        mode: str | None = None,
        limit: int = 50,
    ) -> list[Chain]:
        """Return chains newest first, optionally filtered."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if state:
            clauses.append("state = ?")
            parameters.append(state)
        if mode:
            clauses.append("mode = ?")
            parameters.append(mode)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection().execute(
            f"SELECT * FROM chains {where} ORDER BY created_at DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [_row_to_chain(row) for row in rows]

    def update_chain(self, chain_id: str, **fields: Any) -> Chain:
        """Update mutable chain columns and return the refreshed chain."""
        allowed = {
            "state",
            "active_session_id",
            "work_branch",
            "completed_at",
            "stop_reason",
            "project_root",
            "goal",
            "config_json",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise DatabaseError(f"cannot update chain fields: {sorted(unknown)}")
        if not fields:
            return self.get_chain(chain_id)
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE chains SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), time.time(), chain_id),
            )
        return self.get_chain(chain_id)

    def transition_chain(self, chain_id: str, new_state: str, *, reason: str | None = None) -> Chain:
        """Move a chain to ``new_state``, enforcing ``CHAIN_TRANSITIONS``."""
        if new_state not in CHAIN_STATES:
            raise TransitionError(f"unknown chain state: {new_state!r}")
        chain = self.get_chain(chain_id)
        if new_state == chain.state:
            return chain
        allowed = CHAIN_TRANSITIONS.get(chain.state, frozenset())
        if new_state not in allowed:
            raise TransitionError(
                f"chain {chain_id}: illegal transition {chain.state} -> {new_state}"
            )
        fields: dict[str, Any] = {"state": new_state}
        if new_state in ("complete", "stopped", "failed") and chain.completed_at is None:
            fields["completed_at"] = time.time()
        if reason is not None:
            fields["stop_reason"] = reason
        return self.update_chain(chain_id, **fields)

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def create_session(
        self,
        *,
        session_id: str,
        chain_id: str,
        role: str,
        ordinal: int | None = None,
        parent_session_id: str | None = None,
        model: str | None = None,
        state: str = "starting",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Record a Codex session row for the given thread id."""
        if role not in ("main", "subagent"):
            raise DatabaseError(f"invalid session role: {role!r}")
        if state not in SESSION_STATES:
            raise DatabaseError(f"invalid session state: {state!r}")
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, chain_id, ordinal, role, parent_session_id, model, state,
                    started_at, ended_at, last_activity_at, token_input, token_cached,
                    token_output, token_total, context_window, request_count, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, 0, 0, 0, NULL, 0, ?)
                """,
                (
                    session_id,
                    chain_id,
                    ordinal,
                    role,
                    parent_session_id,
                    model,
                    state,
                    now,
                    now,
                    canonical_json(metadata or {}),
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> Session:
        """Return one session or raise ``KeyError``."""
        row = self._connection().execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        return _row_to_session(row)

    def find_session(self, session_id: str) -> Session | None:
        """Return one session or ``None``."""
        row = self._connection().execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, chain_id: str) -> list[Session]:
        """Return all sessions of a chain ordered by main session ordinal."""
        rows = self._connection().execute(
            """
            SELECT * FROM sessions WHERE chain_id = ?
            ORDER BY COALESCE(ordinal, 9999), started_at
            """,
            (chain_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def main_sessions(self, chain_id: str) -> list[Session]:
        """Return only the main (handoff-producing) sessions of a chain."""
        rows = self._connection().execute(
            "SELECT * FROM sessions WHERE chain_id = ? AND role = 'main' ORDER BY ordinal",
            (chain_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        touch: bool = True,
        **fields: Any,
    ) -> Session:
        """Update a session row; unknown columns are rejected."""
        allowed = {
            "state",
            "ended_at",
            "token_input",
            "token_cached",
            "token_output",
            "token_total",
            "context_window",
            "request_count",
            "model",
            "metadata_json",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise DatabaseError(f"cannot update session fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        sets = [assignments] if assignments else []
        if touch:
            sets.append("last_activity_at = ?")
            fields = {**fields, "last_activity_at": time.time()}
        if not sets:
            return self.get_session(session_id)
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",
                (*fields.values(), session_id),
            )
        return self.get_session(session_id)

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def append_event(
        self,
        *,
        event_key: str,
        kind: str,
        source: str,
        payload: dict[str, Any],
        chain_id: str | None = None,
        session_id: str | None = None,
        occurred_at: float | None = None,
        simulated: bool = False,
    ) -> bool:
        """Insert an event idempotently; return True when a new row was written."""
        connection = self._connection()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO events (
                chain_id, session_id, kind, source, event_key, occurred_at,
                payload_json, simulated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain_id,
                session_id,
                kind,
                source,
                event_key,
                occurred_at if occurred_at is not None else time.time(),
                canonical_json(payload),
                1 if simulated else 0,
            ),
        )
        return cursor.rowcount > 0

    def list_events(
        self,
        *,
        chain_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent events newest first."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if chain_id:
            clauses.append("chain_id = ?")
            parameters.append(chain_id)
        if kind:
            clauses.append("kind = ?")
            parameters.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection().execute(
            f"SELECT * FROM events {where} ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "chain_id": row["chain_id"],
                "session_id": row["session_id"],
                "kind": row["kind"],
                "source": row["source"],
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
                "simulated": bool(row["simulated"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # checkpoints
    # ------------------------------------------------------------------
    def create_checkpoint(
        self,
        *,
        chain_id: str,
        reason: str,
        handoff_md: str,
        state_json: str,
        git_head: str | None,
        git_branch: str | None,
        git_dirty: bool,
        git_status_text: str,
        session_id: str | None,
    ) -> Checkpoint:
        """Store the next version of the chain checkpoint."""
        handoff_sha = sha256_text(handoff_md)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM checkpoints WHERE chain_id = ?",
                (chain_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            checksum = checkpoint_checksum(
                chain_id=chain_id,
                version=version,
                git_head=git_head,
                git_branch=git_branch,
                handoff_sha256=handoff_sha,
                state_json=state_json,
            )
            cursor = connection.execute(
                """
                INSERT INTO checkpoints (
                    chain_id, session_id, version, reason, git_head, git_branch,
                    git_dirty, git_status_text, handoff_sha256, handoff_md,
                    state_json, checksum, verified_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    chain_id,
                    session_id,
                    version,
                    reason,
                    git_head,
                    git_branch,
                    1 if git_dirty else 0,
                    git_status_text,
                    handoff_sha,
                    handoff_md,
                    state_json,
                    checksum,
                    time.time(),
                ),
            )
            checkpoint_id = int(cursor.lastrowid or 0)
        return self.get_checkpoint(checkpoint_id)

    def get_checkpoint(self, checkpoint_id: int) -> Checkpoint:
        """Return one checkpoint row or raise ``KeyError``."""
        row = self._connection().execute(
            "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"checkpoint not found: {checkpoint_id}")
        return _row_to_checkpoint(row)

    def latest_checkpoint(self, chain_id: str) -> Checkpoint | None:
        """Return the newest checkpoint regardless of integrity."""
        row = self._connection().execute(
            "SELECT * FROM checkpoints WHERE chain_id = ? ORDER BY version DESC LIMIT 1",
            (chain_id,),
        ).fetchone()
        return _row_to_checkpoint(row) if row else None

    def latest_valid_checkpoint(self, chain_id: str) -> Checkpoint | None:
        """Return the newest checkpoint whose checksum still verifies."""
        rows = self._connection().execute(
            "SELECT * FROM checkpoints WHERE chain_id = ? ORDER BY version DESC",
            (chain_id,),
        ).fetchall()
        for row in rows:
            checkpoint = _row_to_checkpoint(row)
            if checkpoint.is_intact():
                return checkpoint
        return None

    def mark_checkpoint_verified(self, checkpoint_id: int) -> None:
        """Record that a checkpoint was successfully used to resume a chain."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE checkpoints SET verified_at = ? WHERE id = ?",
                (time.time(), checkpoint_id),
            )

    def list_checkpoints(self, chain_id: str, limit: int = 50) -> list[Checkpoint]:
        """Return checkpoints newest first."""
        rows = self._connection().execute(
            "SELECT * FROM checkpoints WHERE chain_id = ? ORDER BY version DESC LIMIT ?",
            (chain_id, limit),
        ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    # ------------------------------------------------------------------
    # handoffs
    # ------------------------------------------------------------------
    def create_handoff(
        self,
        *,
        chain_id: str,
        sequence: int,
        from_session_id: str | None,
        checkpoint_id: int | None,
        simulated: bool = False,
    ) -> Handoff:
        """Create the handoff record for ``sequence`` in phase PREPARE."""
        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO handoffs (
                    chain_id, sequence, from_session_id, to_session_id, phase, state,
                    checkpoint_id, attempt, started_at, updated_at, finished_at,
                    error, details_json, simulated
                ) VALUES (?, ?, ?, NULL, 'PREPARE', 'pending', ?, 1, ?, ?, NULL, NULL, '{}', ?)
                """,
                (
                    chain_id,
                    sequence,
                    from_session_id,
                    checkpoint_id,
                    now,
                    now,
                    1 if simulated else 0,
                ),
            )
            handoff_id = int(cursor.lastrowid or 0)
        return self.get_handoff(handoff_id)

    def get_handoff(self, handoff_id: int) -> Handoff:
        """Return one handoff row or raise ``KeyError``."""
        row = self._connection().execute(
            "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"handoff not found: {handoff_id}")
        return _row_to_handoff(row)

    def latest_handoff(self, chain_id: str) -> Handoff | None:
        """Return the newest handoff of a chain."""
        row = self._connection().execute(
            "SELECT * FROM handoffs WHERE chain_id = ? ORDER BY sequence DESC LIMIT 1",
            (chain_id,),
        ).fetchone()
        return _row_to_handoff(row) if row else None

    def list_handoffs(self, chain_id: str) -> list[Handoff]:
        """Return all handoffs of a chain ordered by sequence."""
        rows = self._connection().execute(
            "SELECT * FROM handoffs WHERE chain_id = ? ORDER BY sequence",
            (chain_id,),
        ).fetchall()
        return [_row_to_handoff(row) for row in rows]

    def update_handoff(
        self,
        handoff_id: int,
        *,
        phase: str | None = None,
        state: str | None = None,
        to_session_id: str | None = None,
        checkpoint_id: int | None = None,
        attempt: int | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> Handoff:
        """Advance a handoff through its phases."""
        fields: dict[str, Any] = {}
        if phase is not None:
            if phase not in HANDOFF_PHASES:
                raise DatabaseError(f"invalid handoff phase: {phase!r}")
            fields["phase"] = phase
        if state is not None:
            if state not in HANDOFF_STATES:
                raise DatabaseError(f"invalid handoff state: {state!r}")
            fields["state"] = state
        if to_session_id is not None:
            fields["to_session_id"] = to_session_id
        if checkpoint_id is not None:
            fields["checkpoint_id"] = checkpoint_id
        if attempt is not None:
            fields["attempt"] = attempt
        if error is not None:
            fields["error"] = error
        if details is not None:
            fields["details_json"] = canonical_json(details)
        fields["updated_at"] = time.time()
        if finished:
            fields["finished_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE handoffs SET {assignments} WHERE id = ?",
                (*fields.values(), handoff_id),
            )
        return self.get_handoff(handoff_id)

    # ------------------------------------------------------------------
    # task results
    # ------------------------------------------------------------------
    def upsert_task(
        self,
        *,
        chain_id: str,
        task_id: str,
        title: str,
        status: str,
        evidence: str | None = None,
        session_id: str | None = None,
        verified: bool = False,
    ) -> None:
        """Insert or update one task entry of the chain ledger."""
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_results (
                    chain_id, task_id, title, status, evidence, session_id, verified, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, task_id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    evidence = COALESCE(excluded.evidence, task_results.evidence),
                    session_id = COALESCE(excluded.session_id, task_results.session_id),
                    verified = excluded.verified,
                    updated_at = excluded.updated_at
                """,
                (
                    chain_id,
                    task_id,
                    title,
                    status,
                    evidence,
                    session_id,
                    1 if verified else 0,
                    time.time(),
                ),
            )

    def list_tasks(self, chain_id: str) -> list[dict[str, Any]]:
        """Return task results ordered by task id."""
        rows = self._connection().execute(
            "SELECT * FROM task_results WHERE chain_id = ? ORDER BY task_id",
            (chain_id,),
        ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "title": row["title"],
                "status": row["status"],
                "evidence": row["evidence"],
                "session_id": row["session_id"],
                "verified": bool(row["verified"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # model request budget
    # ------------------------------------------------------------------
    def record_model_request(
        self, *, chain_id: str, session_id: str | None, kind: str = "turn", simulated: bool = False
    ) -> int:
        """Count one model request and return the new chain-level total."""
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO model_requests (chain_id, session_id, kind, started_at, simulated) "
                "VALUES (?, ?, ?, ?, ?)",
                (chain_id, session_id, kind, now, 1 if simulated else 0),
            )
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM model_requests WHERE chain_id = ?", (chain_id,)
            ).fetchone()
        return int(row["c"])

    def count_model_requests(self, chain_id: str, *, simulated: bool | None = None) -> int:
        """Return how many model requests the chain has issued so far.

        ``simulated=None`` counts every request, ``simulated=False`` counts only
        requests backed by a real model call and ``simulated=True`` counts only
        injected/mock ones.
        """
        if simulated is None:
            row = self._connection().execute(
                "SELECT COUNT(*) AS c FROM model_requests WHERE chain_id = ?", (chain_id,)
            ).fetchone()
        else:
            row = self._connection().execute(
                "SELECT COUNT(*) AS c FROM model_requests WHERE chain_id = ? AND simulated = ?",
                (chain_id, 1 if simulated else 0),
            ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # execution locks (single writer per chain)
    # ------------------------------------------------------------------
    def acquire_lock(self, chain_id: str, *, pid: int | None = None) -> bool:
        """Try to become the single writer of a chain."""
        owner_pid = pid if pid is not None else os.getpid()
        host = socket.gethostname()
        now = time.time()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM execution_locks WHERE chain_id = ?", (chain_id,)
            ).fetchone()
            if row is not None:
                if not _lock_is_stale(row, host):
                    return row["owner_pid"] == owner_pid and row["owner_host"] == host
                connection.execute(
                    "DELETE FROM execution_locks WHERE chain_id = ?", (chain_id,)
                )
            connection.execute(
                "INSERT INTO execution_locks (chain_id, owner_pid, owner_host, acquired_at, "
                "heartbeat_at) VALUES (?, ?, ?, ?, ?)",
                (chain_id, owner_pid, host, now, now),
            )
        return True

    def heartbeat_lock(self, chain_id: str, *, pid: int | None = None) -> bool:
        """Refresh the lock heartbeat; returns False when the lock was lost."""
        owner_pid = pid if pid is not None else os.getpid()
        host = socket.gethostname()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE execution_locks SET heartbeat_at = ? "
                "WHERE chain_id = ? AND owner_pid = ? AND owner_host = ?",
                (time.time(), chain_id, owner_pid, host),
            )
            return cursor.rowcount > 0

    def release_lock(self, chain_id: str, *, pid: int | None = None) -> None:
        """Drop the lock if this process owns it."""
        owner_pid = pid if pid is not None else os.getpid()
        host = socket.gethostname()
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM execution_locks WHERE chain_id = ? AND owner_pid = ? AND owner_host = ?",
                (chain_id, owner_pid, host),
            )

    def lock_owner(self, chain_id: str) -> dict[str, Any] | None:
        """Return the current lock owner, if any."""
        row = self._connection().execute(
            "SELECT * FROM execution_locks WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "owner_pid": row["owner_pid"],
            "owner_host": row["owner_host"],
            "acquired_at": row["acquired_at"],
            "heartbeat_at": row["heartbeat_at"],
        }

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    def chains_needing_recovery(self) -> list[Chain]:
        """Return chains that were mid-flight when the controller stopped."""
        rows = self._connection().execute(
            "SELECT * FROM chains WHERE state IN ('running', 'handoff') "
            "ORDER BY updated_at"
        ).fetchall()
        return [_row_to_chain(row) for row in rows]


def _lock_is_stale(row: sqlite3.Row, host: str) -> bool:
    """Return True when the lock holder is on another host or a dead process."""
    if row["owner_host"] != host:
        return False
    return not _process_alive(int(row["owner_pid"]))


def _process_alive(pid: int) -> bool:
    """Return True when ``pid`` belongs to a live process on this host."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _row_to_chain(row: sqlite3.Row) -> Chain:
    return Chain(
        id=row["id"],
        project_root=row["project_root"],
        goal=row["goal"],
        mode=row["mode"],
        test_level=row["test_level"],
        state=row["state"],
        active_session_id=row["active_session_id"],
        work_branch=row["work_branch"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        stop_reason=row["stop_reason"],
        config_json=row["config_json"],
        simulated=bool(row["simulated"]),
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        chain_id=row["chain_id"],
        ordinal=row["ordinal"],
        role=row["role"],
        parent_session_id=row["parent_session_id"],
        model=row["model"],
        state=row["state"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        last_activity_at=row["last_activity_at"],
        token_input=row["token_input"],
        token_cached=row["token_cached"],
        token_output=row["token_output"],
        token_total=row["token_total"],
        context_window=row["context_window"],
        request_count=row["request_count"],
        metadata_json=row["metadata_json"],
    )


def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        id=row["id"],
        chain_id=row["chain_id"],
        session_id=row["session_id"],
        version=row["version"],
        reason=row["reason"],
        git_head=row["git_head"],
        git_branch=row["git_branch"],
        git_dirty=bool(row["git_dirty"]),
        git_status_text=row["git_status_text"],
        handoff_sha256=row["handoff_sha256"],
        handoff_md=row["handoff_md"],
        state_json=row["state_json"],
        checksum=row["checksum"],
        verified_at=row["verified_at"],
        created_at=row["created_at"],
    )


def _row_to_handoff(row: sqlite3.Row) -> Handoff:
    return Handoff(
        id=row["id"],
        chain_id=row["chain_id"],
        sequence=row["sequence"],
        from_session_id=row["from_session_id"],
        to_session_id=row["to_session_id"],
        phase=row["phase"],
        state=row["state"],
        checkpoint_id=row["checkpoint_id"],
        attempt=row["attempt"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        details_json=row["details_json"],
        simulated=bool(row["simulated"]),
    )
