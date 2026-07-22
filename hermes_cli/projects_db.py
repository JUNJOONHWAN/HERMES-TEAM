"""Per-profile first-class Project store.

A **Project** is a human-named, multi-folder workspace. Unlike the desktop's
old inferred "workspaces" (derived from each session's ``cwd`` + a git probe)
and unlike kanban's self-generated worktrees, a Project is an explicit,
persisted entity the user creates and names. It anchors:

- **Desktop session grouping** — a session belongs to a project when its
  ``cwd`` lives under one of the project's folders (longest-prefix match).
- **Kanban task worktrees** — a task linked to a project creates its worktree
  under the project's primary repo with a deterministic branch name, instead
  of the random ``wt/<task-id>`` fallback.

Scope: **per-profile**, stored at ``$HERMES_HOME/projects.db`` (resolved via
``get_hermes_home()``), mirroring sessions / config / cron. This deliberately
differs from kanban, whose board DB is root-anchored and shared across
profiles. A Project may *bind* a kanban board (``board_slug``) so the two
systems agree on the repo + branch convention without merging their stores.

The schema is intentionally small and additive: column additions go through
:func:`_add_column_if_missing` so opening an old DB is always safe.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing, write_txn
from hermes_constants import get_hermes_home

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def projects_db_path() -> Path:
    """The per-profile projects DB path (``$HERMES_HOME/projects.db``).

    Profile-aware: ``get_hermes_home()`` already points at the active profile's
    home. Tests pass an explicit ``db_path`` to :func:`connect`.
    """
    return get_hermes_home() / "projects.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    description   TEXT,
    icon          TEXT,
    color         TEXT,
    board_slug    TEXT,
    primary_path  TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    completed_at  INTEGER,
    status        TEXT NOT NULL DEFAULT 'active',
    archived      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS project_folders (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    label       TEXT,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    added_at    INTEGER NOT NULL,
    PRIMARY KEY (project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_project_folders_path
    ON project_folders(path);

CREATE TABLE IF NOT EXISTS project_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Git repos found by scanning the filesystem (desktop "repo-first" discovery).
-- Cached here so the overview is instant after the first scan instead of
-- re-walking the disk every time the Projects view opens.
CREATE TABLE IF NOT EXISTS discovered_repos (
    root          TEXT PRIMARY KEY,
    label         TEXT,
    last_seen     INTEGER NOT NULL
);

-- Durable controller projection. Kanban remains the task/event store; this
-- row is the project-level status that web and messaging surfaces can read
-- without reconstructing policy from a pile of cards.
CREATE TABLE IF NOT EXISTS project_progress (
    project_id             TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    phase                  TEXT NOT NULL DEFAULT 'planning',
    milestone              TEXT,
    summary                TEXT,
    next_action            TEXT,
    last_card_id           TEXT,
    last_verified_card_id  TEXT,
    counts_json            TEXT NOT NULL DEFAULT '{}',
    approval_policy        TEXT NOT NULL DEFAULT 'code_card_requires_approval',
    updated_at             INTEGER NOT NULL
);

-- A governed card proposal is not a Kanban card until the operator approves it.
-- request_json is the complete immutable creation payload, so approval works
-- identically from Telegram and the web dashboard after a process restart.
CREATE TABLE IF NOT EXISTS project_card_approvals (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    action           TEXT NOT NULL,
    source_card_id   TEXT,
    title            TEXT NOT NULL,
    shell_key        TEXT NOT NULL,
    request_json     TEXT NOT NULL,
    dedupe_key       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    requested_by     TEXT,
    decision_reason  TEXT,
    created_at       INTEGER NOT NULL,
    decided_at       INTEGER,
    decided_by       TEXT,
    created_task_id  TEXT
);

CREATE INDEX IF NOT EXISTS idx_project_card_approvals_project_status
    ON project_card_approvals(project_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_card_approvals_open_dedupe
    ON project_card_approvals(project_id, dedupe_key)
    WHERE status IN ('pending', 'approved');

-- One governed source repository per Project. Card branches/worktrees remain
-- in Kanban, while repo provisioning and push state belong to the Project DB.
CREATE TABLE IF NOT EXISTS project_repositories (
    project_id          TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    mode                TEXT NOT NULL DEFAULT 'none',
    provider            TEXT,
    local_path          TEXT,
    remote_name         TEXT,
    remote_url          TEXT,
    visibility          TEXT,
    default_branch      TEXT NOT NULL DEFAULT 'main',
    integration_branch  TEXT,
    status              TEXT NOT NULL DEFAULT 'unconfigured',
    last_error          TEXT,
    current_branch      TEXT,
    base_sha            TEXT,
    head_sha            TEXT,
    last_card_id        TEXT,
    last_commit_sha     TEXT,
    last_pushed_sha     TEXT,
    updated_at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_git_events (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    card_id     TEXT,
    event_type  TEXT NOT NULL,
    branch      TEXT,
    sha         TEXT,
    remote      TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_git_events_project_created
    ON project_git_events(project_id, created_at, id);
"""


