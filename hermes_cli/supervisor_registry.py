"""Persistent control-plane registry for a lightweight Hermes supervisor.

Hermes owns role contracts, routing policy, Kanban provenance and compact
execution receipts.  Domain work is performed by registered executors.  Role
shells are immutable, versioned contracts; executors and bindings are mutable
operational records.  One executor can serve many shells and one shell can
have many candidate executors.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from hermes_cli.sqlite_util import add_column_if_missing, write_txn


class SupervisorRegistryError(RuntimeError):
    """Base error for invalid supervisor registry operations."""


class NoEligibleExecutor(SupervisorRegistryError):
    """Raised when an active role shell has no safe executor binding."""


class ReceiptValidationError(SupervisorRegistryError):
    """Raised when a bound run tries to close without a valid receipt."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS role_shells (
    id                    TEXT PRIMARY KEY,
    shell_key             TEXT NOT NULL,
    version               INTEGER NOT NULL,
    supersedes_shell_id   TEXT,
    name                  TEXT NOT NULL,
    description           TEXT,
    contract_json         TEXT NOT NULL,
    contract_hash         TEXT NOT NULL,
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    allowed_capabilities  TEXT NOT NULL DEFAULT '[]',
    evidence_policy       TEXT NOT NULL DEFAULT '{}',
    created_at            INTEGER NOT NULL,
    UNIQUE(shell_key, version),
    FOREIGN KEY(supersedes_shell_id) REFERENCES role_shells(id)
);

CREATE TABLE IF NOT EXISTS role_shell_heads (
    shell_key  TEXT PRIMARY KEY,
    shell_id   TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(shell_id) REFERENCES role_shells(id)
);

CREATE TABLE IF NOT EXISTS executors (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    adapter_type          TEXT NOT NULL,
    description           TEXT,
    launch_config         TEXT NOT NULL DEFAULT '{}',
    capabilities          TEXT NOT NULL DEFAULT '[]',
    capacity              INTEGER NOT NULL DEFAULT 1,
    heartbeat_required    INTEGER NOT NULL DEFAULT 1,
    heartbeat_ttl_seconds INTEGER NOT NULL DEFAULT 300,
    last_heartbeat_at     INTEGER,
    health_state          TEXT NOT NULL DEFAULT 'unknown',
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS role_bindings (
    id               TEXT PRIMARY KEY,
    shell_id         TEXT NOT NULL,
    executor_id      TEXT NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 0,
    weight           REAL NOT NULL DEFAULT 1.0,
    capability_cap   TEXT NOT NULL DEFAULT '[]',
    constraints_json TEXT NOT NULL DEFAULT '{}',
    responsibility   TEXT NOT NULL DEFAULT 'candidate',
    assignment_note  TEXT,
    assigned_by      TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_selected_at INTEGER,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE(shell_id, executor_id),
    FOREIGN KEY(shell_id) REFERENCES role_shells(id),
    FOREIGN KEY(executor_id) REFERENCES executors(id)
);

CREATE TABLE IF NOT EXISTS adapter_overrides (
    id             TEXT PRIMARY KEY,
    scope_type     TEXT NOT NULL,
    scope_key      TEXT NOT NULL,
    executor_id    TEXT NOT NULL,
    mode           TEXT NOT NULL,
    expires_at     INTEGER,
    remaining_uses INTEGER,
    reason         TEXT,
    created_by     TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    CHECK(scope_type IN ('task','shell','all')),
    CHECK(mode IN ('once','temporary','permanent')),
    FOREIGN KEY(executor_id) REFERENCES executors(id)
);

CREATE TABLE IF NOT EXISTS controller_adapters (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    provider              TEXT NOT NULL,
    model                 TEXT NOT NULL,
    base_url              TEXT,
    api_mode              TEXT,
    reasoning_effort      TEXT NOT NULL DEFAULT 'medium',
    key_env               TEXT,
    health_url            TEXT,
    fallback_adapter_id   TEXT,
    description           TEXT,
    metadata_json         TEXT NOT NULL DEFAULT '{}',
    health_state          TEXT NOT NULL DEFAULT 'unknown',
    last_health_at        INTEGER,
    enabled               INTEGER NOT NULL DEFAULT 0,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    FOREIGN KEY(fallback_adapter_id) REFERENCES controller_adapters(id)
);

CREATE TABLE IF NOT EXISTS controller_overrides (
    id                    TEXT PRIMARY KEY,
    scope_type            TEXT NOT NULL,
    scope_key             TEXT NOT NULL,
    controller_adapter_id TEXT NOT NULL,
    mode                  TEXT NOT NULL,
    expires_at            INTEGER,
    remaining_uses        INTEGER,
    reason                TEXT,
    created_by            TEXT,
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    CHECK(scope_type IN ('session','all')),
    CHECK(mode IN ('once','temporary','permanent')),
    FOREIGN KEY(controller_adapter_id) REFERENCES controller_adapters(id)
);

CREATE TABLE IF NOT EXISTS adapter_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    scope_type  TEXT,
    scope_key   TEXT,
    executor_id TEXT,
    binding_id  TEXT,
    override_id TEXT,
    task_id     TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_by  TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_receipts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL UNIQUE,
    task_id           TEXT NOT NULL,
    role_shell_id     TEXT NOT NULL,
    executor_id       TEXT NOT NULL,
    binding_id        TEXT NOT NULL,
    status            TEXT NOT NULL,
    receipt_json      TEXT NOT NULL,
    validation_error  TEXT,
    created_at        INTEGER NOT NULL,
    FOREIGN KEY(run_id) REFERENCES task_runs(id),
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(role_shell_id) REFERENCES role_shells(id),
    FOREIGN KEY(executor_id) REFERENCES executors(id),
    FOREIGN KEY(binding_id) REFERENCES role_bindings(id)
);