# ---------------------------------------------------------------------------
# Slug + id helpers
# ---------------------------------------------------------------------------

# Lowercase alphanumerics, hyphens, underscores; 1-64 chars; no leading
# separator. Strict enough to stop traversal and path separators, loose enough
# for kebab-case names like ``hermes-agent``. Display formatting (spaces,
# emoji, capitalisation) lives in ``name``; the slug is just a stable handle.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _slugify(name: str) -> str:
    """Derive a slug candidate from a human name (best-effort)."""
    s = str(name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-_")
    s = s[:64].strip("-_")
    return s or "project"


def normalize_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    if slug is None:
        return None
    s = str(slug).strip().lower()
    if not s:
        return None
    if not _SLUG_RE.match(s):
        raise ValueError(
            f"invalid project slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with "
            f"'-' or '_'"
        )
    return s


def _new_project_id() -> str:
    return "p_" + secrets.token_hex(4)


def _new_approval_id() -> str:
    return "pa_" + secrets.token_hex(4)


def _new_git_event_id() -> str:
    return "pge_" + secrets.token_hex(4)


def _now() -> int:
    return int(time.time())


def _normalize_path(path: str) -> str:
    """Absolute, user-expanded, separator-normalized path (no trailing sep)."""
    p = os.path.abspath(os.path.expanduser(str(path).strip()))
    return p.rstrip("/\\") or p


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_INITIALIZED_PATHS: set[str] = set()


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (and initialize if needed) the per-profile projects DB.

    WAL with DELETE fallback for network filesystems (shared helper from
    ``hermes_state``). Schema init is idempotent (``CREATE TABLE IF NOT
    EXISTS`` + additive migrations) and cached per-path per-process.
    """
    path = db_path if db_path is not None else projects_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="projects.db")
        conn.execute("PRAGMA foreign_keys=ON")
        if resolved not in _INITIALIZED_PATHS:
            conn.executescript(SCHEMA_SQL)
            _migrate_add_optional_columns(conn)
            _INITIALIZED_PATHS.add(resolved)
    except Exception:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def connect_closing(db_path: Optional[Path] = None):
    """Open a projects DB connection and guarantee it is closed on exit.

    sqlite3's connection context manager only commits/rollbacks; it does NOT
    close the file descriptor. Long-lived processes (gateway, dashboard) route
    many project operations through ``connect()``; without closing, FDs to
    ``projects.db`` accumulate. Mirrors ``kanban_db.connect_closing``.
    """
    conn = connect(db_path=db_path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# TEXT columns added to `projects` after v1; re-applied idempotently on every
# open so a legacy DB upgrades in place.
_OPTIONAL_PROJECT_COLUMNS = ("board_slug", "primary_path", "icon", "color")
VALID_PROJECT_STATUSES = frozenset({"active", "paused", "completed"})


def _migrate_add_optional_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after v1 to legacy DBs (safe on every open)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    for col in _OPTIONAL_PROJECT_COLUMNS:
        if col not in cols:
            _add_column_if_missing(conn, "projects", col, f"{col} TEXT")
    if "updated_at" not in cols:
        _add_column_if_missing(
            conn,
            "projects",
            "updated_at",
            "updated_at INTEGER NOT NULL DEFAULT 0",
        )
        conn.execute(
            "UPDATE projects SET updated_at = created_at WHERE updated_at = 0"
        )
    if "completed_at" not in cols:
        _add_column_if_missing(conn, "projects", "completed_at", "completed_at INTEGER")
    if "status" not in cols:
        _add_column_if_missing(
            conn,
            "projects",
            "status",
            "status TEXT NOT NULL DEFAULT 'active'",
        )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProjectFolder:
    path: str
    label: Optional[str] = None
    is_primary: bool = False
    added_at: int = 0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "label": self.label,
            "is_primary": bool(self.is_primary),
            "added_at": self.added_at,
        }


@dataclass
class Project:
    id: str
    slug: str
    name: str
    created_at: int
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    board_slug: Optional[str] = None
    primary_path: Optional[str] = None
    status: str = "active"
    updated_at: int = 0
    completed_at: Optional[int] = None
    archived: bool = False
    folders: List[ProjectFolder] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "board_slug": self.board_slug,
            "primary_path": self.primary_path,
            "status": self.status,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "archived": bool(self.archived),
            "created_at": self.created_at,
            "folders": [f.to_dict() for f in self.folders],
        }


def _project_from_row(row: sqlite3.Row) -> Project:
    keys = row.keys()
    return Project(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        created_at=row["created_at"],
        description=row["description"] if "description" in keys else None,
        icon=row["icon"] if "icon" in keys else None,
        color=row["color"] if "color" in keys else None,
        board_slug=row["board_slug"] if "board_slug" in keys else None,
        primary_path=row["primary_path"] if "primary_path" in keys else None,
        status=row["status"] if "status" in keys else "active",
        updated_at=(
            int(row["updated_at"])
            if "updated_at" in keys and row["updated_at"] is not None
            else int(row["created_at"])
        ),
        completed_at=(
            int(row["completed_at"])
            if "completed_at" in keys and row["completed_at"] is not None
            else None
        ),
        archived=bool(row["archived"]) if "archived" in keys else False,
    )


def _load_folders(conn: sqlite3.Connection, project_id: str) -> List[ProjectFolder]:
    rows = conn.execute(
        "SELECT path, label, is_primary, added_at FROM project_folders "
        "WHERE project_id = ? ORDER BY is_primary DESC, added_at ASC",
        (project_id,),
    ).fetchall()
    return [
        ProjectFolder(
            path=r["path"],
            label=r["label"],
            is_primary=bool(r["is_primary"]),
            added_at=r["added_at"],
        )
        for r in rows
    ]


def _attach_folders(conn: sqlite3.Connection, project: Project) -> Project:
    project.folders = _load_folders(conn, project.id)
    return project


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _unique_slug(conn: sqlite3.Connection, candidate: str) -> str:
    """Return ``candidate`` or ``candidate-2``, ``-3`` ... if taken."""
    base = candidate
    n = 1
    slug = base
    while conn.execute(
        "SELECT 1 FROM projects WHERE slug = ?", (slug,)
    ).fetchone() is not None:
        n += 1
        suffix = f"-{n}"
        slug = (base[: 64 - len(suffix)]).rstrip("-_") + suffix
    return slug


def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    slug: Optional[str] = None,
    folders: Optional[Iterable[str]] = None,
    primary_path: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    board_slug: Optional[str] = None,
) -> str:
    """Create a project and return its id.

    ``folders`` are normalized to absolute paths. If ``primary_path`` is given
    it is added to the folder set (if not already present) and marked primary;
    otherwise the first folder becomes primary.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("project name must not be empty")

    slug_candidate = normalize_slug(slug) if slug else _slugify(name)
    pid = _new_project_id()
    now = _now()

    folder_paths: List[str] = []
    for f in folders or []:
        norm = _normalize_path(f)
        if norm and norm not in folder_paths:
            folder_paths.append(norm)

    primary = _normalize_path(primary_path) if primary_path else None
    if primary and primary not in folder_paths:
        folder_paths.insert(0, primary)
    if primary is None and folder_paths:
        primary = folder_paths[0]

    with write_txn(conn):
        unique = _unique_slug(conn, slug_candidate)
        conn.execute(
            "INSERT INTO projects "
            "(id, slug, name, description, icon, color, board_slug, "
            " primary_path, created_at, updated_at, status, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0)",
            (
                pid,
                unique,
                name,
                description,
                icon,
                color,
                normalize_slug(board_slug) if board_slug else None,
                primary,
                now,
                now,
            ),
        )
        for path in folder_paths:
            conn.execute(
                "INSERT INTO project_folders "
                "(project_id, path, label, is_primary, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, path, None, 1 if path == primary else 0, now),
            )
    return pid


def list_projects(
    conn: sqlite3.Connection, *, include_archived: bool = False
) -> List[Project]:
    sql = "SELECT * FROM projects"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY created_at ASC"
    rows = conn.execute(sql).fetchall()
    return [_attach_folders(conn, _project_from_row(r)) for r in rows]


def get_project(
    conn: sqlite3.Connection, id_or_slug: str
) -> Optional[Project]:
    """Look up a project by id first, then by slug."""
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (id_or_slug,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = ?", (str(id_or_slug).lower(),)
        ).fetchone()
    if row is None:
        return None
    return _attach_folders(conn, _project_from_row(row))


def update_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    board_slug: Optional[str] = None,
) -> bool:
    """Patch top-level project fields. Only provided fields change.

    ``icon``, ``color``, and ``board_slug`` accept an empty string to clear
    (store NULL) — passing ``None`` leaves the field untouched, so callers that
    want to clear must send ``""``.
    """
    sets: List[str] = []
    params: List[object] = []
    if name is not None:
        n = str(name).strip()
        if not n:
            raise ValueError("project name must not be empty")
        sets.append("name = ?")
        params.append(n)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if icon is not None:
        sets.append("icon = ?")
        params.append(icon or None)
    if color is not None:
        sets.append("color = ?")
        params.append(color or None)
    if board_slug is not None:
        sets.append("board_slug = ?")
        params.append(normalize_slug(board_slug) if board_slug.strip() else None)
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(project_id)
    with write_txn(conn):
        cur = conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
        )
    return cur.rowcount > 0