CREATE TABLE IF NOT EXISTS task_recovery_sources (
    recovery_task_id TEXT NOT NULL,
    source_task_id   TEXT NOT NULL,
    relation         TEXT NOT NULL DEFAULT 'result_recovery',
    created_by       TEXT,
    created_at       INTEGER NOT NULL,
    PRIMARY KEY(recovery_task_id, source_task_id),
    FOREIGN KEY(recovery_task_id) REFERENCES tasks(id),
    FOREIGN KEY(source_task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_role_shells_key
    ON role_shells(shell_key, version);
CREATE INDEX IF NOT EXISTS idx_role_bindings_shell
    ON role_bindings(shell_id, enabled, priority);
CREATE INDEX IF NOT EXISTS idx_role_bindings_executor
    ON role_bindings(executor_id, enabled);
CREATE INDEX IF NOT EXISTS idx_adapter_overrides_scope
    ON adapter_overrides(scope_type, scope_key, enabled, created_at);
CREATE INDEX IF NOT EXISTS idx_adapter_events_scope
    ON adapter_events(scope_type, scope_key, created_at);
CREATE INDEX IF NOT EXISTS idx_adapter_events_task
    ON adapter_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_controller_overrides_scope
    ON controller_overrides(scope_type, scope_key, enabled, created_at);
CREATE INDEX IF NOT EXISTS idx_run_receipts_task
    ON run_receipts(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_recovery_sources_source
    ON task_recovery_sources(source_task_id, created_at);

CREATE TRIGGER IF NOT EXISTS role_shells_immutable_update
BEFORE UPDATE ON role_shells
BEGIN
    SELECT RAISE(ABORT, 'role shell versions are immutable; create a new version');
END;

CREATE TRIGGER IF NOT EXISTS role_shells_immutable_delete
BEFORE DELETE ON role_shells
BEGIN
    SELECT RAISE(ABORT, 'role shell versions are immutable');
END;
"""


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _capabilities(value: Iterable[str] | None) -> list[str]:
    return sorted({str(item).strip() for item in (value or ()) if str(item).strip()})


def supervisor_root_enabled(
    config: dict[str, Any],
    *,
    environ: Optional[dict[str, str]] = None,
) -> bool:
    """Return true only for the central root, never a bound executor child."""
    env = os.environ if environ is None else environ
    return bool(
        (config.get("supervisor") or {}).get("enabled", False)
        and not str(env.get("HERMES_ROLE_SHELL_ID") or "").strip()
    )


def timeline_goal_id(task_id: str, run_id: int) -> str:
    """Stable Timeline goal identity stamped by the dispatcher for one attempt."""
    return f"hermes-task-{task_id}-run-{int(run_id)}"


def canonical_task_recovery_sources(
    conn: sqlite3.Connection,
    source_task_ids: Iterable[str],
) -> list[str]:
    """Flatten recovery-of-recovery lineage to its original source cards.

    Recovery cards are operational attempts, not new business requests.  A
    verifier asked to recover an existing recovery card must therefore inherit
    that card's original sources instead of starting an unbounded chain.
    """
    ensure_schema(conn)
    roots: list[str] = []

    def visit(task_id: str, path: tuple[str, ...]) -> None:
        if task_id in path:
            cycle = " -> ".join((*path, task_id))
            raise SupervisorRegistryError(
                f"cyclic task recovery lineage detected: {cycle}"
            )
        rows = conn.execute(
            "SELECT source_task_id FROM task_recovery_sources "
            "WHERE recovery_task_id=? ORDER BY created_at,source_task_id",
            (task_id,),
        ).fetchall()
        if not rows:
            if task_id not in roots:
                roots.append(task_id)
            return
        for row in rows:
            visit(str(row["source_task_id"]), (*path, task_id))

    cleaned: list[str] = []
    for raw in source_task_ids:
        source_id = str(raw or "").strip()
        if source_id and source_id not in cleaned:
            cleaned.append(source_id)
    for source_id in cleaned:
        visit(source_id, ())
    return roots


def find_existing_recovery_task(
    conn: sqlite3.Connection,
    *,
    source_task_ids: Iterable[str],
    role_shell_id: str,
    session_id: Optional[str],
) -> Optional[dict[str, Any]]:
    """Return the canonical card already recovering the same source set.

    Active attempts win, followed by a blocked card that can be reopened, then
    a completed result.  Archived cards are intentionally ignored.
    """
    ensure_schema(conn)
    canonical_sources = canonical_task_recovery_sources(conn, source_task_ids)
    if not canonical_sources:
        return None
    wanted = set(canonical_sources)
    normalized_session = str(session_id or "").strip() or None
    rows = conn.execute(
        "SELECT DISTINCT t.id,t.status,t.created_at,t.session_id,t.role_shell_id "
        "FROM tasks t JOIN task_recovery_sources rs ON rs.recovery_task_id=t.id "
        "WHERE t.role_shell_id=? AND t.status!='archived' "
        "AND ((t.session_id=? ) OR (t.session_id IS NULL AND ? IS NULL)) "
        "ORDER BY CASE t.status "
        "WHEN 'running' THEN 0 WHEN 'ready' THEN 1 WHEN 'todo' THEN 2 "
        "WHEN 'scheduled' THEN 3 WHEN 'blocked' THEN 4 WHEN 'done' THEN 5 "
        "ELSE 6 END,t.created_at,t.id",
        (str(role_shell_id), normalized_session, normalized_session),
    ).fetchall()
    for row in rows:
        direct_sources = [
            item["source_task_id"]
            for item in list_task_recovery_sources(conn, str(row["id"]))
        ]
        if set(canonical_task_recovery_sources(conn, direct_sources)) == wanted:
            result = dict(row)
            result["source_task_ids"] = canonical_sources
            return result
    return None


def register_task_recovery_sources(
    conn: sqlite3.Connection,
    *,
    recovery_task_id: str,
    source_task_ids: Iterable[str],
    created_by: Optional[str] = None,
) -> list[str]:
    """Record non-blocking result-recovery lineage between Kanban cards.

    ``task_links`` is a dependency graph: a blocked source would park its child
    in ``todo`` forever. Recovery lineage is intentionally separate so a
    verifier can run immediately while retaining an auditable source relation.
    """
    ensure_schema(conn)
    recovery_task_id = str(recovery_task_id or "").strip()
    cleaned: list[str] = []
    for raw in source_task_ids:
        source_id = str(raw or "").strip()
        if source_id and source_id not in cleaned:
            cleaned.append(source_id)
    if not recovery_task_id:
        raise SupervisorRegistryError("recovery_task_id is required")
    if not cleaned:
        return []
    existing = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM tasks WHERE id IN ("
            + ",".join("?" for _ in [recovery_task_id, *cleaned])
            + ")",
            [recovery_task_id, *cleaned],
        ).fetchall()
    }
    missing = [
        task_id
        for task_id in [recovery_task_id, *cleaned]
        if task_id not in existing
    ]
    if missing:
        raise SupervisorRegistryError(
            "unknown recovery task(s): " + ", ".join(missing)
        )
    if recovery_task_id in cleaned:
        raise SupervisorRegistryError("a recovery card cannot reference itself")
    cleaned = canonical_task_recovery_sources(conn, cleaned)
    if recovery_task_id in cleaned:
        raise SupervisorRegistryError("a recovery card cannot reference itself")

    from hermes_cli import kanban_db as kb

    with write_txn(conn):
        for source_id in cleaned:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO task_recovery_sources "
                "(recovery_task_id,source_task_id,relation,created_by,created_at) "
                "VALUES (?,?,'result_recovery',?,?)",
                (recovery_task_id, source_id, created_by, _now()),
            )
            if inserted.rowcount != 1:
                continue
            kb._append_event(
                conn,
                source_id,
                "result_recovery_requested",
                {
                    "recovery_task_id": recovery_task_id,
                    "created_by": created_by,
                },
            )
            kb._append_event(
                conn,
                recovery_task_id,
                "result_recovery_source_linked",
                {
                    "source_task_id": source_id,
                    "created_by": created_by,
                },
            )
    return cleaned


def list_task_recovery_sources(
    conn: sqlite3.Connection, recovery_task_id: str
) -> list[dict[str, Any]]:
    """Return auditable non-blocking source cards for one recovery card."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT rs.source_task_id,rs.relation,rs.created_by,rs.created_at,"
        "t.title,t.status,t.result,t.last_failure_error "
        "FROM task_recovery_sources rs "
        "JOIN tasks t ON t.id=rs.source_task_id "
        "WHERE rs.recovery_task_id=? ORDER BY rs.created_at,rs.source_task_id",
        (str(recovery_task_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def terminalize_recovery_sources_in_txn(
    conn: sqlite3.Connection,
    *,
    recovery_task_id: str,
) -> list[str]:
    """Archive blocked source cards after a recovery result is committed.

    This helper deliberately assumes the caller already owns the Kanban write
    transaction.  Recovery lineage is audit history, while the blocked source
    card is an obsolete execution attempt once its verified replacement has
    completed.  Archiving it keeps the board terminal without deleting any
    comments, runs, receipts, or lineage.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='task_recovery_sources'"
    ).fetchone()
    if table_exists is None:
        return []

    rows = conn.execute(
        "SELECT source_task_id FROM task_recovery_sources "
        "WHERE recovery_task_id=? ORDER BY created_at,source_task_id",
        (str(recovery_task_id),),
    ).fetchall()
    if not rows:
        return []

    from hermes_cli import kanban_db as kb

    archived: list[str] = []
    for row in rows:
        source_id = str(row["source_task_id"])
        cur = conn.execute(
            "UPDATE tasks SET status='archived', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL "
            "WHERE id=? AND status='blocked'",
            (source_id,),
        )
        if cur.rowcount != 1:
            continue
        source_run_id = kb._end_run(
            conn,
            source_id,
            outcome="reclaimed",
            status="reclaimed",
            summary=f"superseded by completed recovery {recovery_task_id}",
        )
        kb._append_event(
            conn,
            source_id,
            "result_recovery_superseded",
            {"recovery_task_id": recovery_task_id},
            run_id=source_run_id,
        )
        finalize_task_once_overrides_in_txn(
            conn,
            task_id=source_id,
            terminal_status="archived",
        )
        archived.append(source_id)

    if archived:
        kb._append_event(
            conn,
            recovery_task_id,
            "result_recovery_sources_terminalized",
            {"source_task_ids": archived},
        )
    return archived


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Install the additive supervisor schema on an initialized Kanban DB."""
    conn.executescript(_SCHEMA)
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    # Some legacy migration tests intentionally exercise a pre-task_runs schema.
    # Keep this extension additive: enrich canonical tables only after they exist.
    if "tasks" in tables:
        task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "role_shell_id" not in task_cols:
            add_column_if_missing(conn, "tasks", "role_shell_id", "role_shell_id TEXT")
        if "status" in task_cols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_role_shell "
                "ON tasks(role_shell_id, status)"
            )
    if "task_runs" in tables:
        run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
        for name in ("role_shell_id", "executor_id", "binding_id"):
            if name not in run_cols:
                add_column_if_missing(conn, "task_runs", name, f"{name} TEXT")
        if "receipt_id" not in run_cols:
            add_column_if_missing(conn, "task_runs", "receipt_id", "receipt_id INTEGER")
        if "adapter_override_id" not in run_cols:
            add_column_if_missing(
                conn,
                "task_runs",
                "adapter_override_id",
                "adapter_override_id TEXT",
            )
        if {"status", "ended_at"}.issubset(run_cols):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_executor_active "
                "ON task_runs(executor_id, status, ended_at)"
            )
    binding_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(role_bindings)")
    }
    if "responsibility" not in binding_cols:
        add_column_if_missing(
            conn,
            "role_bindings",
            "responsibility",
            "responsibility TEXT NOT NULL DEFAULT 'candidate'",
        )
    if "assignment_note" not in binding_cols:
        add_column_if_missing(
            conn, "role_bindings", "assignment_note", "assignment_note TEXT"
        )
    if "assigned_by" not in binding_cols:
        add_column_if_missing(
            conn, "role_bindings", "assigned_by", "assigned_by TEXT"
        )


@dataclass(frozen=True)
class RoleShell:
    id: str
    shell_key: str
    version: int
    supersedes_shell_id: Optional[str]
    name: str
    description: Optional[str]
    contract: dict[str, Any]
    contract_hash: str
    required_capabilities: list[str]
    allowed_capabilities: list[str]
    evidence_policy: dict[str, Any]
    created_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RoleShell":
        return cls(
            id=row["id"],
            shell_key=row["shell_key"],
            version=int(row["version"]),
            supersedes_shell_id=row["supersedes_shell_id"],
            name=row["name"],
            description=row["description"],
            contract=_json_dict(row["contract_json"]),
            contract_hash=row["contract_hash"],
            required_capabilities=_json_list(row["required_capabilities"]),
            allowed_capabilities=_json_list(row["allowed_capabilities"]),
            evidence_policy=_json_dict(row["evidence_policy"]),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True)
class Executor:
    id: str
    name: str
    adapter_type: str
    description: Optional[str]
    launch_config: dict[str, Any]
    capabilities: list[str]
    capacity: int
    heartbeat_required: bool
    heartbeat_ttl_seconds: int
    last_heartbeat_at: Optional[int]
    health_state: str
    enabled: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Executor":
        return cls(
            id=row["id"],
            name=row["name"],
            adapter_type=row["adapter_type"],
            description=row["description"],
            launch_config=_json_dict(row["launch_config"]),
            capabilities=_json_list(row["capabilities"]),
            capacity=max(1, int(row["capacity"])),
            heartbeat_required=bool(row["heartbeat_required"]),
            heartbeat_ttl_seconds=max(1, int(row["heartbeat_ttl_seconds"])),
            last_heartbeat_at=(
                int(row["last_heartbeat_at"])
                if row["last_heartbeat_at"] is not None else None
            ),
            health_state=row["health_state"],
            enabled=bool(row["enabled"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class Binding:
    id: str
    shell_id: str
    executor_id: str
    priority: int
    weight: float
    capability_cap: list[str]
    constraints: dict[str, Any]
    responsibility: str
    assignment_note: Optional[str]
    assigned_by: Optional[str]
    enabled: bool
    last_selected_at: Optional[int]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Binding":
        return cls(
            id=row["id"],
            shell_id=row["shell_id"],
            executor_id=row["executor_id"],
            priority=int(row["priority"]),
            weight=float(row["weight"]),
            capability_cap=_json_list(row["capability_cap"]),
            constraints=_json_dict(row["constraints_json"]),
            responsibility=(
                str(row["responsibility"])
                if "responsibility" in row.keys()
                else "candidate"
            ),
            assignment_note=(
                row["assignment_note"] if "assignment_note" in row.keys() else None
            ),
            assigned_by=(row["assigned_by"] if "assigned_by" in row.keys() else None),
            enabled=bool(row["enabled"]),
            last_selected_at=(
                int(row["last_selected_at"])
                if row["last_selected_at"] is not None else None
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class Selection:
    shell: RoleShell
    executor: Executor
    binding: Binding
    effective_capabilities: list[str]
    active_runs: int
    adapter_override_id: Optional[str] = None


@dataclass(frozen=True)
class AdapterOverride:
    id: str
    scope_type: str
    scope_key: str
    executor_id: str
    mode: str
    expires_at: Optional[int]
    remaining_uses: Optional[int]
    reason: Optional[str]
    created_by: Optional[str]
    enabled: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AdapterOverride":
        return cls(
            id=row["id"],
            scope_type=row["scope_type"],
            scope_key=row["scope_key"],
            executor_id=row["executor_id"],
            mode=row["mode"],
            expires_at=(
                int(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            remaining_uses=(
                int(row["remaining_uses"])
                if row["remaining_uses"] is not None
                else None
            ),
            reason=row["reason"],
            created_by=row["created_by"],
            enabled=bool(row["enabled"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def active(self, *, now: Optional[int] = None) -> bool:
        current = _now() if now is None else int(now)
        return bool(
            self.enabled
            and (self.expires_at is None or self.expires_at > current)
            and (self.remaining_uses is None or self.remaining_uses > 0)
        )


@dataclass(frozen=True)
class ControllerAdapter:
    id: str
    name: str
    provider: str
    model: str
    base_url: Optional[str]
    api_mode: Optional[str]
    reasoning_effort: str
    key_env: Optional[str]
    health_url: Optional[str]
    fallback_adapter_id: Optional[str]
    description: Optional[str]
    metadata: dict[str, Any]
    health_state: str
    last_health_at: Optional[int]
    enabled: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ControllerAdapter":
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            base_url=(str(row["base_url"]) if row["base_url"] else None),
            api_mode=(str(row["api_mode"]) if row["api_mode"] else None),
            reasoning_effort=str(row["reasoning_effort"] or "medium"),
            key_env=(str(row["key_env"]) if row["key_env"] else None),
            health_url=(str(row["health_url"]) if row["health_url"] else None),
            fallback_adapter_id=(
                str(row["fallback_adapter_id"])
                if row["fallback_adapter_id"] else None
            ),
            description=(str(row["description"]) if row["description"] else None),
            metadata=_json_dict(row["metadata_json"]),
            health_state=str(row["health_state"] or "unknown"),
            last_health_at=(
                int(row["last_health_at"])
                if row["last_health_at"] is not None else None
            ),
            enabled=bool(row["enabled"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def routable(self) -> bool:
        return bool(self.enabled and self.health_state == "healthy")


@dataclass(frozen=True)
class ControllerOverride:
    id: str
    scope_type: str
    scope_key: str
    controller_adapter_id: str
    mode: str
    expires_at: Optional[int]
    remaining_uses: Optional[int]
    reason: Optional[str]
    created_by: Optional[str]
    enabled: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ControllerOverride":
        return cls(
            id=str(row["id"]),
            scope_type=str(row["scope_type"]),
            scope_key=str(row["scope_key"]),
            controller_adapter_id=str(row["controller_adapter_id"]),
            mode=str(row["mode"]),
            expires_at=(
                int(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            remaining_uses=(
                int(row["remaining_uses"])
                if row["remaining_uses"] is not None else None
            ),
            reason=(str(row["reason"]) if row["reason"] else None),
            created_by=(str(row["created_by"]) if row["created_by"] else None),
            enabled=bool(row["enabled"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def active(self, *, now: Optional[int] = None) -> bool:
        current = _now() if now is None else int(now)
        return bool(
            self.enabled
            and (self.expires_at is None or self.expires_at > current)
            and (self.remaining_uses is None or self.remaining_uses > 0)
        )


def register_shell_version(
    conn: sqlite3.Connection,
    *,
    shell_key: str,
    name: str,
    contract: dict[str, Any],
    description: Optional[str] = None,
    required_capabilities: Iterable[str] = (),
    allowed_capabilities: Iterable[str] = (),
    evidence_policy: Optional[dict[str, Any]] = None,
) -> RoleShell:
    """Append a shell version and move only its mutable head pointer."""
    ensure_schema(conn)
    shell_key = str(shell_key).strip()
    if not shell_key:
        raise SupervisorRegistryError("shell_key is required")
    required = _capabilities(required_capabilities)
    allowed = _capabilities(allowed_capabilities)
    if not allowed:
        raise SupervisorRegistryError("allowed_capabilities must be non-empty")
    if not set(required).issubset(set(allowed)):
        raise SupervisorRegistryError("required capabilities must be allowed by the shell")
    contract = dict(contract or {})
    allowed_adapters = contract.get("allowed_adapters")
    if not isinstance(allowed_adapters, list) or not allowed_adapters:
        raise SupervisorRegistryError("contract.allowed_adapters must be a non-empty list")
    contract_json = _canonical_json(contract)
    contract_hash = hashlib.sha256(contract_json.encode("utf-8")).hexdigest()
    now = _now()
    with write_txn(conn):
        previous = conn.execute(
            "SELECT s.* FROM role_shell_heads h JOIN role_shells s "
            "ON s.id=h.shell_id WHERE h.shell_key=?",
            (shell_key,),
        ).fetchone()
        version = int(previous["version"]) + 1 if previous else 1
        supersedes = previous["id"] if previous else None
        shell_id = f"shell_{shell_key}_v{version}"
        conn.execute(
            "INSERT INTO role_shells "
            "(id,shell_key,version,supersedes_shell_id,name,description,"
            "contract_json,contract_hash,required_capabilities,allowed_capabilities,"
            "evidence_policy,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                shell_id, shell_key, version, supersedes, name, description,
                contract_json, contract_hash, _canonical_json(required),
                _canonical_json(allowed), _canonical_json(evidence_policy or {}), now,
            ),
        )
        conn.execute(
            "INSERT INTO role_shell_heads(shell_key,shell_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(shell_key) DO UPDATE SET shell_id=excluded.shell_id, "
            "updated_at=excluded.updated_at",
            (shell_key, shell_id, now),
        )
    return get_shell(conn, shell_id=shell_id)  # type: ignore[return-value]


def ensure_shell_version(
    conn: sqlite3.Connection,
    *,
    shell_key: str,
    name: str,
    contract: dict[str, Any],
    description: Optional[str] = None,
    required_capabilities: Iterable[str] = (),
    allowed_capabilities: Iterable[str] = (),
    evidence_policy: Optional[dict[str, Any]] = None,
) -> RoleShell:
    """Return the matching active version or append exactly one new version."""
    required = _capabilities(required_capabilities)
    allowed = _capabilities(allowed_capabilities)
    evidence = dict(evidence_policy or {})
    active = get_shell(conn, shell_key=str(shell_key).strip())
    if active is not None and all(
        (
            active.name == name,
            active.description == description,
            _canonical_json(active.contract) == _canonical_json(dict(contract or {})),
            active.required_capabilities == required,
            active.allowed_capabilities == allowed,
            _canonical_json(active.evidence_policy) == _canonical_json(evidence),
        )
    ):
        return active
    return register_shell_version(
        conn,
        shell_key=shell_key,
        name=name,
        description=description,
        contract=contract,
        required_capabilities=required,
        allowed_capabilities=allowed,
        evidence_policy=evidence,
    )


def get_shell(
    conn: sqlite3.Connection,
    *,
    shell_id: Optional[str] = None,
    shell_key: Optional[str] = None,
) -> Optional[RoleShell]:
    if shell_id:
        row = conn.execute("SELECT * FROM role_shells WHERE id=?", (shell_id,)).fetchone()
    elif shell_key:
        row = conn.execute(
            "SELECT s.* FROM role_shell_heads h JOIN role_shells s "
            "ON s.id=h.shell_id WHERE h.shell_key=?",
            (shell_key,),
        ).fetchone()
    else:
        raise SupervisorRegistryError("shell_id or shell_key is required")
    return RoleShell.from_row(row) if row else None


def list_shells(conn: sqlite3.Connection, *, active_only: bool = False) -> list[RoleShell]:
    if active_only:
        rows = conn.execute(
            "SELECT s.* FROM role_shell_heads h JOIN role_shells s ON s.id=h.shell_id "
            "ORDER BY s.shell_key"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM role_shells ORDER BY shell_key, version"
        ).fetchall()
    return [RoleShell.from_row(row) for row in rows]


def register_executor(
    conn: sqlite3.Connection,
    *,
    name: str,
    adapter_type: str,
    launch_config: Optional[dict[str, Any]] = None,
    capabilities: Iterable[str] = (),
    capacity: int = 1,
    heartbeat_required: bool = True,
    heartbeat_ttl_seconds: int = 300,
    description: Optional[str] = None,
    executor_id: Optional[str] = None,
) -> Executor:
    ensure_schema(conn)
    launch = _validate_executor_spec(
        adapter_type=adapter_type,
        launch_config=launch_config,
        capacity=capacity,
        capabilities=capabilities,
    )
    adapter_type = str(adapter_type).strip()
    now = _now()
    executor_id = executor_id or _new_id("executor")
    with write_txn(conn):
        conn.execute(
            "INSERT INTO executors "
            "(id,name,adapter_type,description,launch_config,capabilities,capacity,"
            "heartbeat_required,heartbeat_ttl_seconds,last_heartbeat_at,health_state,"
            "enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                executor_id, name, adapter_type, description, _canonical_json(launch),
                _canonical_json(_capabilities(capabilities)), int(capacity),
                int(bool(heartbeat_required)), max(1, int(heartbeat_ttl_seconds)),
                None, "unknown" if heartbeat_required else "healthy", 1, now, now,
            ),
        )
    return get_executor(conn, executor_id)  # type: ignore[return-value]


def _validate_executor_spec(
    *,
    adapter_type: str,
    launch_config: Optional[dict[str, Any]],
    capacity: int,
    capabilities: Iterable[str] = (),
) -> dict[str, Any]:
    adapter_type = str(adapter_type).strip()
    if adapter_type not in {"hermes_profile", "command", "manual"}:
        raise SupervisorRegistryError(f"unsupported adapter_type: {adapter_type}")
    if int(capacity) < 1:
        raise SupervisorRegistryError("capacity must be >= 1")
    launch = dict(launch_config or {})
    if adapter_type == "hermes_profile" and not str(launch.get("profile") or "").strip():
        raise SupervisorRegistryError("hermes_profile executor requires launch_config.profile")
    if adapter_type == "command":
        argv = launch.get("argv")
        if not isinstance(argv, list) or not argv:
            raise SupervisorRegistryError(
                "command executor requires non-empty launch_config.argv"
            )
        if launch.get("shell"):
            raise SupervisorRegistryError("command executor shell mode is forbidden")
        if not any("{prompt_file}" in str(item) for item in argv):
            raise SupervisorRegistryError(
                "command executor argv must contain {prompt_file}"
            )
        enforcement = launch.get("capability_enforcement")
        if enforcement not in {"env", "argv"}:
            raise SupervisorRegistryError(
                "command executor requires capability_enforcement env or argv"
            )
        if enforcement == "argv" and not any(
            "{capabilities_csv}" in str(item) for item in argv
        ):
            raise SupervisorRegistryError(
                "argv capability enforcement requires {capabilities_csv}"
            )
        raw_health_urls = launch.get("health_urls")
        if raw_health_urls is None and launch.get("health_url") is not None:
            raw_health_urls = [launch.get("health_url")]
        if raw_health_urls is not None:
            if not isinstance(raw_health_urls, list) or not raw_health_urls:
                raise SupervisorRegistryError(
                    "command executor health_urls must be a non-empty list"
                )
            health_urls: list[str] = []
            for raw_url in raw_health_urls:
                url = str(raw_url or "").strip()
                try:
                    parsed = urlsplit(url)
                except ValueError as exc:
                    raise SupervisorRegistryError(
                        f"invalid command executor health URL: {url!r}"
                    ) from exc
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    raise SupervisorRegistryError(
                        f"invalid command executor health URL: {url!r}"
                    )
                health_urls.append(url)
            launch["health_urls"] = health_urls
            launch.pop("health_url", None)
            try:
                timeout = float(launch.get("health_timeout_seconds", 3.0))
            except (TypeError, ValueError) as exc:
                raise SupervisorRegistryError(
                    "command executor health_timeout_seconds must be numeric"
                ) from exc
            if not 0.1 <= timeout <= 30.0:
                raise SupervisorRegistryError(
                    "command executor health_timeout_seconds must be between 0.1 and 30"
                )
            launch["health_timeout_seconds"] = timeout
        raw_contract = launch.get("tool_contract")
        if raw_contract is not None:
            if not isinstance(raw_contract, dict):
                raise SupervisorRegistryError(
                    "command executor tool_contract must be an object"
                )
            contract = dict(raw_contract)
            if int(contract.get("schema_version", 1)) != 1:
                raise SupervisorRegistryError(
                    "command executor tool_contract schema_version must be 1"
                )
            transport = str(contract.get("transport") or "").strip()
            if transport not in {"native_mcp", "adapter_brokered", "none"}:
                raise SupervisorRegistryError(
                    "command executor tool_contract transport must be "
                    "native_mcp, adapter_brokered, or none"
                )
            adapter_capabilities = _capabilities(
                contract.get("adapter_capabilities") or ()
            )
            native_capabilities = _capabilities(
                contract.get("native_capabilities") or ()
            )
            claimed = set(_capabilities(capabilities))
            provided = set(adapter_capabilities) | set(native_capabilities)
            undeclared = sorted(claimed - provided)
            if undeclared:
                raise SupervisorRegistryError(
                    "command executor capabilities missing from tool_contract: "
                    + ", ".join(undeclared)
                )
            required_mcp_servers = _capabilities(
                contract.get("required_mcp_servers") or ()
            )
            if required_mcp_servers and transport != "native_mcp":
                raise SupervisorRegistryError(
                    "required_mcp_servers require native_mcp transport"
                )
            raw_probe = contract.get("probe")
            probe: Optional[dict[str, Any]] = None
            if raw_probe is not None:
                if not isinstance(raw_probe, dict):
                    raise SupervisorRegistryError(
                        "command executor tool_contract.probe must be an object"
                    )
                probe = dict(raw_probe)
                probe_argv = probe.get("argv")
                if not isinstance(probe_argv, list) or not probe_argv:
                    raise SupervisorRegistryError(
                        "command executor tool_contract.probe.argv must be a "
                        "non-empty list"
                    )
                if probe.get("shell"):
                    raise SupervisorRegistryError(
                        "command executor tool probe shell mode is forbidden"
                    )
                markers = _capabilities(probe.get("required_output") or ())
                missing_markers = sorted(set(required_mcp_servers) - set(markers))
                if missing_markers:
                    raise SupervisorRegistryError(
                        "tool probe required_output must cover required MCP servers: "
                        + ", ".join(missing_markers)
                    )
                try:
                    probe_timeout = float(probe.get("timeout_seconds", 30.0))
                except (TypeError, ValueError) as exc:
                    raise SupervisorRegistryError(
                        "command executor tool probe timeout_seconds must be numeric"
                    ) from exc
                if not 0.1 <= probe_timeout <= 120.0:
                    raise SupervisorRegistryError(
                        "command executor tool probe timeout_seconds must be "
                        "between 0.1 and 120"
                    )
                probe = {
                    "argv": [str(item) for item in probe_argv],
                    "required_output": markers,
                    "timeout_seconds": probe_timeout,
                }
            if required_mcp_servers and probe is None:
                raise SupervisorRegistryError(
                    "native MCP tool contract requires a tool probe"
                )
            launch["tool_contract"] = {
                "schema_version": 1,
                "transport": transport,
                "adapter_capabilities": adapter_capabilities,
                "native_capabilities": native_capabilities,
                "required_mcp_servers": required_mcp_servers,
                "probe": probe,
            }
    return launch


def upsert_executor(
    conn: sqlite3.Connection,
    *,
    executor_id: str,
    name: str,
    adapter_type: str,
    launch_config: Optional[dict[str, Any]] = None,
    capabilities: Iterable[str] = (),
    capacity: int = 1,
    heartbeat_required: bool = True,
    heartbeat_ttl_seconds: int = 300,
    description: Optional[str] = None,
) -> Executor:
    """Idempotently install or refresh a mutable executor definition."""
    ensure_schema(conn)
    launch = _validate_executor_spec(
        adapter_type=adapter_type,
        launch_config=launch_config,
        capacity=capacity,
        capabilities=capabilities,
    )
    adapter_type = str(adapter_type).strip()
    existing = get_executor(conn, executor_id)
    if existing is None:
        return register_executor(
            conn,
            executor_id=executor_id,
            name=name,
            adapter_type=adapter_type,
            launch_config=launch,
            capabilities=capabilities,
            capacity=capacity,
            heartbeat_required=heartbeat_required,
            heartbeat_ttl_seconds=heartbeat_ttl_seconds,
            description=description,
        )
    now = _now()
    with write_txn(conn):
        conn.execute(
            "UPDATE executors SET name=?,adapter_type=?,description=?,launch_config=?,"
            "capabilities=?,capacity=?,heartbeat_required=?,heartbeat_ttl_seconds=?,"
            "health_state=?,enabled=1,updated_at=? WHERE id=?",
            (
                name, adapter_type, description, _canonical_json(launch),
                _canonical_json(_capabilities(capabilities)), int(capacity),
                int(bool(heartbeat_required)), max(1, int(heartbeat_ttl_seconds)),
                (
                    existing.health_state
                    if heartbeat_required
                    else "healthy"
                ),
                now,
                executor_id,
            ),
        )
    return get_executor(conn, executor_id)  # type: ignore[return-value]


def get_executor(conn: sqlite3.Connection, executor_id: str) -> Optional[Executor]:
    row = conn.execute("SELECT * FROM executors WHERE id=?", (executor_id,)).fetchone()
    return Executor.from_row(row) if row else None


def list_executors(conn: sqlite3.Connection) -> list[Executor]:
    return [
        Executor.from_row(row)
        for row in conn.execute("SELECT * FROM executors ORDER BY name,id")
    ]


def _load_runtime_config(home: Path) -> tuple[dict[str, Any], str]:
    """Read one effective Hermes config without changing process-global state."""
    from hermes_cli.config import load_config
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    config_path = home / "config.yaml"
    token = set_hermes_home_override(home)
    try:
        config = load_config()
    except Exception:
        return {}, "unreadable"
    finally:
        reset_hermes_home_override(token)
    return config, "configured" if config_path.is_file() else "defaults"


def _runtime_backend_label(backend: str, provider: str) -> str:
    normalized = str(backend or provider or "unknown").strip().lower()
    labels = {
        "codex_app_server": "Codex app-server",
        "openai-codex": "OpenAI Codex",
        "opencode-zen": "OpenCode Zen",
        "vllm": "vLLM",
        "ollama": "Ollama",
        "openai": "OpenAI API",
    }
    return labels.get(normalized, str(backend or provider or "unknown"))


def _runtime_descriptor(
    config: dict[str, Any],
    *,
    profile: str,
    config_scope: str,
    config_state: str,
) -> dict[str, Any]:
    """Return safe operator-facing runtime metadata (never credentials)."""
    model_config = config.get("model") or {}
    if not isinstance(model_config, dict):
        model_config = {"default": model_config}
    agent_config = config.get("agent") or {}
    if not isinstance(agent_config, dict):
        agent_config = {}
    provider = str(
        model_config.get("provider")
        or config.get("provider")
        or "unknown"
    ).strip()
    providers = config.get("providers") or {}
    provider_config = (
        providers.get(provider) or {}
        if isinstance(providers, dict)
        else {}
    )
    if not isinstance(provider_config, dict):
        provider_config = {}
    openai_runtime = str(model_config.get("openai_runtime") or "").strip()
    backend = (
        openai_runtime
        if openai_runtime and openai_runtime.lower() != "auto"
        else provider
    )
    model = str(
        model_config.get("default")
        or model_config.get("model")
        or provider_config.get("default_model")
        or config.get("model_name")
        or "unknown"
    ).strip()
    reasoning = str(
        agent_config.get("reasoning_effort")
        or model_config.get("reasoning_effort")
        or config.get("reasoning_effort")
        or "default"
    ).strip()
    api_mode = str(
        model_config.get("api_mode")
        or provider_config.get("transport")
        or config.get("api_mode")
        or ""
    ).strip() or None
    base_url = str(
        model_config.get("base_url")
        or provider_config.get("base_url")
        or ""
    ).strip()
    try:
        endpoint = urlsplit(base_url).netloc or None
    except ValueError:
        endpoint = None
    backend_label = _runtime_backend_label(backend, provider)
    return {
        "profile": profile,
        "config_scope": config_scope,
        "backend": backend or "unknown",
        "backend_label": backend_label,
        "provider": provider or "unknown",
        "model": model or "unknown",
        "reasoning_effort": reasoning or "default",
        "api_mode": api_mode,
        "endpoint": endpoint,
        "context_length": model_config.get("context_length"),
        "config_state": config_state,
        "display_label": f"{backend_label} · {model or 'unknown'} · {reasoning or 'default'}",
    }


def controller_runtime_descriptor() -> dict[str, Any]:
    """Describe the actual central Hermes controller runtime."""
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root()
    config, state = _load_runtime_config(root)
    return _runtime_descriptor(
        config,
        profile="default",
        config_scope="controller",
        config_state=state,
    )


def controller_adapter_runtime_descriptor(
    adapter: ControllerAdapter,
) -> dict[str, Any]:
    """Render a registered controller candidate without exposing credentials."""
    backend = (
        "codex_app_server"
        if adapter.api_mode == "codex_app_server"
        else adapter.provider
    )
    try:
        endpoint = urlsplit(adapter.base_url or "").netloc or None
    except ValueError:
        endpoint = None
    backend_label = _runtime_backend_label(backend, adapter.provider)
    return {
        "profile": "default",
        "config_scope": "controller_adapter_registry",
        "backend": backend,
        "backend_label": backend_label,
        "provider": adapter.provider,
        "model": adapter.model,
        "reasoning_effort": adapter.reasoning_effort,
        "api_mode": adapter.api_mode,
        "endpoint": endpoint,
        "context_length": adapter.metadata.get("context_length"),
        "config_state": "registered",
        "display_label": (
            f"{backend_label} · {adapter.model} · {adapter.reasoning_effort}"
        ),
    }


def _controller_adapter_dict(adapter: ControllerAdapter) -> dict[str, Any]:
    return {
        "controller_adapter_id": adapter.id,
        "name": adapter.name,
        "description": adapter.description,
        "runtime": controller_adapter_runtime_descriptor(adapter),
        "provider": adapter.provider,
        "model": adapter.model,
        "reasoning_effort": adapter.reasoning_effort,
        "key_env": adapter.key_env,
        "health_url": adapter.health_url,
        "fallback_adapter_id": adapter.fallback_adapter_id,
        "enabled": adapter.enabled,
        "health_state": adapter.health_state,
        "last_health_at": adapter.last_health_at,
        "routable": adapter.routable(),
        "metadata": dict(adapter.metadata),
        "display_label": controller_adapter_runtime_descriptor(adapter)[
            "display_label"
        ],
    }


def executor_runtime_descriptor(executor: Executor) -> dict[str, Any]:
    """Describe the live backend/model used by a registered executor."""
    if executor.adapter_type != "hermes_profile":
        backend = _runtime_backend_label(executor.adapter_type, executor.adapter_type)
        model = str(executor.launch_config.get("model") or "operator-managed")
        reasoning = str(executor.launch_config.get("reasoning_effort") or "default")
        endpoint_value = str(executor.launch_config.get("endpoint") or "").strip()
        if not endpoint_value:
            health_urls = executor.launch_config.get("health_urls") or []
            if isinstance(health_urls, list) and health_urls:
                endpoint_value = str(health_urls[0] or "").strip()
        try:
            endpoint = urlsplit(endpoint_value).netloc or None
        except ValueError:
            endpoint = None
        return {
            "profile": None,
            "config_scope": "executor_launch_config",
            "backend": executor.adapter_type,
            "backend_label": backend,
            "provider": executor.adapter_type,
            "model": model,
            "reasoning_effort": reasoning,
            "api_mode": None,
            "endpoint": endpoint,
            "context_length": None,
            "config_state": "registered",
            "display_label": f"{backend} · {model} · {reasoning}",
        }

    from hermes_constants import get_default_hermes_root

    profile = str(executor.launch_config.get("profile") or "default").strip()
    root = get_default_hermes_root()
    home = root if profile == "default" else root / "profiles" / profile
    config, state = _load_runtime_config(home)
    return _runtime_descriptor(
        config,
        profile=profile,
        config_scope="executor_profile",
        config_state=state,
    )


def _executor_view(executor: Executor) -> dict[str, Any]:
    runtime = executor_runtime_descriptor(executor)
    tool_contract = executor.launch_config.get("tool_contract")
    contract_view = None
    if isinstance(tool_contract, dict):
        contract_view = {
            "schema_version": tool_contract.get("schema_version"),
            "transport": tool_contract.get("transport"),
            "adapter_capabilities": _json_list(
                tool_contract.get("adapter_capabilities")
            ),
            "native_capabilities": _json_list(
                tool_contract.get("native_capabilities")
            ),
            "required_mcp_servers": _json_list(
                tool_contract.get("required_mcp_servers")
            ),
            "probe_configured": isinstance(tool_contract.get("probe"), dict),
        }
    return {
        "executor_id": executor.id,
        "name": executor.name,
        "adapter_type": executor.adapter_type,
        "description": executor.description,
        "enabled": executor.enabled,
        "health_state": executor.health_state,
        "heartbeat_required": executor.heartbeat_required,
        "heartbeat_ttl_seconds": executor.heartbeat_ttl_seconds,
        "last_heartbeat_at": executor.last_heartbeat_at,
        "capacity": executor.capacity,
        "runtime": runtime,
        "tool_contract": contract_view,
        "display_label": f"{executor.name} · {runtime['display_label']}",
    }


def heartbeat_executor(
    conn: sqlite3.Connection,
    executor_id: str,
    *,
    health_state: str = "healthy",
) -> bool:
    if health_state not in {"healthy", "degraded", "unhealthy", "unknown"}:
        raise SupervisorRegistryError(f"invalid health_state: {health_state}")
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE executors SET last_heartbeat_at=?,health_state=?,updated_at=? "
            "WHERE id=? AND enabled=1",
            (now, health_state, now, executor_id),
        )
    return cur.rowcount == 1


def refresh_executor_health_probes(
    conn: sqlite3.Connection,
    *,
    executor_ids: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Refresh selected enabled command executors with confirmed health gates.

    A command adapter may front several independently required processes (for
    example an API plus planner/executor model servers), so every configured
    URL and tool-contract probe must succeed before the executor becomes
    routable.  Failures are confirmed by a second full attempt by default so a
    one-shot provider/session error cannot immediately poison role routing.
    """
    selected_ids = {
        str(executor_id).strip()
        for executor_id in (executor_ids or ())
        if str(executor_id).strip()
    }
    results: list[dict[str, Any]] = []
    for executor in list_executors(conn):
        if selected_ids and executor.id not in selected_ids:
            continue
        urls = executor.launch_config.get("health_urls") or []
        tool_contract = executor.launch_config.get("tool_contract") or {}
        tool_probe = (
            tool_contract.get("probe") if isinstance(tool_contract, dict) else None
        )
        if (
            executor.adapter_type != "command"
            or not executor.enabled
            or (
                (not isinstance(urls, list) or not urls)
                and not isinstance(tool_probe, dict)
            )
        ):
            continue
        timeout = float(executor.launch_config.get("health_timeout_seconds", 3.0))
        confirmation_attempts = max(
            1,
            min(
                3,
                int(
                    executor.launch_config.get(
                        "health_failure_confirmation_attempts", 2
                    )
                ),
            ),
        )
        attempts: list[dict[str, Any]] = []
        healthy = False
        checks: list[dict[str, Any]] = []
        for attempt_number in range(1, confirmation_attempts + 1):
            attempt_checks: list[dict[str, Any]] = []
            attempt_healthy = True
            for raw_url in urls:
                url = str(raw_url)
                try:
                    request = Request(
                        url, headers={"User-Agent": "Hermes-Supervisor/1"}
                    )
                    with urlopen(request, timeout=timeout) as response:  # nosec B310
                        status = int(getattr(response, "status", response.getcode()))
                    ok = 200 <= status < 400
                    attempt_checks.append(
                        {"url": url, "status": status, "healthy": ok}
                    )
                    attempt_healthy = attempt_healthy and ok
                except Exception as exc:
                    attempt_healthy = False
                    attempt_checks.append(
                        {
                            "url": url,
                            "status": None,
                            "healthy": False,
                            "error": str(exc)[:300],
                        }
                    )
            if isinstance(tool_probe, dict):
                probe_argv = tool_probe.get("argv") or []
                markers = _json_list(tool_probe.get("required_output") or [])
                probe_timeout = float(tool_probe.get("timeout_seconds", 30.0))
                try:
                    completed = subprocess.run(
                        [str(item) for item in probe_argv],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=probe_timeout,
                        check=False,
                    )
                    output = completed.stdout or ""
                    missing = sorted(
                        marker for marker in markers if marker not in output
                    )
                    ok = completed.returncode == 0 and not missing
                    attempt_checks.append(
                        {
                            "kind": "tool_contract",
                            "healthy": ok,
                            "returncode": completed.returncode,
                            "required_output": markers,
                            "missing_output": missing,
                        }
                    )
                    attempt_healthy = attempt_healthy and ok
                except Exception as exc:
                    attempt_healthy = False
                    attempt_checks.append(
                        {
                            "kind": "tool_contract",
                            "healthy": False,
                            "required_output": markers,
                            "error": str(exc)[:300],
                        }
                    )
            attempts.append(
                {
                    "attempt": attempt_number,
                    "healthy": attempt_healthy,
                    "checks": attempt_checks,
                }
            )
            checks = attempt_checks
            healthy = attempt_healthy
            if healthy:
                break
        heartbeat_executor(
            conn,
            executor.id,
            health_state="healthy" if healthy else "unhealthy",
        )
        results.append(
            {
                "executor_id": executor.id,
                "healthy": healthy,
                "checks": checks,
                "attempt_count": len(attempts),
                "confirmed_failure": bool(not healthy and len(attempts) >= 2),
                "attempts": attempts,
            }
        )
    return results


def set_executor_enabled(conn: sqlite3.Connection, executor_id: str, enabled: bool) -> bool:
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE executors SET enabled=?,updated_at=? WHERE id=?",
            (int(bool(enabled)), now, executor_id),
        )
    return cur.rowcount == 1


def set_executor_operational_state(
    conn: sqlite3.Connection,
    executor_value: str,
    *,
    enabled: bool,
    reason: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> dict[str, Any]:
    """Enable/disable an executor with an audited external health gate.

    Enabling a command executor that declares ``health_urls`` is fail-closed:
    every endpoint is probed immediately and a failed gate returns the
    executor to disabled before the command is allowed to become routable.
    Disabling affects future claims; the returned active-run count makes clear
    that already-running cards require their own archive/rerun action.
    """
    ensure_schema(conn)
    executor = resolve_executor(conn, executor_value)
    if executor is None:
        raise SupervisorRegistryError(f"unknown executor: {executor_value}")
    if not set_executor_enabled(conn, executor.id, enabled):
        raise SupervisorRegistryError(
            f"failed to {'enable' if enabled else 'disable'} executor {executor.id}"
        )
    probe_result = None
    health_gate_passed = True
    health_urls = executor.launch_config.get("health_urls") or []
    tool_contract = executor.launch_config.get("tool_contract") or {}
    tool_probe = (
        tool_contract.get("probe") if isinstance(tool_contract, dict) else None
    )
    if enabled and executor.adapter_type == "command" and (
        health_urls or isinstance(tool_probe, dict)
    ):
        probes = refresh_executor_health_probes(
            conn,
            executor_ids=(executor.id,),
        )
        probe_result = next(
            (row for row in probes if row["executor_id"] == executor.id), None
        )
        health_gate_passed = bool(probe_result and probe_result.get("healthy"))
        if not health_gate_passed:
            set_executor_enabled(conn, executor.id, False)
    current = get_executor(conn, executor.id)
    if current is None:
        raise SupervisorRegistryError(f"executor disappeared: {executor.id}")
    running = active_run_count(conn, executor.id)
    details = {
        "requested_enabled": bool(enabled),
        "effective_enabled": current.enabled,
        "health_gate_passed": health_gate_passed,
        "health_state": current.health_state,
        "probe": probe_result,
        "active_runs": running,
        "running_tasks_unchanged": bool(running),
        "reason": str(reason).strip() if reason else None,
    }
    with write_txn(conn):
        event_id = _append_adapter_event_in_txn(
            conn,
            kind=(
                "adapter_executor_enabled"
                if current.enabled
                else (
                    "adapter_executor_enable_rejected"
                    if enabled
                    else "adapter_executor_disabled"
                )
            ),
            scope_type="executor",
            scope_key=executor.id,
            executor_id=executor.id,
            details=details,
            created_by=changed_by,
        )
    return {
        "event_id": event_id,
        "executor_id": current.id,
        "name": current.name,
        "requested_enabled": bool(enabled),
        "enabled": current.enabled,
        "health_state": current.health_state,
        "health_gate_passed": health_gate_passed,
        "probe": probe_result,
        "active_runs": running,
        "running_tasks_unchanged": bool(running),
    }


def bind_executor(
    conn: sqlite3.Connection,
    *,
    shell_id: str,
    executor_id: str,
    priority: int = 0,
    weight: float = 1.0,
    capability_cap: Iterable[str] = (),
    constraints: Optional[dict[str, Any]] = None,
    responsibility: str = "candidate",
    assignment_note: Optional[str] = None,
    assigned_by: Optional[str] = None,
    binding_id: Optional[str] = None,
) -> Binding:
    ensure_schema(conn)
    if get_shell(conn, shell_id=shell_id) is None:
        raise SupervisorRegistryError(f"unknown shell: {shell_id}")
    if get_executor(conn, executor_id) is None:
        raise SupervisorRegistryError(f"unknown executor: {executor_id}")
    binding_id = binding_id or _new_id("binding")
    responsibility = str(responsibility or "candidate").strip().lower()
    if responsibility not in {"primary", "candidate"}:
        raise SupervisorRegistryError(
            "binding responsibility must be primary or candidate"
        )
    now = _now()
    with write_txn(conn):
        conn.execute(
            "INSERT INTO role_bindings "
            "(id,shell_id,executor_id,priority,weight,capability_cap,constraints_json,"
            "responsibility,assignment_note,assigned_by,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding_id, shell_id, executor_id, int(priority), float(weight),
                _canonical_json(_capabilities(capability_cap)),
                _canonical_json(constraints or {}), responsibility,
                (str(assignment_note).strip() if assignment_note else None),
                (str(assigned_by).strip() if assigned_by else None),
                1, now, now,
            ),
        )
    return get_binding(conn, binding_id)  # type: ignore[return-value]


def upsert_binding(
    conn: sqlite3.Connection,
    *,
    shell_id: str,
    executor_id: str,
    priority: int = 0,
    weight: float = 1.0,
    capability_cap: Iterable[str] = (),
    constraints: Optional[dict[str, Any]] = None,
    responsibility: str = "candidate",
    assignment_note: Optional[str] = None,
    assigned_by: Optional[str] = None,
    binding_id: Optional[str] = None,
) -> Binding:
    """Idempotently install or refresh a mutable many-to-many binding."""
    ensure_schema(conn)
    if get_shell(conn, shell_id=shell_id) is None:
        raise SupervisorRegistryError(f"unknown shell: {shell_id}")
    if get_executor(conn, executor_id) is None:
        raise SupervisorRegistryError(f"unknown executor: {executor_id}")
    existing_row = conn.execute(
        "SELECT * FROM role_bindings WHERE shell_id=? AND executor_id=?",
        (shell_id, executor_id),
    ).fetchone()
    if existing_row is None and binding_id is not None:
        # Immutable shell upgrades move the mutable head from e.g. code_v1 to
        # code_v2 while bootstrap deliberately keeps a stable binding id.  An
        # exact (new shell, executor) lookup cannot find that pre-upgrade row,
        # so resolve it by id and permit only a same-role, same-executor
        # rebind.  This preserves adapter ownership/history without allowing a
        # stable id to jump to an unrelated role or executor.
        candidate = conn.execute(
            "SELECT * FROM role_bindings WHERE id=?", (binding_id,)
        ).fetchone()
        if candidate is not None:
            old_shell = get_shell(conn, shell_id=str(candidate["shell_id"]))
            new_shell = get_shell(conn, shell_id=shell_id)
            if str(candidate["executor_id"]) != executor_id:
                raise SupervisorRegistryError(
                    f"binding executor mismatch for {binding_id}: "
                    f"{candidate['executor_id']} != {executor_id}"
                )
            if (
                old_shell is None
                or new_shell is None
                or old_shell.shell_key != new_shell.shell_key
            ):
                raise SupervisorRegistryError(
                    f"binding role mismatch for {binding_id}: "
                    f"{candidate['shell_id']} -> {shell_id}"
                )
            existing_row = candidate
    if existing_row is None:
        return bind_executor(
            conn,
            shell_id=shell_id,
            executor_id=executor_id,
            priority=priority,
            weight=weight,
            capability_cap=capability_cap,
            constraints=constraints,
            responsibility=responsibility,
            assignment_note=assignment_note,
            assigned_by=assigned_by,
            binding_id=binding_id,
        )
    existing_id = str(existing_row["id"])
    if binding_id is not None and binding_id != existing_id:
        raise SupervisorRegistryError(
            f"binding id mismatch for {shell_id}/{executor_id}: {existing_id}"
        )
    now = _now()
    responsibility = str(responsibility or "candidate").strip().lower()
    if responsibility not in {"primary", "candidate"}:
        raise SupervisorRegistryError(
            "binding responsibility must be primary or candidate"
        )
    with write_txn(conn):
        conn.execute(
            "UPDATE role_bindings SET shell_id=?,executor_id=?,priority=?,weight=?,capability_cap=?,"
            "constraints_json=?,responsibility=?,assignment_note=?,assigned_by=?,"
            "enabled=1,updated_at=? WHERE id=?",
            (
                shell_id,
                executor_id,
                int(priority),
                float(weight),
                _canonical_json(_capabilities(capability_cap)),
                _canonical_json(constraints or {}),
                responsibility,
                (str(assignment_note).strip() if assignment_note else None),
                (str(assigned_by).strip() if assigned_by else None),
                now,
                existing_id,
            ),
        )
    return get_binding(conn, existing_id)  # type: ignore[return-value]


def get_binding(conn: sqlite3.Connection, binding_id: str) -> Optional[Binding]:
    row = conn.execute("SELECT * FROM role_bindings WHERE id=?", (binding_id,)).fetchone()
    return Binding.from_row(row) if row else None


def list_bindings(
    conn: sqlite3.Connection,
    *,
    shell_id: Optional[str] = None,
    executor_id: Optional[str] = None,
) -> list[Binding]:
    clauses: list[str] = []
    params: list[Any] = []
    if shell_id:
        clauses.append("shell_id=?")
        params.append(shell_id)
    if executor_id:
        clauses.append("executor_id=?")
        params.append(executor_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT * FROM role_bindings" + where
        + " ORDER BY priority DESC,weight DESC,created_at,id",
        params,
    ).fetchall()
    return [Binding.from_row(row) for row in rows]


def set_binding_enabled(conn: sqlite3.Connection, binding_id: str, enabled: bool) -> bool:
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE role_bindings SET enabled=?,updated_at=? WHERE id=?",
            (int(bool(enabled)), now, binding_id),
        )
    return cur.rowcount == 1


def resolve_shell(conn: sqlite3.Connection, value: str) -> Optional[RoleShell]:
    """Resolve either an immutable shell id or the active shell key."""
    target = str(value or "").strip()
    if not target:
        return None
    return get_shell(conn, shell_id=target) or get_shell(conn, shell_key=target)


def resolve_executor(conn: sqlite3.Connection, value: str) -> Optional[Executor]:
    """Resolve an executor by stable id or exact display/profile name."""
    target = str(value or "").strip()
    if not target:
        return None
    executor = get_executor(conn, target)
    if executor is not None:
        return executor
    rows = conn.execute(
        "SELECT * FROM executors WHERE name=? ORDER BY id", (target,)
    ).fetchall()
    if len(rows) > 1:
        raise SupervisorRegistryError(f"ambiguous executor name: {target}")
    return Executor.from_row(rows[0]) if rows else None


def get_controller_adapter(
    conn: sqlite3.Connection, controller_adapter_id: str
) -> Optional[ControllerAdapter]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM controller_adapters WHERE id=?",
        (str(controller_adapter_id),),
    ).fetchone()
    return ControllerAdapter.from_row(row) if row else None


def resolve_controller_adapter(
    conn: sqlite3.Connection, value: str
) -> Optional[ControllerAdapter]:
    """Resolve a controller candidate by stable id or exact name."""
    target = str(value or "").strip()
    if not target:
        return None
    item = get_controller_adapter(conn, target)
    if item is not None:
        return item
    rows = conn.execute(
        "SELECT * FROM controller_adapters WHERE name=? ORDER BY id",
        (target,),
    ).fetchall()
    if len(rows) > 1:
        raise SupervisorRegistryError(f"ambiguous controller adapter name: {target}")
    return ControllerAdapter.from_row(rows[0]) if rows else None


def list_controller_adapters(conn: sqlite3.Connection) -> list[ControllerAdapter]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM controller_adapters ORDER BY enabled DESC,name,id"
    ).fetchall()
    return [ControllerAdapter.from_row(row) for row in rows]


def upsert_controller_adapter(
    conn: sqlite3.Connection,
    *,
    controller_adapter_id: str,
    name: str,
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    api_mode: Optional[str] = None,
    reasoning_effort: str = "medium",
    key_env: Optional[str] = None,
    health_url: Optional[str] = None,
    fallback_adapter_id: Optional[str] = None,
    description: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    initial_enabled: bool = False,
    initial_health_state: str = "unknown",
    verified_healthy: bool = False,
) -> ControllerAdapter:
    """Register one swappable Hermes controller runtime without storing secrets."""
    ensure_schema(conn)
    adapter_id = str(controller_adapter_id or "").strip()
    clean_name = str(name or "").strip()
    clean_provider = str(provider or "").strip()
    clean_model = str(model or "").strip()
    clean_key_env = str(key_env or "").strip() or None
    if not adapter_id or not clean_name or not clean_provider or not clean_model:
        raise SupervisorRegistryError(
            "controller adapter id, name, provider, and model are required"
        )
    if clean_key_env and not all(
        ch.isupper() or ch.isdigit() or ch == "_" for ch in clean_key_env
    ):
        raise SupervisorRegistryError(
            "controller adapter key_env must contain only A-Z, 0-9, and _"
        )
    clean_base_url = str(base_url or "").strip() or None
    clean_health_url = str(health_url or "").strip() or None
    for label, value in (("base_url", clean_base_url), ("health_url", clean_health_url)):
        if value and urlsplit(value).scheme not in {"http", "https"}:
            raise SupervisorRegistryError(
                f"controller adapter {label} must use http or https"
            )
    if fallback_adapter_id:
        fallback_adapter_id = str(fallback_adapter_id).strip()
        if fallback_adapter_id == adapter_id:
            raise SupervisorRegistryError("a controller adapter cannot fall back to itself")
        if get_controller_adapter(conn, fallback_adapter_id) is None:
            raise SupervisorRegistryError(
                f"unknown controller fallback adapter: {fallback_adapter_id}"
            )
    existing = get_controller_adapter(conn, adapter_id)
    now = _now()
    if verified_healthy:
        enabled = 1
        health_state = "healthy"
        last_health_at = now
    elif existing is not None:
        enabled = int(existing.enabled)
        health_state = existing.health_state
        last_health_at = existing.last_health_at
    else:
        enabled = int(bool(initial_enabled))
        health_state = str(initial_health_state or "unknown")
        last_health_at = now if health_state in {"healthy", "unhealthy"} else None
    with write_txn(conn):
        conn.execute(
            "INSERT INTO controller_adapters "
            "(id,name,provider,model,base_url,api_mode,reasoning_effort,key_env,"
            "health_url,fallback_adapter_id,description,metadata_json,health_state,"
            "last_health_at,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,provider=excluded.provider,"
            "model=excluded.model,base_url=excluded.base_url,api_mode=excluded.api_mode,"
            "reasoning_effort=excluded.reasoning_effort,key_env=excluded.key_env,"
            "health_url=excluded.health_url,fallback_adapter_id=excluded.fallback_adapter_id,"
            "description=excluded.description,metadata_json=excluded.metadata_json,"
            "health_state=excluded.health_state,last_health_at=excluded.last_health_at,"
            "enabled=excluded.enabled,updated_at=excluded.updated_at",
            (
                adapter_id,
                clean_name,
                clean_provider,
                clean_model,
                clean_base_url,
                (str(api_mode).strip() if api_mode else None),
                str(reasoning_effort or "medium").strip() or "medium",
                clean_key_env,
                clean_health_url,
                fallback_adapter_id,
                (str(description).strip() if description else None),
                _canonical_json(metadata or {}),
                health_state,
                last_health_at,
                enabled,
                (existing.created_at if existing else now),
                now,
            ),
        )
    return get_controller_adapter(conn, adapter_id)  # type: ignore[return-value]


def probe_controller_adapter(
    adapter: ControllerAdapter, *, timeout_seconds: float = 5.0
) -> dict[str, Any]:
    """Perform the candidate's declared auth/catalog/tool-call health gate."""
    started = time.monotonic()
    result: dict[str, Any] = {
        "controller_adapter_id": adapter.id,
        "provider": adapter.provider,
        "model": adapter.model,
        "healthy": False,
        "checked_at": _now(),
    }
    secret = str(os.environ.get(adapter.key_env, "") if adapter.key_env else "").strip()
    if adapter.key_env and not secret:
        result.update(
            {
                "reason": f"required environment variable {adapter.key_env} is missing",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        )
        return result
    if adapter.provider == "openai-codex" and not adapter.health_url:
        runtime = controller_runtime_descriptor()
        provider_matches = runtime.get("provider") == "openai-codex"
        result.update(
            {
                "healthy": bool(provider_matches),
                "reason": (
                    "active controller config resolves OpenAI Codex"
                    if provider_matches
                    else "active controller config is not OpenAI Codex"
                ),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        )
        return result
    if not adapter.health_url:
        result.update(
            {
                "reason": "no health_url configured",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        )
        return result
    headers = {
        "Accept": "application/json",
        "User-Agent": "HermesAgent/supervisor-health",
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        request = Request(adapter.health_url, headers=headers)
        with urlopen(request, timeout=max(0.2, float(timeout_seconds))) as response:
            status = int(getattr(response, "status", 200) or 200)
            payload = response.read(2_000_000)
        parsed = json.loads(payload.decode("utf-8")) if payload else {}
        model_ids: set[str] = set()
        rows = parsed.get("data") if isinstance(parsed, dict) else None
        if isinstance(rows, list):
            model_ids = {
                str(row.get("id") or "").strip()
                for row in rows
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            }
        require_model = bool(adapter.metadata.get("require_model_in_catalog", True))
        configured_model_present = (
            adapter.model in model_ids if model_ids else not require_model
        )
        dynamic_free = bool(
            adapter.metadata.get("dynamic_free_model_fallback", False)
        )
        free_suffix = str(
            adapter.metadata.get("free_model_suffix") or "-free"
        )
        explicit_free_models = adapter.metadata.get("free_model_ids")
        if not isinstance(explicit_free_models, list):
            explicit_free_models = []
        explicit_free_model_ids = {
            str(value or "").strip()
            for value in explicit_free_models
            if str(value or "").strip()
        }

        def is_free_model(model_id: str) -> bool:
            return bool(
                model_id.endswith(free_suffix)
                or model_id in explicit_free_model_ids
            )

        configured_fallbacks = adapter.metadata.get("model_fallback_candidates")
        if not isinstance(configured_fallbacks, list):
            configured_fallbacks = []
        candidate_models: list[str] = []
        for value in [adapter.model, *configured_fallbacks]:
            candidate = str(value or "").strip()
            if candidate and candidate not in candidate_models:
                candidate_models.append(candidate)
        if dynamic_free:
            for candidate in sorted(model_ids):
                if is_free_model(candidate) and candidate not in candidate_models:
                    candidate_models.append(candidate)
        if model_ids:
            candidate_models = [
                candidate for candidate in candidate_models if candidate in model_ids
            ]
        elif require_model:
            candidate_models = []
        healthy = bool(200 <= status < 300 and candidate_models)
        tool_smoke_required = bool(adapter.metadata.get("require_tool_smoke", False))
        result.update(
            {
                "healthy": healthy,
                "http_status": status,
                "model_present": configured_model_present,
                "configured_model_present": configured_model_present,
                "catalog_model_count": len(model_ids),
                "catalog_free_models": sorted(
                    model_id
                    for model_id in model_ids
                    if is_free_model(model_id)
                ),
                "candidate_models": candidate_models,
                "reason": (
                    "health gate passed"
                    if healthy
                    else (
                        f"configured model {adapter.model!r} not present in catalog "
                        "and no eligible fallback model was found"
                    )
                ),
            }
        )
        if healthy and tool_smoke_required:
            if not adapter.base_url:
                result.update(
                    {
                        "healthy": False,
                        "tool_smoke_required": True,
                        "tool_smoke_passed": False,
                        "reason": "tool smoke requires base_url",
                    }
                )
            else:
                completion_url = (
                    str(adapter.metadata.get("tool_smoke_url") or "").strip()
                    or f"{adapter.base_url.rstrip('/')}/chat/completions"
                )
                smoke_headers = dict(headers)
                smoke_headers["Content-Type"] = "application/json"
                tool_smoke_choice = str(
                    adapter.metadata.get("tool_smoke_choice") or "required"
                ).strip()
                if tool_smoke_choice not in {"auto", "required"}:
                    tool_smoke_choice = "required"
                tool_smoke_max_tokens = max(
                    16,
                    min(
                        512,
                        int(adapter.metadata.get("tool_smoke_max_tokens") or 64),
                    ),
                )
                smoke_attempts: list[dict[str, Any]] = []
                selected_model = None
                for candidate_model in candidate_models:
                    smoke_payload = {
                        "model": candidate_model,
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "You must call the hermes_health tool with value ok. "
                                    "Do not answer directly."
                                ),
                            }
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "hermes_health",
                                    "description": "Hermes controller health probe",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "value": {"type": "string"}
                                        },
                                        "required": ["value"],
                                    },
                                },
                            }
                        ],
                        "tool_choice": tool_smoke_choice,
                        "max_tokens": tool_smoke_max_tokens,
                        "temperature": 0,
                    }
                    try:
                        smoke_request = Request(
                            completion_url,
                            data=json.dumps(smoke_payload).encode("utf-8"),
                            headers=smoke_headers,
                            method="POST",
                        )
                        with urlopen(
                            smoke_request,
                            timeout=max(0.2, float(timeout_seconds)),
                        ) as smoke_response:
                            smoke_status = int(
                                getattr(smoke_response, "status", 200) or 200
                            )
                            smoke_raw = smoke_response.read(2_000_000)
                        smoke_parsed = (
                            json.loads(smoke_raw.decode("utf-8"))
                            if smoke_raw
                            else {}
                        )
                        choices = (
                            smoke_parsed.get("choices")
                            if isinstance(smoke_parsed, dict)
                            else None
                        )
                        message = (
                            choices[0].get("message")
                            if isinstance(choices, list)
                            and choices
                            and isinstance(choices[0], dict)
                            else None
                        )
                        tool_calls = (
                            message.get("tool_calls")
                            if isinstance(message, dict)
                            else None
                        )
                        tool_name = None
                        if isinstance(tool_calls, list) and tool_calls:
                            first_call = tool_calls[0]
                            function = (
                                first_call.get("function")
                                if isinstance(first_call, dict)
                                else None
                            )
                            if isinstance(function, dict):
                                tool_name = (
                                    str(function.get("name") or "").strip() or None
                                )
                        passed = bool(
                            200 <= smoke_status < 300
                            and tool_name == "hermes_health"
                        )
                        smoke_attempts.append(
                            {
                                "model": candidate_model,
                                "http_status": smoke_status,
                                "tool_name": tool_name,
                                "passed": passed,
                            }
                        )
                    except Exception as smoke_exc:
                        passed = False
                        smoke_attempts.append(
                            {
                                "model": candidate_model,
                                "http_status": getattr(smoke_exc, "code", None),
                                "tool_name": None,
                                "passed": False,
                                "reason": f"{type(smoke_exc).__name__}: {smoke_exc}",
                            }
                        )
                    if passed:
                        selected_model = candidate_model
                        break
                tool_smoke_passed = selected_model is not None
                selected_attempt = smoke_attempts[-1] if smoke_attempts else {}
                result.update(
                    {
                        "healthy": tool_smoke_passed,
                        "tool_smoke_required": True,
                        "tool_smoke_passed": tool_smoke_passed,
                        "tool_smoke_http_status": selected_attempt.get("http_status"),
                        "tool_smoke_tool_name": selected_attempt.get("tool_name"),
                        "tool_smoke_attempts": smoke_attempts,
                        "selected_model": selected_model,
                        "model_switched": bool(
                            selected_model and selected_model != adapter.model
                        ),
                        "reason": (
                            (
                                "health gate passed"
                                if selected_model == adapter.model
                                else f"health gate passed with fallback model "
                                f"{selected_model}"
                            )
                            if tool_smoke_passed
                            else "no eligible controller model emitted required tool call"
                        ),
                    }
                )
        elif healthy:
            selected_model = candidate_models[0]
            result.update(
                {
                    "selected_model": selected_model,
                    "model_switched": selected_model != adapter.model,
                }
            )
    except Exception as exc:
        result.update(
            {
                "healthy": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
        status_code = getattr(exc, "code", None)
        if status_code is not None:
            result["http_status"] = int(status_code)
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def set_controller_adapter_operational_state(
    conn: sqlite3.Connection,
    controller_adapter_value: str,
    *,
    enabled: bool,
    reason: Optional[str] = None,
    changed_by: Optional[str] = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Enable only after a successful live health gate; disabling is immediate."""
    ensure_schema(conn)
    adapter = resolve_controller_adapter(conn, controller_adapter_value)
    if adapter is None:
        raise SupervisorRegistryError(
            f"unknown controller adapter: {controller_adapter_value}"
        )
    probe = (
        probe_controller_adapter(adapter, timeout_seconds=timeout_seconds)
        if enabled
        else {
            "controller_adapter_id": adapter.id,
            "healthy": False,
            "reason": "disabled by operator",
            "checked_at": _now(),
            "latency_ms": 0,
        }
    )
    now = _now()
    effective_enabled = bool(enabled and probe.get("healthy"))
    selected_model = str(probe.get("selected_model") or "").strip()
    effective_model = (
        selected_model if effective_enabled and selected_model else adapter.model
    )
    model_changed = effective_model != adapter.model
    health_state = "healthy" if effective_enabled else "disabled" if not enabled else "unhealthy"
    with write_txn(conn):
        conn.execute(
            "UPDATE controller_adapters SET model=?,enabled=?,health_state=?,"
            "last_health_at=?,updated_at=? WHERE id=?",
            (
                effective_model,
                int(effective_enabled),
                health_state,
                now,
                now,
                adapter.id,
            ),
        )
        _append_adapter_event_in_txn(
            conn,
            kind="controller_adapter_state_changed",
            scope_type="all",
            scope_key="hermes",
            executor_id=adapter.id,
            details={
                "requested_enabled": bool(enabled),
                "effective_enabled": effective_enabled,
                "health_state": health_state,
                "health_gate": probe,
                "reason": reason,
                "previous_model": adapter.model,
                "effective_model": effective_model,
                "model_changed": model_changed,
            },
            created_by=changed_by,
        )
    return {
        "controller_adapter_id": adapter.id,
        "requested_enabled": bool(enabled),
        "enabled": effective_enabled,
        "health_state": health_state,
        "health_gate_passed": bool(probe.get("healthy")),
        "health_gate": probe,
        "previous_model": adapter.model,
        "effective_model": effective_model,
        "model_changed": model_changed,
    }


def set_controller_adapter_model(
    conn: sqlite3.Connection,
    controller_adapter_value: str,
    *,
    model: str,
    reason: Optional[str] = None,
    changed_by: Optional[str] = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Health-gate and persist an explicit model change for one controller."""
    ensure_schema(conn)
    adapter = resolve_controller_adapter(conn, controller_adapter_value)
    if adapter is None:
        raise SupervisorRegistryError(
            f"unknown controller adapter: {controller_adapter_value}"
        )
    requested_model = str(model or "").strip()
    if not requested_model:
        raise SupervisorRegistryError("controller model is required")
    free_suffix = str(adapter.metadata.get("free_model_suffix") or "-free")
    explicit_free_models = adapter.metadata.get("free_model_ids") or []
    if not isinstance(explicit_free_models, list):
        explicit_free_models = []
    if (
        adapter.metadata.get("anonymous_api")
        and not requested_model.endswith(free_suffix)
        and requested_model not in explicit_free_models
    ):
        raise SupervisorRegistryError(
            "anonymous OpenCode controller accepts only declared free models"
        )
    strict_metadata = dict(adapter.metadata)
    strict_metadata["dynamic_free_model_fallback"] = False
    strict_metadata["model_fallback_candidates"] = []
    candidate = replace(adapter, model=requested_model, metadata=strict_metadata)
    probe = probe_controller_adapter(candidate, timeout_seconds=timeout_seconds)
    changed = bool(probe.get("healthy"))
    now = _now()
    with write_txn(conn):
        if changed:
            conn.execute(
                "UPDATE controller_adapters SET model=?,health_state='healthy',"
                "last_health_at=?,updated_at=? WHERE id=?",
                (requested_model, now, now, adapter.id),
            )
        _append_adapter_event_in_txn(
            conn,
            kind=(
                "controller_adapter_model_changed"
                if changed
                else "controller_adapter_model_change_rejected"
            ),
            scope_type="all",
            scope_key="hermes",
            executor_id=adapter.id,
            details={
                "previous_model": adapter.model,
                "requested_model": requested_model,
                "changed": changed,
                "health_gate": probe,
                "reason": reason,
            },
            created_by=changed_by,
        )
    return {
        "controller_adapter_id": adapter.id,
        "previous_model": adapter.model,
        "requested_model": requested_model,
        "effective_model": requested_model if changed else adapter.model,
        "changed": changed,
        "health_gate": probe,
    }


def _append_adapter_event_in_txn(
    conn: sqlite3.Connection,
    *,
    kind: str,
    scope_type: Optional[str] = None,
    scope_key: Optional[str] = None,
    executor_id: Optional[str] = None,
    binding_id: Optional[str] = None,
    override_id: Optional[str] = None,
    task_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    created_by: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO adapter_events "
        "(kind,scope_type,scope_key,executor_id,binding_id,override_id,task_id,"
        "details_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            str(kind), scope_type, scope_key, executor_id, binding_id,
            override_id, task_id, _canonical_json(details or {}), created_by, _now(),
        ),
    )
    return int(cur.lastrowid)


def list_adapter_events(
    conn: sqlite3.Connection,
    *,
    scope_type: Optional[str] = None,
    scope_key: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if scope_type:
        clauses.append("scope_type=?")
        params.append(scope_type)
    if scope_key:
        clauses.append("scope_key=?")
        params.append(scope_key)
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 1000)))
    rows = conn.execute(
        "SELECT * FROM adapter_events" + where + " ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "kind": row["kind"],
            "scope_type": row["scope_type"],
            "scope_key": row["scope_key"],
            "executor_id": row["executor_id"],
            "binding_id": row["binding_id"],
            "override_id": row["override_id"],
            "task_id": row["task_id"],
            "details": _json_dict(row["details_json"]),
            "created_by": row["created_by"],
            "created_at": int(row["created_at"]),
        }
        for row in rows
    ]


def _static_executor_compatible(
    shell: RoleShell,
    executor: Executor,
    binding: Optional[Binding] = None,
) -> tuple[bool, str]:
    if executor.adapter_type == "manual":
        return False, "manual executors cannot be dispatched autonomously"
    allowed_adapters = set(_json_list(shell.contract.get("allowed_adapters")))
    if executor.adapter_type not in allowed_adapters:
        return False, f"adapter type {executor.adapter_type!r} is not allowed"
    effective = set(shell.allowed_capabilities) & set(executor.capabilities)
    if binding is not None and binding.capability_cap:
        effective &= set(binding.capability_cap)
    missing = sorted(set(shell.required_capabilities) - effective)
    if missing:
        return False, "missing required capabilities: " + ", ".join(missing)
    return True, ""


def assign_adapter(
    conn: sqlite3.Connection,
    *,
    shell_value: str,
    executor_value: str,
    responsibility: str = "candidate",
    priority: Optional[int] = None,
    weight: Optional[float] = None,
    note: Optional[str] = None,
    assigned_by: Optional[str] = None,
) -> Binding:
    """Add/strengthen a candidate or make it the shell's primary owner."""
    ensure_schema(conn)
    shell = resolve_shell(conn, shell_value)
    if shell is None:
        raise SupervisorRegistryError(f"unknown role shell: {shell_value}")
    active = get_shell(conn, shell_key=shell.shell_key)
    if active is None or active.id != shell.id:
        raise SupervisorRegistryError(
            f"role shell {shell.id} is not the active {shell.shell_key} version"
        )
    executor = resolve_executor(conn, executor_value)
    if executor is None:
        raise SupervisorRegistryError(f"unknown executor: {executor_value}")
    responsibility = str(responsibility or "candidate").strip().lower()
    if responsibility not in {"primary", "candidate"}:
        raise SupervisorRegistryError("responsibility must be primary or candidate")
    compatible, reason = _static_executor_compatible(shell, executor)
    if not compatible:
        raise SupervisorRegistryError(
            f"executor {executor.id} cannot own {shell.shell_key}: {reason}"
        )
    existing_row = conn.execute(
        "SELECT * FROM role_bindings WHERE shell_id=? AND executor_id=?",
        (shell.id, executor.id),
    ).fetchone()
    existing = Binding.from_row(existing_row) if existing_row else None
    binding = upsert_binding(
        conn,
        shell_id=shell.id,
        executor_id=executor.id,
        priority=(int(priority) if priority is not None else (existing.priority if existing else 0)),
        weight=(float(weight) if weight is not None else (existing.weight if existing else 1.0)),
        capability_cap=(existing.capability_cap if existing else ()),
        constraints=(existing.constraints if existing else {"auto_spawn": True}),
        responsibility=responsibility,
        assignment_note=note,
        assigned_by=assigned_by,
        binding_id=(existing.id if existing else None),
    )
    now = _now()
    with write_txn(conn):
        if responsibility == "primary":
            conn.execute(
                "UPDATE role_bindings SET responsibility='candidate',updated_at=? "
                "WHERE shell_id=? AND id!=? AND responsibility='primary'",
                (now, shell.id, binding.id),
            )
        conn.execute(
            "UPDATE role_bindings SET responsibility=?,assignment_note=?,assigned_by=?,"
            "updated_at=? WHERE id=?",
            (
                responsibility,
                (str(note).strip() if note else None),
                (str(assigned_by).strip() if assigned_by else None),
                now,
                binding.id,
            ),
        )
        _append_adapter_event_in_txn(
            conn,
            kind="adapter_assigned",
            scope_type="shell",
            scope_key=shell.shell_key,
            executor_id=executor.id,
            binding_id=binding.id,
            details={
                "role_shell_id": shell.id,
                "responsibility": responsibility,
                "priority": binding.priority,
                "weight": binding.weight,
                "note": note,
            },
            created_by=assigned_by,
        )
    return get_binding(conn, binding.id)  # type: ignore[return-value]


def _normalize_override_scope(
    conn: sqlite3.Connection,
    *,
    target: str,
    scope_type: Optional[str] = None,
) -> tuple[str, str, list[RoleShell]]:
    value = str(target or "").strip()
    explicit = str(scope_type or "").strip().lower() or None
    if explicit and explicit not in {"task", "shell", "all"}:
        raise SupervisorRegistryError("scope_type must be task, shell, or all")
    if explicit == "all" or (explicit is None and value.lower() == "all"):
        shells = list_shells(conn, active_only=True)
        if not shells:
            raise SupervisorRegistryError("no active role shells")
        return "all", "*", shells
    task_row = None
    if explicit in {None, "task"}:
        task_row = conn.execute(
            "SELECT id,role_shell_id FROM tasks WHERE id=?", (value,)
        ).fetchone()
    if task_row is not None:
        if not task_row["role_shell_id"]:
            raise SupervisorRegistryError(
                f"task {value} is not managed by a role-shell adapter"
            )
        shell = get_shell(conn, shell_id=task_row["role_shell_id"])
        if shell is None:
            raise SupervisorRegistryError(
                f"task {value} references a missing role shell"
            )
        return "task", value, [shell]
    if explicit == "task":
        raise SupervisorRegistryError(f"unknown task: {value}")
    shell = resolve_shell(conn, value)
    if shell is None:
        raise SupervisorRegistryError(f"unknown adapter target: {value}")
    active = get_shell(conn, shell_key=shell.shell_key)
    if active is None:
        raise SupervisorRegistryError(f"no active shell for {shell.shell_key}")
    return "shell", active.shell_key, [active]


def _validate_override_executor(
    conn: sqlite3.Connection,
    *,
    executor: Executor,
    shells: Iterable[RoleShell],
) -> None:
    failures: list[str] = []
    for shell in shells:
        row = conn.execute(
            "SELECT * FROM role_bindings WHERE shell_id=? AND executor_id=?",
            (shell.id, executor.id),
        ).fetchone()
        if row is None:
            failures.append(f"{shell.shell_key}: no binding")
            continue
        binding = Binding.from_row(row)
        compatible, reason = _static_executor_compatible(shell, executor, binding)
        if not binding.enabled:
            failures.append(f"{shell.shell_key}: binding disabled")
        elif not compatible:
            failures.append(f"{shell.shell_key}: {reason}")
    if failures:
        raise SupervisorRegistryError(
            f"executor {executor.id} cannot be forced for target: " + "; ".join(failures)
        )


def create_adapter_override_in_txn(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_key: str,
    executor_id: str,
    mode: str,
    duration_seconds: Optional[int] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> AdapterOverride:
    """Insert a validated override while the caller already owns a write txn."""
    mode = str(mode or "").strip().lower()
    if mode not in {"once", "temporary", "permanent"}:
        raise SupervisorRegistryError("override mode must be once, temporary, or permanent")
    now = _now()
    if mode == "temporary":
        if duration_seconds is None or int(duration_seconds) <= 0:
            raise SupervisorRegistryError(
                "temporary override requires positive duration_seconds"
            )
        expires_at = now + int(duration_seconds)
    else:
        expires_at = None
    remaining_uses = 1 if mode == "once" else None
    superseded = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM adapter_overrides WHERE scope_type=? AND scope_key=? "
            "AND enabled=1",
            (scope_type, scope_key),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE adapter_overrides SET enabled=0,updated_at=? "
        "WHERE scope_type=? AND scope_key=? AND enabled=1",
        (now, scope_type, scope_key),
    )
    override_id = _new_id("adapter_override")
    conn.execute(
        "INSERT INTO adapter_overrides "
        "(id,scope_type,scope_key,executor_id,mode,expires_at,remaining_uses,reason,"
        "created_by,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
        (
            override_id, scope_type, scope_key, executor_id, mode, expires_at,
            remaining_uses, (str(reason).strip() if reason else None),
            (str(created_by).strip() if created_by else None), now, now,
        ),
    )
    _append_adapter_event_in_txn(
        conn,
        kind="adapter_override_created",
        scope_type=scope_type,
        scope_key=scope_key,
        executor_id=executor_id,
        override_id=override_id,
        task_id=(scope_key if scope_type == "task" else None),
        details={
            "mode": mode,
            "expires_at": expires_at,
            "remaining_uses": remaining_uses,
            "reason": reason,
            "superseded_override_ids": superseded,
        },
        created_by=created_by,
    )
    row = conn.execute(
        "SELECT * FROM adapter_overrides WHERE id=?", (override_id,)
    ).fetchone()
    return AdapterOverride.from_row(row)


def create_adapter_override(
    conn: sqlite3.Connection,
    *,
    target: str,
    executor_value: str,
    mode: str,
    scope_type: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> AdapterOverride:
    """Force one task, one role, or all roles to a compatible executor."""
    ensure_schema(conn)
    normalized_type, key, shells = _normalize_override_scope(
        conn, target=target, scope_type=scope_type
    )
    executor = resolve_executor(conn, executor_value)
    if executor is None:
        raise SupervisorRegistryError(f"unknown executor: {executor_value}")
    _validate_override_executor(conn, executor=executor, shells=shells)
    with write_txn(conn):
        override = create_adapter_override_in_txn(
            conn,
            scope_type=normalized_type,
            scope_key=key,
            executor_id=executor.id,
            mode=mode,
            duration_seconds=duration_seconds,
            reason=reason,
            created_by=created_by,
        )
        if normalized_type == "task":
            from hermes_cli.kanban_db import _append_event

            _append_event(
                conn,
                key,
                "adapter_override_created",
                {
                    "override_id": override.id,
                    "executor_id": executor.id,
                    "mode": override.mode,
                    "expires_at": override.expires_at,
                    "reason": override.reason,
                },
            )
    return override


def finalize_task_once_overrides_in_txn(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    terminal_status: str,
) -> list[str]:
    """Consume task-scoped ``once`` overrides at the card boundary.

    A task override means "use this executor for this card", not "use this
    executor for only the card's first process attempt".  Crash/timeout/spawn
    retries therefore keep the override.  The caller owns the write
    transaction and invokes this only when the card becomes truly terminal.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='adapter_overrides'"
    ).fetchone()
    if table_exists is None:
        return []
    now = _now()
    rows = conn.execute(
        "SELECT id,executor_id FROM adapter_overrides "
        "WHERE scope_type='task' AND scope_key=? AND mode='once' "
        "AND enabled=1 AND (remaining_uses IS NULL OR remaining_uses>0)",
        (str(task_id),),
    ).fetchall()
    consumed: list[str] = []
    for row in rows:
        override_id = str(row["id"])
        cur = conn.execute(
            "UPDATE adapter_overrides SET remaining_uses=0,enabled=0,updated_at=? "
            "WHERE id=? AND enabled=1",
            (now, override_id),
        )
        if cur.rowcount != 1:
            continue
        _append_adapter_event_in_txn(
            conn,
            kind="adapter_override_consumed",
            scope_type="task",
            scope_key=str(task_id),
            executor_id=str(row["executor_id"]),
            override_id=override_id,
            task_id=str(task_id),
            details={
                "terminal_status": str(terminal_status),
                "remaining_uses_after": 0,
            },
            created_by="kanban-terminal-transition",
        )
        consumed.append(override_id)
    return consumed


def get_adapter_override(
    conn: sqlite3.Connection, override_id: str
) -> Optional[AdapterOverride]:
    row = conn.execute(
        "SELECT * FROM adapter_overrides WHERE id=?", (override_id,)
    ).fetchone()
    return AdapterOverride.from_row(row) if row else None


def list_adapter_overrides(
    conn: sqlite3.Connection,
    *,
    include_inactive: bool = False,
    scope_type: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> list[AdapterOverride]:
    ensure_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        now = _now()
        clauses.extend(
            [
                "enabled=1",
                "(expires_at IS NULL OR expires_at>?)",
                "(remaining_uses IS NULL OR remaining_uses>0)",
            ]
        )
        params.append(now)
    if scope_type:
        clauses.append("scope_type=?")
        params.append(scope_type)
    if scope_key:
        clauses.append("scope_key=?")
        params.append(scope_key)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT * FROM adapter_overrides" + where + " ORDER BY created_at DESC,id DESC",
        params,
    ).fetchall()
    return [AdapterOverride.from_row(row) for row in rows]


def clear_adapter_override(
    conn: sqlite3.Connection,
    override_id: str,
    *,
    cleared_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    ensure_schema(conn)
    override = get_adapter_override(conn, override_id)
    if override is None:
        return False
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE adapter_overrides SET enabled=0,updated_at=? WHERE id=? AND enabled=1",
            (now, override_id),
        )
        if cur.rowcount:
            _append_adapter_event_in_txn(
                conn,
                kind="adapter_override_cleared",
                scope_type=override.scope_type,
                scope_key=override.scope_key,
                executor_id=override.executor_id,
                override_id=override.id,
                task_id=(override.scope_key if override.scope_type == "task" else None),
                details={"reason": reason},
                created_by=cleared_by,
            )
            if override.scope_type == "task":
                from hermes_cli.kanban_db import _append_event

                _append_event(
                    conn,
                    override.scope_key,
                    "adapter_override_cleared",
                    {
                        "override_id": override.id,
                        "executor_id": override.executor_id,
                        "reason": reason,
                    },
                )
    return cur.rowcount == 1


def get_controller_override(
    conn: sqlite3.Connection, override_id: str
) -> Optional[ControllerOverride]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM controller_overrides WHERE id=?", (str(override_id),)
    ).fetchone()
    return ControllerOverride.from_row(row) if row else None


def list_controller_overrides(
    conn: sqlite3.Connection,
    *,
    include_inactive: bool = False,
    scope_type: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> list[ControllerOverride]:
    ensure_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.extend(
            [
                "enabled=1",
                "(expires_at IS NULL OR expires_at>?)",
                "(remaining_uses IS NULL OR remaining_uses>0)",
            ]
        )
        params.append(_now())
    if scope_type:
        clauses.append("scope_type=?")
        params.append(str(scope_type))
    if scope_key:
        clauses.append("scope_key=?")
        params.append(str(scope_key))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT * FROM controller_overrides" + where
        + " ORDER BY created_at DESC,id DESC",
        params,
    ).fetchall()
    return [ControllerOverride.from_row(row) for row in rows]


def create_controller_override(
    conn: sqlite3.Connection,
    *,
    controller_adapter_value: str,
    mode: str,
    session_id: Optional[str] = None,
    scope_type: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> ControllerOverride:
    """Persist a controller switch for one conversation or the whole root."""
    ensure_schema(conn)
    adapter = resolve_controller_adapter(conn, controller_adapter_value)
    if adapter is None:
        raise SupervisorRegistryError(
            f"unknown controller adapter: {controller_adapter_value}"
        )
    if not adapter.routable():
        raise SupervisorRegistryError(
            f"controller adapter {adapter.id} is not routable "
            f"(enabled={adapter.enabled}, health={adapter.health_state})"
        )
    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in {"once", "temporary", "permanent"}:
        raise SupervisorRegistryError(
            "controller override mode must be once, temporary, or permanent"
        )
    clean_scope = str(scope_type or "").strip().lower()
    if not clean_scope:
        clean_scope = "all" if clean_mode == "permanent" else "session"
    if clean_scope not in {"session", "all"}:
        raise SupervisorRegistryError("controller scope_type must be session or all")
    clean_session_id = str(session_id or "").strip()
    if clean_scope == "session" and not clean_session_id:
        raise SupervisorRegistryError(
            "session-scoped controller override requires an active session"
        )
    scope_key = clean_session_id if clean_scope == "session" else "*"
    now = _now()
    expires_at: Optional[int] = None
    if clean_mode == "temporary":
        if duration_seconds is None or int(duration_seconds) <= 0:
            raise SupervisorRegistryError(
                "temporary controller override requires positive duration_seconds"
            )
        expires_at = now + int(duration_seconds)
    remaining_uses = 1 if clean_mode == "once" else None
    override_id = _new_id("controller_override")
    with write_txn(conn):
        superseded = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM controller_overrides WHERE scope_type=? "
                "AND scope_key=? AND enabled=1",
                (clean_scope, scope_key),
            ).fetchall()
        ]
        conn.execute(
            "UPDATE controller_overrides SET enabled=0,updated_at=? "
            "WHERE scope_type=? AND scope_key=? AND enabled=1",
            (now, clean_scope, scope_key),
        )
        conn.execute(
            "INSERT INTO controller_overrides "
            "(id,scope_type,scope_key,controller_adapter_id,mode,expires_at,"
            "remaining_uses,reason,created_by,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
            (
                override_id,
                clean_scope,
                scope_key,
                adapter.id,
                clean_mode,
                expires_at,
                remaining_uses,
                (str(reason).strip() if reason else None),
                (str(created_by).strip() if created_by else None),
                now,
                now,
            ),
        )
        _append_adapter_event_in_txn(
            conn,
            kind="controller_override_created",
            scope_type=clean_scope,
            scope_key=scope_key,
            executor_id=adapter.id,
            override_id=override_id,
            details={
                "mode": clean_mode,
                "expires_at": expires_at,
                "remaining_uses": remaining_uses,
                "reason": reason,
                "fallback_adapter_id": adapter.fallback_adapter_id,
                "superseded_override_ids": superseded,
            },
            created_by=created_by,
        )
    return get_controller_override(conn, override_id)  # type: ignore[return-value]


def resolve_controller_selection(
    conn: sqlite3.Connection, *, session_id: Optional[str] = None
) -> Optional[tuple[ControllerOverride, ControllerAdapter]]:
    """Resolve session override before global override without consuming it."""
    ensure_schema(conn)
    candidates: list[ControllerOverride] = []
    clean_session_id = str(session_id or "").strip()
    if clean_session_id:
        candidates.extend(
            list_controller_overrides(
                conn, scope_type="session", scope_key=clean_session_id
            )
        )
    candidates.extend(
        list_controller_overrides(conn, scope_type="all", scope_key="*")
    )
    if not candidates:
        return None
    override = candidates[0]
    adapter = get_controller_adapter(conn, override.controller_adapter_id)
    if adapter is None:
        raise SupervisorRegistryError(
            f"controller override {override.id} references a missing adapter"
        )
    return override, adapter


def controller_fallback_chain(
    conn: sqlite3.Connection, adapter: ControllerAdapter
) -> list[ControllerAdapter]:
    """Follow the explicit fallback chain, stopping on cycles or unavailable nodes."""
    chain: list[ControllerAdapter] = []
    seen = {adapter.id}
    next_id = adapter.fallback_adapter_id
    while next_id:
        if next_id in seen:
            raise SupervisorRegistryError(
                f"controller fallback cycle detected at {next_id}"
            )
        seen.add(next_id)
        item = get_controller_adapter(conn, next_id)
        if item is None:
            raise SupervisorRegistryError(
                f"missing controller fallback adapter: {next_id}"
            )
        if item.routable():
            chain.append(item)
        next_id = item.fallback_adapter_id
    return chain


def clear_controller_override(
    conn: sqlite3.Connection,
    override_id: str,
    *,
    cleared_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    ensure_schema(conn)
    override = get_controller_override(conn, override_id)
    if override is None:
        return False
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE controller_overrides SET enabled=0,updated_at=? "
            "WHERE id=? AND enabled=1",
            (now, override.id),
        )
        if cur.rowcount:
            _append_adapter_event_in_txn(
                conn,
                kind="controller_override_cleared",
                scope_type=override.scope_type,
                scope_key=override.scope_key,
                executor_id=override.controller_adapter_id,
                override_id=override.id,
                details={"reason": reason},
                created_by=cleared_by,
            )
    return cur.rowcount == 1


def record_controller_override_turn(
    conn: sqlite3.Connection,
    *,
    override_id: str,
    session_id: Optional[str],
    actual_provider: Optional[str],
    actual_model: Optional[str],
    fallback_active: bool,
    failed: bool,
) -> bool:
    """Audit one actual controller turn and consume a one-shot override."""
    ensure_schema(conn)
    override = get_controller_override(conn, override_id)
    if override is None:
        return False
    now = _now()
    with write_txn(conn):
        if override.mode == "once" and override.enabled:
            conn.execute(
                "UPDATE controller_overrides SET remaining_uses=0,enabled=0,"
                "updated_at=? WHERE id=?",
                (now, override.id),
            )
        kind = (
            "controller_override_failback"
            if fallback_active
            else "controller_override_turn_failed"
            if failed
            else "controller_override_used"
        )
        _append_adapter_event_in_txn(
            conn,
            kind=kind,
            scope_type=override.scope_type,
            scope_key=override.scope_key,
            executor_id=override.controller_adapter_id,
            override_id=override.id,
            details={
                "session_id": session_id,
                "mode": override.mode,
                "actual_provider": actual_provider,
                "actual_model": actual_model,
                "fallback_active": bool(fallback_active),
                "failed": bool(failed),
                "remaining_uses": 0 if override.mode == "once" else None,
            },
            created_by="gateway",
        )
    return True


def record_controller_resolution_failback(
    conn: sqlite3.Connection,
    *,
    override: ControllerOverride,
    requested_adapter: ControllerAdapter,
    fallback_adapter: ControllerAdapter,
    session_id: Optional[str],
    reason: str,
) -> None:
    """Audit a pre-request auth/runtime failure that returned to the fallback."""
    with write_txn(conn):
        _append_adapter_event_in_txn(
            conn,
            kind="controller_override_failback",
            scope_type=override.scope_type,
            scope_key=override.scope_key,
            executor_id=requested_adapter.id,
            override_id=override.id,
            details={
                "session_id": session_id,
                "requested_adapter_id": requested_adapter.id,
                "fallback_adapter_id": fallback_adapter.id,
                "stage": "runtime_resolution",
                "reason": str(reason),
            },
            created_by="gateway",
        )


def rebind_session_controller_overrides(
    conn: sqlite3.Connection,
    *,
    old_session_id: str,
    new_session_id: str,
) -> list[str]:
    """Carry active temporary/permanent session switches across compression splits."""
    old_id = str(old_session_id or "").strip()
    new_id = str(new_session_id or "").strip()
    if not old_id or not new_id or old_id == new_id:
        return []
    overrides = list_controller_overrides(
        conn, scope_type="session", scope_key=old_id
    )
    if not overrides:
        return []
    moved: list[str] = []
    now = _now()
    with write_txn(conn):
        conn.execute(
            "UPDATE controller_overrides SET enabled=0,updated_at=? "
            "WHERE scope_type='session' AND scope_key=? AND enabled=1",
            (now, new_id),
        )
        for override in overrides:
            conn.execute(
                "UPDATE controller_overrides SET scope_key=?,updated_at=? WHERE id=?",
                (new_id, now, override.id),
            )
            moved.append(override.id)
            _append_adapter_event_in_txn(
                conn,
                kind="controller_override_session_rebound",
                scope_type="session",
                scope_key=new_id,
                executor_id=override.controller_adapter_id,
                override_id=override.id,
                details={"old_session_id": old_id, "new_session_id": new_id},
                created_by="gateway",
            )
    return moved


def list_recent_adapter_tasks(
    conn: sqlite3.Connection,
    *,
    session_id: Optional[str] = None,
    limit: int = 10,
    completed_only: bool = False,
    fallback_global: bool = True,
) -> list[dict[str, Any]]:
    """Return recent role-shell cards, preferring the current conversation."""
    ensure_schema(conn)
    limit = max(1, min(int(limit), 100))
    executors = {item.id: item for item in list_executors(conn)}

    def _query(session: Optional[str]) -> list[sqlite3.Row]:
        clauses = ["t.role_shell_id IS NOT NULL", "t.status != 'archived'"]
        params: list[Any] = []
        if completed_only:
            clauses.append("t.status = 'done'")
        if session:
            clauses.append("t.session_id = ?")
            params.append(session)
        params.append(limit)
        return conn.execute(
            "SELECT t.*, r.id AS recent_run_id, r.status AS recent_run_status, "
            "r.outcome AS recent_run_outcome, r.executor_id AS recent_executor_id, "
            "r.started_at AS recent_run_started_at, r.ended_at AS recent_run_ended_at "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = ("
            "SELECT rr.id FROM task_runs rr WHERE rr.task_id=t.id "
            "ORDER BY rr.id DESC LIMIT 1) WHERE "
            + " AND ".join(clauses)
            + " ORDER BY COALESCE(t.completed_at,t.started_at,t.created_at) DESC, "
            "t.created_at DESC,t.id DESC LIMIT ?",
            params,
        ).fetchall()

    normalized_session = str(session_id or "").strip() or None
    rows = _query(normalized_session)
    used_fallback = False
    if normalized_session and not rows and fallback_global:
        rows = _query(None)
        used_fallback = True
    results: list[dict[str, Any]] = []
    for row in rows:
        shell = get_shell(conn, shell_id=row["role_shell_id"])
        executor_id = row["recent_executor_id"]
        executor = executors.get(executor_id) if executor_id else None
        results.append(
            {
                "task_id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "session_id": row["session_id"],
                "role_shell_id": row["role_shell_id"],
                "shell_key": shell.shell_key if shell else None,
                "created_at": int(row["created_at"]),
                "started_at": (
                    int(row["started_at"]) if row["started_at"] is not None else None
                ),
                "completed_at": (
                    int(row["completed_at"])
                    if row["completed_at"] is not None
                    else None
                ),
                "last_run": (
                    {
                        "run_id": int(row["recent_run_id"]),
                        "status": row["recent_run_status"],
                        "outcome": row["recent_run_outcome"],
                        "executor_id": executor_id,
                        "executor_name": executor.name if executor else None,
                        "runtime": (
                            executor_runtime_descriptor(executor) if executor else None
                        ),
                        "started_at": row["recent_run_started_at"],
                        "ended_at": row["recent_run_ended_at"],
                    }
                    if row["recent_run_id"] is not None
                    else None
                ),
                "rerunnable": row["status"] == "done",
                "session_match": bool(
                    normalized_session
                    and not used_fallback
                    and row["session_id"] == normalized_session
                ),
            }
        )
    return results


_RECENT_TASK_ALIASES = {
    "",
    "latest",
    "last",
    "recent",
    "latest_done",
    "방금",
    "최근",
    "마지막",
}


def resolve_adapter_task_reference(
    conn: sqlite3.Connection,
    task_reference: Optional[str],
    *,
    session_id: Optional[str] = None,
    completed_only: bool = False,
) -> dict[str, Any]:
    """Resolve an exact card id or conversational latest/last reference."""
    from hermes_cli import kanban_db as kb

    reference = str(task_reference or "").strip()
    if reference.lower() in _RECENT_TASK_ALIASES:
        rows = list_recent_adapter_tasks(
            conn,
            session_id=session_id,
            limit=1,
            completed_only=completed_only,
            fallback_global=True,
        )
        if not rows:
            qualifier = "completed " if completed_only else ""
            raise SupervisorRegistryError(f"no recent {qualifier}adapter-managed task")
        return {
            "task_id": rows[0]["task_id"],
            "resolved_from": "latest_completed" if completed_only else "latest",
            "session_match": rows[0]["session_match"],
            "recent_task": rows[0],
        }
    task = kb.get_task(conn, reference)
    if task is None:
        raise SupervisorRegistryError(f"unknown task: {reference}")
    if not task.role_shell_id:
        raise SupervisorRegistryError(
            f"task {reference} is not managed by a role-shell adapter"
        )
    return {
        "task_id": task.id,
        "resolved_from": "exact_task_id",
        "session_match": bool(session_id and task.session_id == session_id),
        "recent_task": None,
    }


def adapter_registry_view(
    conn: sqlite3.Connection,
    *,
    history_limit: int = 50,
    session_id: Optional[str] = None,
    recent_limit: int = 10,
) -> dict[str, Any]:
    """Return the shared controller-plus-seven-role view for CLI, chat, and web."""
    ensure_schema(conn)
    active_overrides = list_adapter_overrides(conn)
    global_overrides = [o for o in active_overrides if o.scope_type == "all"]
    shell_overrides = {
        o.scope_key: o for o in active_overrides if o.scope_type == "shell"
    }
    executors = {item.id: item for item in list_executors(conn)}
    executor_views = {key: _executor_view(item) for key, item in executors.items()}
    route_health_by_shell = {
        str(row.get("role_shell_id")): row for row in build_shell_health(conn)
    }
    shells: list[dict[str, Any]] = []
    control_slots: list[dict[str, Any]] = []
    configured_controller_runtime = controller_runtime_descriptor()
    controller_candidates = list_controller_adapters(conn)
    controller_candidate_views = [
        _controller_adapter_dict(item) for item in controller_candidates
    ]
    controller_selection = resolve_controller_selection(conn, session_id=session_id)
    if controller_selection is not None:
        controller_override, selected_controller = controller_selection
        controller_runtime = controller_adapter_runtime_descriptor(selected_controller)
        controller_selection_source = f"{controller_override.scope_type}_override"
        controller_active_override = _controller_override_dict(controller_override)
        effective_controller_id = selected_controller.id
    else:
        controller_runtime = configured_controller_runtime
        controller_selection_source = "controller_config"
        controller_active_override = None
        effective_controller_id = next(
            (
                item.id
                for item in controller_candidates
                if item.provider == configured_controller_runtime.get("provider")
                and item.model == configured_controller_runtime.get("model")
            ),
            None,
        )
    control_slots.append(
        {
            "slot_key": "hermes",
            "slot_type": "controller",
            "name": "Hermes Control Tower",
            "delegation_only": True,
            "profile": "default",
            "runtime": controller_runtime,
            "configured_runtime": configured_controller_runtime,
            "display_label": f"Hermes · {controller_runtime['display_label']}",
            "selection_source": controller_selection_source,
            "effective_controller_adapter_id": effective_controller_id,
            "active_override": controller_active_override,
            "alternatives": controller_candidate_views,
            "controls": ["supervisor_adapter.switch", "/model", "/reasoning"],
            "supported_switch_modes": ["once", "temporary", "permanent"],
        }
    )
    for shell in list_shells(conn, active_only=True):
        bindings = list_bindings(conn, shell_id=shell.id)
        rows = []
        for binding in bindings:
            executor = executors.get(binding.executor_id)
            rows.append(
                {
                    "binding_id": binding.id,
                    "executor_id": binding.executor_id,
                    "executor_name": executor.name if executor else None,
                    "display_label": (
                        executor_views[binding.executor_id]["display_label"]
                        if executor else None
                    ),
                    "runtime": (
                        executor_views[binding.executor_id]["runtime"]
                        if executor else None
                    ),
                    "responsibility": binding.responsibility,
                    "priority": binding.priority,
                    "weight": binding.weight,
                    "enabled": binding.enabled,
                    "executor_enabled": executor.enabled if executor else False,
                    "health_state": executor.health_state if executor else "missing",
                    "assignment_note": binding.assignment_note,
                    "assigned_by": binding.assigned_by,
                }
            )
        primary = [row for row in rows if row["responsibility"] == "primary"]
        candidates = [row for row in rows if row["responsibility"] != "primary"]
        shell_override = shell_overrides.get(shell.shell_key)
        global_override = global_overrides[0] if global_overrides else None
        effective_override = shell_override or global_override
        if effective_override:
            effective_executor_id = effective_override.executor_id
            selection_source = f"{effective_override.scope_type}_override"
        else:
            effective_row = (primary or candidates or [None])[0]
            effective_executor_id = (
                effective_row["executor_id"] if effective_row else None
            )
            selection_source = (
                "primary" if primary else "candidate" if candidates else "unbound"
            )
        effective_executor = (
            executor_views.get(effective_executor_id) if effective_executor_id else None
        )
        shell_row = {
            "shell_key": shell.shell_key,
            "role_shell_id": shell.id,
            "name": shell.name,
            "primary": primary,
            "candidates": candidates,
            "active_override": (
                _override_dict(shell_override) if shell_override else None
            ),
            "effective_executor_id": effective_executor_id,
            "effective_executor": effective_executor,
            "selection_source": selection_source,
            "route_health": route_health_by_shell.get(shell.id),
        }
        shells.append(shell_row)
        control_slots.append(
            {
                "slot_key": shell.shell_key,
                "slot_type": "role_shell",
                "name": shell.name,
                "role_shell_id": shell.id,
                "delegation_only": False,
                "executor_id": effective_executor_id,
                "runtime": (
                    effective_executor.get("runtime") if effective_executor else None
                ),
                "display_label": (
                    effective_executor.get("display_label")
                    if effective_executor
                    else "Unbound"
                ),
                "selection_source": selection_source,
                "route_health": route_health_by_shell.get(shell.id),
                "active_override": (
                    _override_dict(effective_override) if effective_override else None
                ),
                "alternatives": primary + candidates,
                "supported_switch_modes": ["once", "temporary", "permanent"],
            }
        )
    return {
        "schema": "hermes.supervisor.adapters.v2",
        "controller": control_slots[0],
        "control_slot_count": len(control_slots),
        "control_slots": control_slots,
        "shells": shells,
        "executors": list(executor_views.values()),
        "controller_adapters": controller_candidate_views,
        "active_controller_override": controller_active_override,
        "global_override": (
            _override_dict(global_overrides[0]) if global_overrides else None
        ),
        "active_overrides": [_override_dict(item) for item in active_overrides],
        "history": list_adapter_events(conn, limit=history_limit),
        "recent_tasks": list_recent_adapter_tasks(
            conn,
            session_id=session_id,
            limit=recent_limit,
            fallback_global=True,
        ),
    }


def _override_dict(item: AdapterOverride) -> dict[str, Any]:
    return {
        "override_id": item.id,
        "scope_type": item.scope_type,
        "scope_key": item.scope_key,
        "executor_id": item.executor_id,
        "mode": item.mode,
        "expires_at": item.expires_at,
        "remaining_uses": item.remaining_uses,
        "reason": item.reason,
        "created_by": item.created_by,
        "enabled": item.enabled,
        "active": item.active(),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _controller_override_dict(item: ControllerOverride) -> dict[str, Any]:
    return {
        "override_id": item.id,
        "scope_type": item.scope_type,
        "scope_key": item.scope_key,
        "controller_adapter_id": item.controller_adapter_id,
        "mode": item.mode,
        "expires_at": item.expires_at,
        "remaining_uses": item.remaining_uses,
        "reason": item.reason,
        "created_by": item.created_by,
        "enabled": item.enabled,
        "active": item.active(),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def reissue_task_with_adapter(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    executor_value: Optional[str] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    """Create a revision child and reuse the prior adapter when none is named."""
    from pathlib import Path

    from hermes_cli import kanban_db as kb

    ensure_schema(conn)
    original = kb.get_task(conn, str(task_id))
    if original is None:
        raise SupervisorRegistryError(f"unknown task: {task_id}")
    if original.status != "done":
        raise SupervisorRegistryError(
            f"task {task_id} is {original.status!r}; adapter reissue requires a completed card"
        )
    if not original.role_shell_id:
        raise SupervisorRegistryError(
            f"task {task_id} is not managed by a role-shell adapter"
        )
    old_shell = get_shell(conn, shell_id=original.role_shell_id)
    if old_shell is None:
        raise SupervisorRegistryError(
            f"task {task_id} references a missing role shell"
        )
    active_shell = get_shell(conn, shell_key=old_shell.shell_key)
    if active_shell is None:
        raise SupervisorRegistryError(
            f"no active role shell for {old_shell.shell_key}"
        )
    requested_executor = str(executor_value or "").strip() or None
    executor_source = "operator_request"
    if requested_executor is None:
        latest = kb.latest_run(conn, original.id)
        if latest is not None and latest.executor_id:
            requested_executor = latest.executor_id
            executor_source = "previous_run"
        else:
            requested_executor = select_binding(
                conn, active_shell.id, task_id=original.id
            ).executor.id
            executor_source = "current_binding"
    executor = resolve_executor(conn, requested_executor)
    if executor is None:
        raise SupervisorRegistryError(f"unknown executor: {requested_executor}")
    _validate_override_executor(conn, executor=executor, shells=[active_shell])
    workspace_kind = "scratch"
    workspace_path = None
    if original.workspace_kind == "dir" and original.workspace_path:
        candidate = Path(original.workspace_path).expanduser()
        if candidate.is_dir():
            workspace_kind = "dir"
            workspace_path = str(candidate)
    reason_text = str(reason or "operator requested adapter rework").strip()
    revision_body = (
        f"Revision of completed Kanban card {original.id}.\n"
        f"Adapter re-request reason: {reason_text}.\n\n"
        f"{original.body or ''}"
    ).rstrip()
    revision_id = kb.create_task(
        conn,
        title=f"Rework: {original.title}",
        body=revision_body,
        created_by=created_by or "supervisor",
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        tenant=original.tenant,
        priority=original.priority,
        parents=[original.id],
        max_runtime_seconds=original.max_runtime_seconds,
        skills=original.skills,
        max_retries=original.max_retries,
        goal_mode=original.goal_mode,
        goal_max_turns=original.goal_max_turns,
        session_id=original.session_id,
        role_shell_id=active_shell.id,
        adapter_executor_id=executor.id,
        adapter_reason=reason_text,
        adapter_created_by=created_by or "supervisor",
    )
    override = list_adapter_overrides(
        conn, scope_type="task", scope_key=revision_id
    )[0]
    with write_txn(conn):
        kb._append_event(
            conn,
            original.id,
            "adapter_rerun_requested",
            {
                "revision_task_id": revision_id,
                "executor_id": executor.id,
                "override_id": override.id,
                "reason": reason_text,
                "created_by": created_by,
            },
        )
        kb._append_event(
            conn,
            revision_id,
            "adapter_rerun_created",
            {
                "original_task_id": original.id,
                "executor_id": executor.id,
                "override_id": override.id,
                "reason": reason_text,
                "created_by": created_by,
            },
        )
        _append_adapter_event_in_txn(
            conn,
            kind="adapter_task_reissued",
            scope_type="task",
            scope_key=revision_id,
            executor_id=executor.id,
            override_id=override.id,
            task_id=revision_id,
            details={
                "original_task_id": original.id,
                "revision_task_id": revision_id,
                "reason": reason_text,
            },
            created_by=created_by,
        )
    return {
        "original_task_id": original.id,
        "revision_task_id": revision_id,
        "role_shell_id": active_shell.id,
        "shell_key": active_shell.shell_key,
        "executor_id": executor.id,
        "executor_source": executor_source,
        "runtime": executor_runtime_descriptor(executor),
        "override_id": override.id,
        "status": kb.get_task(conn, revision_id).status,
    }


def inspect_task_adapter(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Explain one card's adapter choice and step/run failures."""
    from hermes_cli import kanban_db as kb

    ensure_schema(conn)
    task = kb.get_task(conn, str(task_id))
    if task is None:
        raise SupervisorRegistryError(f"unknown task: {task_id}")
    shell = get_shell(conn, shell_id=task.role_shell_id) if task.role_shell_id else None
    active_override = (
        _matching_adapter_override(conn, shell=shell, task_id=task.id)
        if shell is not None
        else None
    )
    available_executors: list[dict[str, Any]] = []
    if shell is not None:
        for binding in list_bindings(conn, shell_id=shell.id):
            executor = get_executor(conn, binding.executor_id)
            if executor is None:
                continue
            compatible, reason = _static_executor_compatible(shell, executor, binding)
            runtime = executor_runtime_descriptor(executor)
            available_executors.append(
                {
                    "executor_id": executor.id,
                    "name": executor.name,
                    "display_label": f"{executor.name} · {runtime['display_label']}",
                    "runtime": runtime,
                    "binding_id": binding.id,
                    "responsibility": binding.responsibility,
                    "priority": binding.priority,
                    "enabled": binding.enabled and executor.enabled,
                    "health_state": executor.health_state,
                    "compatible": compatible,
                    "incompatibility_reason": reason or None,
                }
            )
    runs = kb.list_runs(conn, task.id)
    executor_cache = {item.id: item for item in list_executors(conn)}
    events = kb.list_events(conn, task.id)
    receipt_row = conn.execute(
        "SELECT id,status,receipt_json,created_at FROM run_receipts "
        "WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task.id,),
    ).fetchone()
    receipt_payload: Optional[dict[str, Any]] = None
    if receipt_row is not None:
        try:
            parsed_receipt = json.loads(receipt_row["receipt_json"])
            if isinstance(parsed_receipt, dict):
                receipt_payload = parsed_receipt
        except (TypeError, ValueError):
            receipt_payload = None
    stored_result = task.result.strip() if task.result and task.result.strip() else None
    recovered_result = receipt_delivery_body(receipt_payload)
    delivery_result = stored_result or recovered_result
    delivery_source = (
        "task.result"
        if stored_result
        else ("receipt.outputs" if recovered_result else None)
    )
    return {
        "task": {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "role_shell_id": task.role_shell_id,
            "shell_key": shell.shell_key if shell else None,
            "current_step_key": task.current_step_key,
            "last_failure_error": task.last_failure_error,
            "consecutive_failures": task.consecutive_failures,
        },
        "delivery": {
            "has_user_facing_output": bool(delivery_result),
            "source": delivery_source,
            "result": delivery_result,
            "result_len": len(delivery_result) if delivery_result else 0,
            "receipt_id": int(receipt_row["id"]) if receipt_row is not None else None,
            "receipt_status": receipt_row["status"] if receipt_row is not None else None,
            "receipt_created_at": (
                int(receipt_row["created_at"]) if receipt_row is not None else None
            ),
            "receipt_outputs": (
                receipt_payload.get("outputs", []) if receipt_payload else []
            ),
        },
        "effective_override": (
            _override_dict(active_override) if active_override is not None else None
        ),
        "available_executors": available_executors,
        "runs": [
            {
                "run_id": run.id,
                "step_key": run.step_key,
                "status": run.status,
                "outcome": run.outcome,
                "executor_id": run.executor_id,
                "runtime": (
                    executor_runtime_descriptor(executor_cache[run.executor_id])
                    if run.executor_id in executor_cache else None
                ),
                "binding_id": run.binding_id,
                "adapter_override_id": run.adapter_override_id,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "summary": run.summary,
                "error": run.error,
            }
            for run in runs
        ],
        "events": [
            {
                "event_id": event.id,
                "run_id": event.run_id,
                "kind": event.kind,
                "payload": event.payload,
                "created_at": event.created_at,
            }
            for event in events
        ],
        "adapter_history": list_adapter_events(conn, task_id=task.id, limit=100),
    }


def active_run_count(conn: sqlite3.Connection, executor_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs WHERE executor_id=? "
        "AND status='running' AND ended_at IS NULL",
        (executor_id,),
    ).fetchone()
    return int(row["n"] if row else 0)


def _binding_eligible(
    conn: sqlite3.Connection,
    shell: RoleShell,
    binding: Binding,
    executor: Executor,
    *,
    now: int,
) -> Optional[Selection]:
    if not binding.enabled or not executor.enabled:
        return None
    allowed_adapters = set(_json_list(shell.contract.get("allowed_adapters")))
    if executor.adapter_type not in allowed_adapters:
        return None
    if executor.adapter_type == "manual":
        # Manual/pull executors may be represented for audit, but the
        # autonomous dispatcher must never claim work for them.
        return None
    if binding.constraints.get("auto_spawn", True) is False:
        return None
    if executor.health_state in {"unhealthy", "degraded"}:
        return None
    if executor.heartbeat_required:
        if executor.last_heartbeat_at is None:
            return None
        if now - executor.last_heartbeat_at > executor.heartbeat_ttl_seconds:
            return None
    running = active_run_count(conn, executor.id)
    if running >= executor.capacity:
        return None
    allowed = set(shell.allowed_capabilities)
    provided = set(executor.capabilities)
    effective = allowed & provided
    if binding.capability_cap:
        effective &= set(binding.capability_cap)
    if not effective:
        return None
    if not set(shell.required_capabilities).issubset(effective):
        return None
    return Selection(
        shell=shell,
        executor=executor,
        binding=binding,
        effective_capabilities=sorted(effective),
        active_runs=running,
    )


def _matching_adapter_override(
    conn: sqlite3.Connection,
    *,
    shell: RoleShell,
    task_id: Optional[str],
) -> Optional[AdapterOverride]:
    now = _now()
    scopes: list[tuple[str, str]] = []
    if task_id:
        scopes.append(("task", str(task_id)))
    scopes.extend([("shell", shell.shell_key), ("all", "*")])
    for scope_type, scope_key in scopes:
        row = conn.execute(
            "SELECT * FROM adapter_overrides WHERE scope_type=? AND scope_key=? "
            "AND enabled=1 AND (expires_at IS NULL OR expires_at>?) "
            "AND (remaining_uses IS NULL OR remaining_uses>0) "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (scope_type, scope_key, now),
        ).fetchone()
        if row is not None:
            return AdapterOverride.from_row(row)
    return None


def select_binding(
    conn: sqlite3.Connection,
    role_shell_id: str,
    *,
    reserve: bool = False,
    require_active: bool = True,
    task_id: Optional[str] = None,
    additional_required_capabilities: Iterable[str] = (),
) -> Selection:
    """Select an eligible executor with deterministic, capacity-aware routing."""
    shell = get_shell(conn, shell_id=role_shell_id)
    if shell is None:
        raise NoEligibleExecutor(f"unknown role shell: {role_shell_id}")
    if require_active:
        head = get_shell(conn, shell_key=shell.shell_key)
        if head is None or head.id != shell.id:
            raise NoEligibleExecutor(
                f"role shell {role_shell_id} is not the active {shell.shell_key} version"
            )
    now = _now()
    additional_required = {
        str(item).strip()
        for item in additional_required_capabilities
        if str(item).strip()
    }
    override = _matching_adapter_override(conn, shell=shell, task_id=task_id)
    if override is not None:
        row = conn.execute(
            "SELECT * FROM role_bindings WHERE shell_id=? AND executor_id=?",
            (shell.id, override.executor_id),
        ).fetchone()
        if row is None:
            raise NoEligibleExecutor(
                f"forced adapter {override.executor_id} has no binding for {shell.shell_key} "
                f"(override {override.id}); fallback is disabled"
            )
        binding = Binding.from_row(row)
        executor = get_executor(conn, override.executor_id)
        selected = (
            _binding_eligible(conn, shell, binding, executor, now=now)
            if executor is not None
            else None
        )
        if selected is None:
            raise NoEligibleExecutor(
                f"forced adapter {override.executor_id} is currently ineligible for "
                f"{shell.shell_key} (override {override.id}); fallback is disabled"
            )
        missing_additional = sorted(
            additional_required - set(selected.effective_capabilities)
        )
        if missing_additional:
            raise NoEligibleExecutor(
                f"forced adapter {override.executor_id} lacks recovery capabilities: "
                + ", ".join(missing_additional)
            )
        selected = Selection(
            shell=selected.shell,
            executor=selected.executor,
            binding=selected.binding,
            effective_capabilities=selected.effective_capabilities,
            active_runs=selected.active_runs,
            adapter_override_id=override.id,
        )
        if reserve:
            conn.execute(
                "UPDATE role_bindings SET last_selected_at=?,updated_at=? WHERE id=?",
                (now, now, selected.binding.id),
            )
            # A task-scoped once override is one CARD, not one process
            # attempt.  Keep it active across crashes/timeouts and consume it
            # only when complete_task/archive_task closes the card.  Shell/all
            # once overrides remain one claim by design.
            consume_on_claim = (
                override.remaining_uses is not None
                and override.scope_type != "task"
            )
            if consume_on_claim:
                remaining = max(0, int(override.remaining_uses) - 1)
                conn.execute(
                    "UPDATE adapter_overrides SET remaining_uses=?,enabled=?,updated_at=? "
                    "WHERE id=?",
                    (remaining, int(remaining > 0), now, override.id),
                )
            _append_adapter_event_in_txn(
                conn,
                kind="adapter_override_selected",
                scope_type=override.scope_type,
                scope_key=override.scope_key,
                executor_id=selected.executor.id,
                binding_id=selected.binding.id,
                override_id=override.id,
                task_id=task_id,
                details={
                    "role_shell_id": shell.id,
                    "mode": override.mode,
                    "remaining_uses_after": (
                        max(0, int(override.remaining_uses) - 1)
                        if consume_on_claim
                        else override.remaining_uses
                    ),
                },
                created_by="dispatcher",
            )
        return selected
    candidates: list[Selection] = []
    for binding in list_bindings(conn, shell_id=role_shell_id):
        executor = get_executor(conn, binding.executor_id)
        if executor is None:
            continue
        selected = _binding_eligible(conn, shell, binding, executor, now=now)
        if selected and additional_required.issubset(
            set(selected.effective_capabilities)
        ):
            candidates.append(selected)
    if not candidates:
        raise NoEligibleExecutor(f"no eligible executor binding for {role_shell_id}")
    candidates.sort(
        key=lambda item: (
            0 if item.binding.responsibility == "primary" else 1,
            -item.binding.priority,
            item.active_runs / item.executor.capacity,
            -item.binding.weight,
            item.binding.last_selected_at or 0,
            item.binding.id,
        )
    )
    selected = candidates[0]
    if reserve:
        conn.execute(
            "UPDATE role_bindings SET last_selected_at=?,updated_at=? WHERE id=?",
            (now, now, selected.binding.id),
        )
    return selected


_RECEIPT_DELIVERY_TEXT_KEYS = (
    "body",
    "final_answer",
    "answer",
    "result",
    "content",
    "text",
    "conclusion",
    "summary",
)


def _format_receipt_delivery_field(label: str, value: Any) -> Optional[str]:
    """Render one structured receipt field without discarding its detail."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, bool):
        return f"{label}: {'true' if value else 'false'}"
    if isinstance(value, (int, float, str)):
        return f"{label}: {value}"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return f"{label}:\n" + "\n".join(f"- {item}" for item in value)
    return f"{label}:\n{json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)}"


def receipt_delivery_body(receipt: Optional[dict[str, Any]]) -> Optional[str]:
    """Recover a complete user-facing body from validated receipt outputs.

    Role workers sometimes put their prose conclusion in ``outputs[].body`` or
    ``outputs[].conclusion`` and leave the legacy ``result`` argument empty.
    Keep the primary prose first, then retain the remaining structured fields
    so Telegram, inspect, and the dashboard do not collapse real work into a
    one-line run summary.
    """
    if not isinstance(receipt, dict):
        return None
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list):
        return None
    rendered_outputs: list[str] = []
    for output in outputs:
        if isinstance(output, str) and output.strip():
            rendered_outputs.append(output.strip())
            continue
        if not isinstance(output, dict):
            continue
        primary_key = next(
            (
                key
                for key in _RECEIPT_DELIVERY_TEXT_KEYS
                if isinstance(output.get(key), str) and output[key].strip()
            ),
            None,
        )
        if primary_key is None:
            continue
        sections = [str(output[primary_key]).strip()]
        for key, value in output.items():
            if key == primary_key or key in {"type", "kind"}:
                continue
            field = _format_receipt_delivery_field(str(key), value)
            if field:
                sections.append(field)
        rendered_outputs.append("\n\n".join(sections))
    return "\n\n---\n\n".join(rendered_outputs) or None


def prepare_receipt(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    expected_run_id: Optional[int],
    receipt: Optional[dict[str, Any]],
    terminal_status: str,
) -> Optional[dict[str, Any]]:
    """Validate a worker receipt against trusted run provenance."""
    row = conn.execute(
        "SELECT id,task_id,role_shell_id,executor_id,binding_id,adapter_override_id "
        "FROM task_runs "
        "WHERE id=COALESCE(?,(SELECT current_run_id FROM tasks WHERE id=?))",
        (expected_run_id, task_id),
    ).fetchone()
    if row is None:
        task_row = conn.execute(
            "SELECT role_shell_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if task_row is not None and task_row["role_shell_id"]:
            raise ReceiptValidationError(
                "bound role task must be claimed by an executor before closure"
            )
        return None
    if not row["role_shell_id"]:
        return None
    if receipt is None or not isinstance(receipt, dict):
        raise ReceiptValidationError("bound role run requires a structured receipt")
    if terminal_status not in {"completed", "blocked", "failed"}:
        raise ReceiptValidationError(f"invalid terminal status: {terminal_status}")
    trusted = {
        "run_id": int(row["id"]),
        "task_id": row["task_id"],
        "role_shell_id": row["role_shell_id"],
        "executor_id": row["executor_id"],
        "binding_id": row["binding_id"],
        "adapter_override_id": row["adapter_override_id"],
    }
    for key, value in trusted.items():
        supplied = receipt.get(key)
        if supplied is not None and str(supplied) != str(value):
            raise ReceiptValidationError(f"receipt {key} does not match trusted run")
    shell = get_shell(conn, shell_id=row["role_shell_id"])
    if shell is None:
        raise ReceiptValidationError("receipt references a missing role shell")
    timeline_required = shell.evidence_policy.get("timeline_required", True) is not False
    timeline = receipt.get("timeline")
    if timeline_required:
        if not isinstance(timeline, dict):
            raise ReceiptValidationError("timeline evidence is required by the role shell")
        expected_goal = timeline_goal_id(row["task_id"], int(row["id"]))
        supplied_goal = str(timeline.get("goal_id") or "").strip()
        if supplied_goal and supplied_goal != expected_goal:
            raise ReceiptValidationError(
                "timeline.goal_id does not match the dispatcher-stamped run goal"
            )
        # The dispatcher owns this identity. Do not force the model to guess or
        # copy a trusted value into its own output; stamp it when omitted while
        # still rejecting an explicitly conflicting value.
        timeline = dict(timeline)
        timeline["goal_id"] = expected_goal
        if timeline.get("context_loaded") is not True:
            raise ReceiptValidationError("timeline.context_loaded must be true")
        if shell.evidence_policy.get("neural_recall_required", False) is True:
            neural = timeline.get("neural_recall")
            if not isinstance(neural, dict) or neural.get("performed") is not True:
                raise ReceiptValidationError(
                    "timeline.neural_recall.performed must be true"
                )
            if not str(neural.get("query") or "").strip():
                raise ReceiptValidationError("timeline.neural_recall.query is required")
            for field in ("candidate_count", "context_chars"):
                value = neural.get(field)
                if not isinstance(value, int) or value < 0:
                    raise ReceiptValidationError(
                        f"timeline.neural_recall.{field} must be a non-negative integer"
                    )
        if shell.evidence_policy.get("code_slice_required", True) is not False:
            if not _json_list(timeline.get("slice_ids")):
                raise ReceiptValidationError("timeline.slice_ids is required")
        if not _json_list(timeline.get("node_ids")):
            raise ReceiptValidationError("timeline.node_ids is required")
        verify = timeline.get("verify_all")
        if not isinstance(verify, dict) or verify.get("invalid_count") != 0:
            raise ReceiptValidationError("timeline verify_all invalid_count must be 0")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list):
        raise ReceiptValidationError("receipt.outputs must be a list")
    canonical = dict(receipt)
    if timeline_required:
        canonical["timeline"] = timeline
    canonical.update(trusted)
    canonical["terminal_status"] = terminal_status
    canonical["timeline_required"] = timeline_required
    canonical["validated_at"] = _now()
    return canonical


def store_receipt_in_txn(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    receipt: dict[str, Any],
) -> int:
    """Persist a prevalidated receipt inside the caller's task transition txn."""
    cur = conn.execute(
        "INSERT INTO run_receipts "
        "(run_id,task_id,role_shell_id,executor_id,binding_id,status,receipt_json,"
        "validation_error,created_at) VALUES(?,?,?,?,?,'valid',?,NULL,?)",
        (
            int(run_id), receipt["task_id"], receipt["role_shell_id"],
            receipt["executor_id"], receipt["binding_id"],
            _canonical_json(receipt), _now(),
        ),
    )
    receipt_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE task_runs SET receipt_id=? WHERE id=?",
        (receipt_id, int(run_id)),
    )
    return receipt_id


def build_worker_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    now = _now()
    rows: list[dict[str, Any]] = []
    for executor in list_executors(conn):
        age = (
            now - executor.last_heartbeat_at
            if executor.last_heartbeat_at is not None else None
        )
        stale = bool(
            executor.heartbeat_required
            and (age is None or age > executor.heartbeat_ttl_seconds)
        )
        running = active_run_count(conn, executor.id)
        rows.append(
            {
                "executor_id": executor.id,
                "name": executor.name,
                "adapter_type": executor.adapter_type,
                "enabled": executor.enabled,
                "health_state": executor.health_state,
                "heartbeat_age_seconds": age,
                "stale": stale,
                "active_runs": running,
                "capacity": executor.capacity,
                "binding_count": len(
                    [b for b in list_bindings(conn, executor_id=executor.id) if b.enabled]
                ),
                "healthy": bool(
                    executor.enabled
                    and not stale
                    and executor.health_state not in {"unhealthy", "degraded"}
                    and running <= executor.capacity
                ),
            }
        )
    return rows


def build_shell_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Report both fallback coverage and the policy-selected route.

    A permanent shell/global override is fail-closed in ``select_binding``.
    Health must therefore evaluate that exact binding instead of reporting a
    shell healthy merely because an unused fallback candidate is available.
    Busy capacity is intentionally not a health failure.
    """
    now = _now()
    rows: list[dict[str, Any]] = []
    for shell in list_shells(conn, active_only=True):
        bindings = [
            item for item in list_bindings(conn, shell_id=shell.id) if item.enabled
        ]
        routable: list[str] = []
        for binding in bindings:
            executor = get_executor(conn, binding.executor_id)
            if executor is None or not executor.enabled:
                continue
            if executor.adapter_type == "manual":
                continue
            if binding.constraints.get("auto_spawn", True) is False:
                continue
            if executor.adapter_type not in set(
                _json_list(shell.contract.get("allowed_adapters"))
            ):
                continue
            if executor.health_state in {"unhealthy", "degraded"}:
                continue
            if executor.heartbeat_required and (
                executor.last_heartbeat_at is None
                or now - executor.last_heartbeat_at > executor.heartbeat_ttl_seconds
            ):
                continue
            effective = set(shell.allowed_capabilities) & set(executor.capabilities)
            if binding.capability_cap:
                effective &= set(binding.capability_cap)
            if not effective or not set(shell.required_capabilities).issubset(effective):
                continue
            routable.append(binding.id)
        active_override = _matching_adapter_override(
            conn,
            shell=shell,
            task_id=None,
        )
        selected_binding_id = None
        selected_executor_id = None
        selection_source = "automatic"
        route_reason = "routable_candidate_available"
        if active_override is not None:
            selection_source = f"{active_override.scope_type}_override"
            selected_executor_id = active_override.executor_id
            selected_row = next(
                (
                    binding
                    for binding in bindings
                    if binding.executor_id == active_override.executor_id
                ),
                None,
            )
            selected_binding_id = selected_row.id if selected_row is not None else None
            selected_route_healthy = bool(selected_binding_id in routable)
            route_reason = (
                "selected_override_routable"
                if selected_route_healthy
                else "selected_override_ineligible_fallback_disabled"
            )
        else:
            selected_route_healthy = bool(routable)
            if not selected_route_healthy:
                route_reason = "no_routable_binding"
        rows.append(
            {
                "role_shell_id": shell.id,
                "shell_key": shell.shell_key,
                "version": shell.version,
                "binding_count": len(bindings),
                "routable_binding_count": len(routable),
                "routable_binding_ids": sorted(routable),
                "coverage_healthy": bool(routable),
                "selected_route_healthy": selected_route_healthy,
                "selected_binding_id": selected_binding_id,
                "selected_executor_id": selected_executor_id,
                "selection_source": selection_source,
                "route_reason": route_reason,
                "healthy": selected_route_healthy,
            }
        )
    return rows


def receipt_summary(conn: sqlite3.Connection) -> dict[str, int]:
    valid = int(conn.execute("SELECT COUNT(*) FROM run_receipts WHERE status='valid'").fetchone()[0])
    missing = int(
        conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE role_shell_id IS NOT NULL "
            "AND ended_at IS NOT NULL AND receipt_id IS NULL "
            "AND outcome IN ('completed','blocked')"
        ).fetchone()[0]
    )
    failed_without_receipt = int(
        conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE role_shell_id IS NOT NULL "
            "AND ended_at IS NOT NULL AND receipt_id IS NULL "
            "AND COALESCE(outcome,'') NOT IN ('completed','blocked')"
        ).fetchone()[0]
    )
    return {
        "valid": valid,
        "missing": missing,
        "invalid": 0,
        "failed_without_receipt": failed_without_receipt,
    }