def add_folder(
    conn: sqlite3.Connection,
    project_id: str,
    path: str,
    *,
    label: Optional[str] = None,
    is_primary: bool = False,
) -> str:
    """Add a folder to a project. Returns the normalized path.

    When ``is_primary`` is set, the folder becomes the project's primary repo
    (the previous primary is demoted, and ``projects.primary_path`` updates).
    """
    norm = _normalize_path(path)
    if not norm:
        raise ValueError("folder path must not be empty")
    if get_project(conn, project_id) is None:
        raise ValueError(f"no such project: {project_id}")
    now = _now()
    with write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO project_folders "
            "(project_id, path, label, is_primary, added_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (project_id, norm, label, now),
        )
        if label is not None:
            conn.execute(
                "UPDATE project_folders SET label = ? "
                "WHERE project_id = ? AND path = ?",
                (label, project_id, norm),
            )
        if is_primary:
            _set_primary_locked(conn, project_id, norm)
        else:
            # First folder of an empty project becomes primary implicitly.
            existing_primary = conn.execute(
                "SELECT 1 FROM project_folders "
                "WHERE project_id = ? AND is_primary = 1",
                (project_id,),
            ).fetchone()
            if existing_primary is None:
                _set_primary_locked(conn, project_id, norm)
    return norm


def remove_folder(conn: sqlite3.Connection, project_id: str, path: str) -> bool:
    """Remove a folder from a project. Repoints primary if it was primary."""
    norm = _normalize_path(path)
    with write_txn(conn):
        was_primary = conn.execute(
            "SELECT is_primary FROM project_folders "
            "WHERE project_id = ? AND path = ?",
            (project_id, norm),
        ).fetchone()
        cur = conn.execute(
            "DELETE FROM project_folders WHERE project_id = ? AND path = ?",
            (project_id, norm),
        )
        if was_primary is not None and was_primary["is_primary"]:
            nxt = conn.execute(
                "SELECT path FROM project_folders WHERE project_id = ? "
                "ORDER BY added_at ASC LIMIT 1",
                (project_id,),
            ).fetchone()
            new_primary = nxt["path"] if nxt else None
            if new_primary:
                _set_primary_locked(conn, project_id, new_primary)
            else:
                conn.execute(
                    "UPDATE projects SET primary_path = NULL WHERE id = ?",
                    (project_id,),
                )
    return cur.rowcount > 0


def _set_primary_locked(
    conn: sqlite3.Connection, project_id: str, path: str
) -> None:
    """Set the primary folder (caller already holds a write txn)."""
    conn.execute(
        "UPDATE project_folders SET is_primary = 0 WHERE project_id = ?",
        (project_id,),
    )
    conn.execute(
        "UPDATE project_folders SET is_primary = 1 "
        "WHERE project_id = ? AND path = ?",
        (project_id, path),
    )
    conn.execute(
        "UPDATE projects SET primary_path = ? WHERE id = ?",
        (path, project_id),
    )


def set_primary(conn: sqlite3.Connection, project_id: str, path: str) -> bool:
    norm = _normalize_path(path)
    with write_txn(conn):
        exists = conn.execute(
            "SELECT 1 FROM project_folders WHERE project_id = ? AND path = ?",
            (project_id, norm),
        ).fetchone()
        if exists is None:
            return False
        _set_primary_locked(conn, project_id, norm)
    return True


def archive_project(conn: sqlite3.Connection, project_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE projects SET archived = 1, updated_at = ? WHERE id = ?",
            (_now(), project_id),
        )
    return cur.rowcount > 0


def restore_project(conn: sqlite3.Connection, project_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE projects SET archived = 0, updated_at = ? WHERE id = ?",
            (_now(), project_id),
        )
    return cur.rowcount > 0


def delete_project(conn: sqlite3.Connection, project_id: str) -> bool:
    """Hard-delete a project and its folders (cascade)."""
    with write_txn(conn):
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return cur.rowcount > 0


def set_project_status(
    conn: sqlite3.Connection,
    project_id: str,
    status: str,
) -> bool:
    """Transition a project between ``active``, ``paused``, and ``completed``.

    Card state is validated by the Project/Card Controller before this store
    function is called.  Keeping that policy out of this low-level module lets
    CLI and desktop metadata code continue to use the Project store without
    importing Kanban.
    """
    normalized = str(status or "").strip().lower()
    if normalized not in VALID_PROJECT_STATUSES:
        raise ValueError(
            f"project status must be one of {sorted(VALID_PROJECT_STATUSES)}"
        )
    now = _now()
    completed_at = now if normalized == "completed" else None
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE projects SET status = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ?",
            (normalized, completed_at, now, project_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Project progress + operator approval gate
# ---------------------------------------------------------------------------


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


def get_project_progress(
    conn: sqlite3.Connection,
    project_id: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM project_progress WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["counts"] = json.loads(result.pop("counts_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        result["counts"] = {}
        result.pop("counts_json", None)
    return result


def upsert_project_progress(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    phase: str,
    counts: Optional[dict] = None,
    milestone: Optional[str] = None,
    summary: Optional[str] = None,
    next_action: Optional[str] = None,
    last_card_id: Optional[str] = None,
    last_verified_card_id: Optional[str] = None,
    approval_policy: str = "code_card_requires_approval",
) -> dict:
    if get_project(conn, project_id) is None:
        raise ValueError(f"no such project: {project_id}")
    normalized_phase = str(phase or "planning").strip().lower()
    now = _now()
    with write_txn(conn):
        conn.execute(
            "INSERT INTO project_progress "
            "(project_id, phase, milestone, summary, next_action, last_card_id, "
            " last_verified_card_id, counts_json, approval_policy, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET "
            "phase=excluded.phase, milestone=excluded.milestone, "
            "summary=excluded.summary, next_action=excluded.next_action, "
            "last_card_id=excluded.last_card_id, "
            "last_verified_card_id=excluded.last_verified_card_id, "
            "counts_json=excluded.counts_json, "
            "approval_policy=excluded.approval_policy, updated_at=excluded.updated_at",
            (
                project_id,
                normalized_phase,
                str(milestone).strip() if milestone else None,
                str(summary).strip() if summary else None,
                str(next_action).strip() if next_action else None,
                str(last_card_id).strip() if last_card_id else None,
                str(last_verified_card_id).strip() if last_verified_card_id else None,
                json.dumps(counts or {}, ensure_ascii=False, sort_keys=True),
                str(approval_policy or "code_card_requires_approval").strip(),
                now,
            ),
        )
    return get_project_progress(conn, project_id) or {}


def create_card_approval(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    action: str,
    title: str,
    shell_key: str,
    request: dict,
    dedupe_key: str,
    source_card_id: Optional[str] = None,
    requested_by: Optional[str] = None,
) -> dict:
    """Persist an idempotent operator approval request for a future card."""
    if get_project(conn, project_id) is None:
        raise ValueError(f"no such project: {project_id}")
    key = str(dedupe_key or "").strip()
    if not key:
        raise ValueError("approval dedupe_key is required")
    existing = conn.execute(
        "SELECT * FROM project_card_approvals WHERE project_id = ? "
        "AND dedupe_key = ? AND status IN ('pending', 'approved') "
        "ORDER BY created_at DESC LIMIT 1",
        (project_id, key),
    ).fetchone()
    if existing is not None:
        return card_approval_from_row(existing)
    approval_id = _new_approval_id()
    now = _now()
    with write_txn(conn):
        conn.execute(
            "INSERT INTO project_card_approvals "
            "(id, project_id, action, source_card_id, title, shell_key, "
            " request_json, dedupe_key, status, requested_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                approval_id,
                project_id,
                str(action or "add_project_card").strip(),
                str(source_card_id).strip() if source_card_id else None,
                str(title or "").strip(),
                str(shell_key or "code").strip(),
                json.dumps(request, ensure_ascii=False, sort_keys=True),
                key,
                str(requested_by).strip() if requested_by else None,
                now,
            ),
        )
    return get_card_approval(conn, approval_id) or {}


def card_approval_from_row(row: sqlite3.Row) -> dict:
    result = dict(row)
    try:
        result["request"] = json.loads(result.pop("request_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        result["request"] = {}
        result.pop("request_json", None)
    return result


def get_card_approval(
    conn: sqlite3.Connection,
    approval_id: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM project_card_approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()
    return card_approval_from_row(row) if row is not None else None


def list_card_approvals(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: Optional[Iterable[str]] = None,
) -> List[dict]:
    normalized = [str(item).strip().lower() for item in statuses or () if str(item).strip()]
    sql = "SELECT * FROM project_card_approvals WHERE project_id = ?"
    params: List[object] = [project_id]
    if normalized:
        sql += " AND status IN (" + ",".join("?" for _ in normalized) + ")"
        params.extend(normalized)
    sql += " ORDER BY created_at ASC, id ASC"
    return [card_approval_from_row(row) for row in conn.execute(sql, params).fetchall()]


def update_card_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    status: str,
    decided_by: Optional[str] = None,
    decision_reason: Optional[str] = None,
    created_task_id: Optional[str] = None,
) -> Optional[dict]:
    normalized = str(status or "").strip().lower()
    if normalized not in {"pending", "approved", "rejected", "consumed"}:
        raise ValueError("invalid project card approval status")
    now = _now()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE project_card_approvals SET status = ?, decided_at = ?, "
            "decided_by = COALESCE(?, decided_by), "
            "decision_reason = COALESCE(?, decision_reason), "
            "created_task_id = COALESCE(?, created_task_id) WHERE id = ?",
            (
                normalized,
                now if normalized != "pending" else None,
                str(decided_by).strip() if decided_by else None,
                str(decision_reason).strip() if decision_reason else None,
                str(created_task_id).strip() if created_task_id else None,
                approval_id,
            ),
        )
    return get_card_approval(conn, approval_id) if cur.rowcount else None


# ---------------------------------------------------------------------------
# Project repository + Git audit state
# ---------------------------------------------------------------------------


def get_project_repository(
    conn: sqlite3.Connection,
    project_id: str,
) -> Optional[dict]:
    return _row_dict(
        conn.execute(
            "SELECT * FROM project_repositories WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    )


def upsert_project_repository(
    conn: sqlite3.Connection,
    project_id: str,
    **fields,
) -> dict:
    if get_project(conn, project_id) is None:
        raise ValueError(f"no such project: {project_id}")
    allowed = {
        "mode", "provider", "local_path", "remote_name", "remote_url",
        "visibility", "default_branch", "integration_branch", "status",
        "last_error", "current_branch", "base_sha", "head_sha",
        "last_card_id", "last_commit_sha", "last_pushed_sha",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown repository fields: {sorted(unknown)}")
    existing = get_project_repository(conn, project_id) or {
        "mode": "none",
        "default_branch": "main",
        "status": "unconfigured",
    }
    merged = {key: fields.get(key, existing.get(key)) for key in allowed}
    now = _now()
    columns = ["project_id", *sorted(allowed), "updated_at"]
    values = [project_id, *(merged.get(key) for key in sorted(allowed)), now]
    updates = ", ".join(
        f"{key}=excluded.{key}" for key in [*sorted(allowed), "updated_at"]
    )
    with write_txn(conn):
        conn.execute(
            f"INSERT INTO project_repositories ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(project_id) DO UPDATE SET {updates}",
            values,
        )
    return get_project_repository(conn, project_id) or {}


def record_git_event(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    event_type: str,
    status: str,
    card_id: Optional[str] = None,
    branch: Optional[str] = None,
    sha: Optional[str] = None,
    remote: Optional[str] = None,
    detail: Optional[str] = None,
) -> dict:
    event_id = _new_git_event_id()
    now = _now()
    with write_txn(conn):
        conn.execute(
            "INSERT INTO project_git_events "
            "(id, project_id, card_id, event_type, branch, sha, remote, status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, project_id, card_id, str(event_type).strip(), branch,
                sha, remote, str(status).strip(), detail, now,
            ),
        )
    row = conn.execute(
        "SELECT * FROM project_git_events WHERE id = ?", (event_id,)
    ).fetchone()
    return dict(row)


def list_git_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM project_git_events WHERE project_id = ? "
        # ``created_at`` is second-resolution; multiple controller events often
        # land in the same second.  Random text ids are not chronological, so
        # use SQLite insertion order as the deterministic tie-breaker.
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (project_id, max(1, min(int(limit), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Active-project pointer (project_meta KV)
# ---------------------------------------------------------------------------


_ACTIVE_META_KEY = "active_id"


def set_active(conn: sqlite3.Connection, project_id: Optional[str]) -> None:
    """Set (or clear, when ``None``) the active project pointer."""
    with write_txn(conn):
        if project_id is None:
            conn.execute("DELETE FROM project_meta WHERE key = ?", (_ACTIVE_META_KEY,))
        else:
            conn.execute(
                "INSERT INTO project_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_ACTIVE_META_KEY, project_id),
            )


def get_active_id(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM project_meta WHERE key = ?", (_ACTIVE_META_KEY,)
    ).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# Discovered repos (filesystem scan cache)
# ---------------------------------------------------------------------------


def record_discovered_repos(
    conn: sqlite3.Connection,
    repos: Iterable[tuple[str, Optional[str]]],
    *,
    replace: bool = False,
) -> int:
    """Persist scanned git repo roots into the cache.

    ``repos`` is an iterable of ``(root, label)``. Roots are normalized; the
    label falls back to the basename. Returns the number of rows written.

    When ``replace`` is true, this is the authoritative result of a fresh disk
    scan: delete stale rows first so old eval/worktree noise disappears instead
    of living forever in the cache.
    """
    now = _now()
    rows = []
    for root, label in repos:
        norm = _normalize_path(root)
        if not norm:
            continue
        rows.append((norm, (label or os.path.basename(norm) or norm), now))

    with write_txn(conn):
        if replace:
            conn.execute("DELETE FROM discovered_repos")
        if rows:
            conn.executemany(
                "INSERT INTO discovered_repos (root, label, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(root) DO UPDATE SET label = excluded.label, "
                "last_seen = excluded.last_seen",
                rows,
            )
    return len(rows)


def list_discovered_repos(conn: sqlite3.Connection) -> List[dict]:
    """All cached discovered repo roots, most-recently-seen first."""
    rows = conn.execute(
        "SELECT root, label, last_seen FROM discovered_repos ORDER BY last_seen DESC"
    ).fetchall()
    return [
        {"root": r["root"], "label": r["label"], "last_seen": r["last_seen"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Resolution + naming
# ---------------------------------------------------------------------------


def project_for_path(
    conn: sqlite3.Connection, path: str, *, include_archived: bool = False
) -> Optional[Project]:
    """Return the project owning ``path`` (longest-prefix folder match).

    A folder owns ``path`` when ``path`` equals the folder or is nested under
    it. The most specific (longest) folder wins, so nested projects resolve to
    the innermost one.
    """
    if not str(path or "").strip():
        return None
    target = _normalize_path(path)
    sql = (
        "SELECT pf.project_id AS pid, pf.path AS folder "
        "FROM project_folders pf JOIN projects p ON p.id = pf.project_id"
    )
    if not include_archived:
        sql += " WHERE p.archived = 0"
    best_pid: Optional[str] = None
    best_len = -1
    for row in conn.execute(sql).fetchall():
        folder = row["folder"]
        if target == folder or target.startswith(folder.rstrip("/\\") + os.sep) or \
                target.startswith(folder.rstrip("/\\") + "/"):
            if len(folder) > best_len:
                best_len = len(folder)
                best_pid = row["pid"]
    if best_pid is None:
        return None
    return get_project(conn, best_pid)


# Deterministic branch slug: lowercase, separators collapsed, capped.
_BRANCH_SAFE_RE = re.compile(r"[^a-z0-9._-]+")


def branch_name_for(project: Project, task_id: str, *, title: str = "") -> str:
    """Deterministic branch name for a project-linked kanban task.

    Shape: ``<project-slug>/<task-id>`` (optionally ``-<title-slug>``). Stable
    and human-meaningful, replacing the random ``wt/<task-id>`` fallback.
    """
    slug = project.slug or _slugify(project.name)
    base = f"{slug}/{task_id}"
    if title:
        tslug = _BRANCH_SAFE_RE.sub("-", str(title).strip().lower()).strip("-")
        tslug = tslug[:40].strip("-")
        if tslug:
            base = f"{base}-{tslug}"
    return base
