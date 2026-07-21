from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import socket
import uuid
from pathlib import Path
from typing import Any

from .neural_links import (
    FEATURE_VERSION_MARKER,
    ensure_neural_schema,
    index_node as index_neural_node,
    recall_from_node_ids,
    recall_query,
)


DEFAULT_DB_PATH = str(Path.home() / ".hermes" / "timeline_code_map" / "graph.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT,
    body TEXT,
    file_path TEXT,
    line_start INTEGER,
    ts TEXT,
    confidence REAL DEFAULT 1.0,
    author TEXT,
    goal_id TEXT,
    prev_id TEXT,
    hash_chain TEXT NOT NULL,
    host_id TEXT,
    origin_db TEXT,
    logical_clock INTEGER,
    sync_batch_id TEXT,
    imported_at TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS edges (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    author TEXT,
    host_id TEXT,
    origin_db TEXT,
    logical_clock INTEGER,
    sync_batch_id TEXT,
    imported_at TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (from_id, to_id, relation)
);

CREATE TABLE IF NOT EXISTS goal_state (
    goal_id TEXT PRIMARY KEY,
    last_node_id TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS code_index_runs (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    file_count INTEGER DEFAULT 0,
    symbol_count INTEGER DEFAULT 0,
    edge_count INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    manifest_json TEXT
);

CREATE TABLE IF NOT EXISTS code_files (
    run_id TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    path TEXT NOT NULL,
    abs_path TEXT,
    language TEXT,
    suffix TEXT,
    size INTEGER DEFAULT 0,
    line_count INTEGER DEFAULT 0,
    sha256 TEXT,
    summary TEXT,
    PRIMARY KEY (run_id, path)
);

CREATE TABLE IF NOT EXISTS code_symbols (
    run_id TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,
    line_start INTEGER,
    line_end INTEGER,
    signature TEXT,
    parent TEXT,
    text TEXT,
    PRIMARY KEY (run_id, path, name, kind, line_start)
);

CREATE TABLE IF NOT EXISTS code_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    repo_root TEXT NOT NULL,
    from_path TEXT NOT NULL,
    from_symbol TEXT,
    to_path TEXT,
    to_symbol TEXT,
    relation TEXT NOT NULL,
    line_start INTEGER,
    evidence TEXT,
    weight REAL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS code_slices (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    run_id TEXT NOT NULL,
    query TEXT NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    body TEXT NOT NULL,
    host_id TEXT,
    origin_db TEXT,
    logical_clock INTEGER,
    sync_batch_id TEXT,
    imported_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_import_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    host_id TEXT,
    origin_db TEXT,
    sync_batch_id TEXT,
    source_created_at TEXT,
    imported_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    peer_id TEXT PRIMARY KEY,
    cursor TEXT,
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS sync_clock (
    host_id TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    id UNINDEXED,
    title,
    body,
    content='nodes',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
  INSERT INTO nodes_fts(rowid, id, title, body)
  VALUES (new.rowid, new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
  INSERT INTO nodes_fts(nodes_fts, rowid, id, title, body)
  VALUES('delete', old.rowid, old.id, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
  INSERT INTO nodes_fts(nodes_fts, rowid, id, title, body)
  VALUES('delete', old.rowid, old.id, old.title, old.body);
  INSERT INTO nodes_fts(rowid, id, title, body)
  VALUES (new.rowid, new.id, new.title, new.body);
END;

CREATE INDEX IF NOT EXISTS idx_nodes_domain_kind ON nodes(domain, kind);
CREATE INDEX IF NOT EXISTS idx_nodes_goal_created ON nodes(goal_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nodes_ts ON nodes(ts);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_code_runs_repo_active ON code_index_runs(repo_root, active, created_at);
CREATE INDEX IF NOT EXISTS idx_code_files_repo_path ON code_files(repo_root, path);
CREATE INDEX IF NOT EXISTS idx_code_symbols_repo_name ON code_symbols(repo_root, name);
CREATE INDEX IF NOT EXISTS idx_code_symbols_repo_path ON code_symbols(repo_root, path);
CREATE INDEX IF NOT EXISTS idx_code_edges_repo_from ON code_edges(repo_root, from_path);
CREATE INDEX IF NOT EXISTS idx_code_edges_repo_to ON code_edges(repo_root, to_path);
CREATE INDEX IF NOT EXISTS idx_code_slices_repo_query ON code_slices(repo_root, query, created_at);
CREATE INDEX IF NOT EXISTS idx_sync_import_events_batch ON sync_import_events(sync_batch_id, imported_at);
"""

AUDIT_RELATIONS = (
    "supports",
    "contradicts",
    "concludes_from",
    "produces",
    "derives_from",
    "supersedes",
    "calls",
    "imports",
    "reads",
    "writes",
    "part_of",
    "depends_on",
    "blocked_by",
    "scheduled_by",
    "assigned_to",
    "dispatched_to",
    "verified_by",
    "satisfies",
)


def _now_iso() -> str:
    return sqlite3.connect(":memory:").execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    ).fetchone()[0]


def _iso_days_ago(days: int) -> str:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(0, days))
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _serialize_body(body: Any) -> str | None:
    if body is None:
        return None
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _deserialize_body(body: str | None) -> Any:
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _compute_hash(node_id: str, content: str, prev_hash: str | None) -> str:
    payload = f"{node_id}|{content}|{prev_hash or 'genesis'}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _command_output(args: list[str], cwd: Path, *, timeout: float = 10.0) -> str:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _repo_state(repo_root: str) -> dict:
    root = Path(repo_root).expanduser().resolve()
    git_root = _command_output(["git", "rev-parse", "--show-toplevel"], root)
    if git_root:
        head = _command_output(["git", "rev-parse", "HEAD"], root)
        status = _command_output(["git", "status", "--short"], root, timeout=20.0)
        return {
            "kind": "git",
            "available": True,
            "git_root": git_root,
            "head": head,
            "dirty": bool(status),
            "dirty_paths": _git_status_paths(status),
            "status_hash": hashlib.sha256(status.encode("utf-8")).hexdigest(),
            "status_preview": status[:1000],
        }
    return {
        "kind": "filesystem",
        "available": False,
        "root": str(root),
        "reason": "not_a_git_repo",
    }


def _git_status_paths(status: str) -> list[str]:
    paths: set[str] = set()
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path_text = line[2:].strip()
        if " -> " in path_text:
            old_path, new_path = path_text.split(" -> ", 1)
            paths.add(old_path)
            paths.add(new_path)
        elif path_text:
            paths.add(path_text)
    return sorted(paths)


def _path_matches(path: str, candidates: set[str]) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    if normalized in candidates:
        return True
    return any(candidate == normalized or candidate.endswith(f"/{normalized}") for candidate in candidates)


def _repo_freshness(
    indexed_state: dict | None,
    current_state: dict | None,
    *,
    relevant_paths: list[str] | None = None,
) -> dict:
    indexed_state = indexed_state or {}
    current_state = current_state or {}
    if indexed_state.get("kind") != "git" or current_state.get("kind") != "git":
        return {
            "level": "unknown",
            "stale": False,
            "warnings": ["repo_freshness_unknown_non_git_or_legacy_index"],
            "indexed_state_available": bool(indexed_state.get("available")),
            "current_state_available": bool(current_state.get("available")),
        }

    changed: list[str] = []
    if indexed_state.get("head") != current_state.get("head"):
        changed.append("git_head_changed")
    if indexed_state.get("status_hash") != current_state.get("status_hash"):
        relevant_paths = relevant_paths or []
        indexed_dirty = set(indexed_state.get("dirty_paths") or [])
        current_dirty = set(current_state.get("dirty_paths") or [])
        dirty_delta = indexed_dirty.symmetric_difference(current_dirty)
        if relevant_paths and not any(_path_matches(path, dirty_delta) for path in relevant_paths):
            return {
                "level": "fresh",
                "stale": False,
                "changed": ["git_status_changed_outside_slice"],
                "warnings": ["repo_has_unrelated_dirty_changes_outside_slice"],
                "indexed_head": indexed_state.get("head"),
                "current_head": current_state.get("head"),
                "indexed_dirty": indexed_state.get("dirty"),
                "current_dirty": current_state.get("dirty"),
                "dirty_paths_checked": sorted(relevant_paths),
                "dirty_paths_changed": sorted(dirty_delta)[:50],
            }
        changed.append("git_status_changed_in_slice" if relevant_paths else "git_status_changed")

    if changed:
        return {
            "level": "stale",
            "stale": True,
            "changed": changed,
            "warnings": ["index_stale_repo_changed_since_run"],
            "indexed_head": indexed_state.get("head"),
            "current_head": current_state.get("head"),
            "indexed_dirty": indexed_state.get("dirty"),
            "current_dirty": current_state.get("dirty"),
        }

    return {
        "level": "fresh",
        "stale": False,
        "warnings": [],
        "indexed_head": indexed_state.get("head"),
        "current_head": current_state.get("head"),
        "dirty": current_state.get("dirty"),
    }


def _apply_freshness_to_quality(quality: dict, freshness: dict) -> dict:
    adjusted = dict(quality)
    warnings = list(adjusted.get("warnings", []))
    recommended = list(adjusted.get("recommended_next_steps", []))
    if freshness.get("stale"):
        for warning in freshness.get("warnings", []):
            if warning not in warnings:
                warnings.append(warning)
        if "rerun_index_repository_before_patching" not in recommended:
            recommended.insert(0, "rerun_index_repository_before_patching")
        adjusted["score"] = min(float(adjusted.get("score", 0.0)), 0.39)
        adjusted["level"] = "low"
        adjusted["gate"] = "do_not_patch_from_this_slice_alone"
    adjusted["warnings"] = warnings
    adjusted["recommended_next_steps"] = recommended
    return adjusted


def _host_id() -> str:
    return os.environ.get("TIMELINE_CODE_MAP_HOST_ID") or socket.gethostname() or "unknown-host"


def _origin_db(db_path: str) -> str:
    return str(Path(db_path).expanduser().resolve())


def _next_logical_clock(conn: sqlite3.Connection, host_id: str) -> int:
    row = conn.execute("SELECT value FROM sync_clock WHERE host_id=?", (host_id,)).fetchone()
    next_value = int(row["value"] if row else 0) + 1
    conn.execute(
        """
        INSERT INTO sync_clock (host_id, value)
        VALUES (?,?)
        ON CONFLICT(host_id) DO UPDATE SET value=excluded.value
        """,
        (host_id, next_value),
    )
    return next_value


def _sync_event_id(event_type: str, payload: dict) -> str:
    if event_type == "node_upsert":
        key = {"id": payload.get("id"), "hash_chain": payload.get("hash_chain")}
    elif event_type == "edge_upsert":
        key = {
            "from_id": payload.get("from_id"),
            "to_id": payload.get("to_id"),
            "relation": payload.get("relation"),
            "created_at": payload.get("created_at"),
        }
    elif event_type == "code_slice_upsert":
        key = {"id": payload.get("id"), "run_id": payload.get("run_id")}
    else:
        key = payload
    raw = json.dumps({"event_type": event_type, "key": key}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _jsonl_event(
    event_type: str,
    payload: dict,
    *,
    host_id: str,
    origin_db: str,
    sync_batch_id: str,
    source_sync_ts: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "event_id": _sync_event_id(event_type, payload),
        "event_type": event_type,
        "host_id": payload.get("host_id") or host_id,
        "origin_db": payload.get("origin_db") or origin_db,
        "sync_batch_id": sync_batch_id,
        "source_created_at": payload.get("created_at"),
        "source_sync_ts": source_sync_ts or payload.get("created_at"),
        "exported_at": _now_iso(),
        "payload": payload,
    }


def _ensure_parent_dir(db_path: str) -> None:
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


SYNC_METADATA_COLUMNS = {
    "host_id": "TEXT",
    "origin_db": "TEXT",
    "logical_clock": "INTEGER",
    "sync_batch_id": "TEXT",
    "imported_at": "TEXT",
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_sync_schema(conn: sqlite3.Connection) -> None:
    for table in ("nodes", "edges", "code_slices"):
        columns = _table_columns(conn, table)
        for name, sql_type in SYNC_METADATA_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    _ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _ensure_sync_schema(conn)
        ensure_neural_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _row_to_node(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "domain": row["domain"],
        "kind": row["kind"],
        "title": row["title"],
        "body": _deserialize_body(row["body"]),
        "file_path": row["file_path"],
        "line_start": row["line_start"],
        "ts": row["ts"],
        "confidence": row["confidence"],
        "author": row["author"],
        "goal_id": row["goal_id"],
        "prev_id": row["prev_id"],
        "hash_chain": row["hash_chain"],
        "host_id": row["host_id"],
        "origin_db": row["origin_db"],
        "logical_clock": row["logical_clock"],
        "sync_batch_id": row["sync_batch_id"],
        "imported_at": row["imported_at"],
        "created_at": row["created_at"],
    }


def _row_to_edge(row: sqlite3.Row) -> dict:
    return {
        "from_id": row["from_id"],
        "to_id": row["to_id"],
        "relation": row["relation"],
        "weight": row["weight"],
        "author": row["author"],
        "host_id": row["host_id"],
        "origin_db": row["origin_db"],
        "logical_clock": row["logical_clock"],
        "sync_batch_id": row["sync_batch_id"],
        "imported_at": row["imported_at"],
        "created_at": row["created_at"],
    }


def _code_index_health(index: dict) -> dict:
    files = index.get("files", [])
    symbols = index.get("symbols", [])
    edges = index.get("edges", [])
    file_count = max(1, len(files))
    edge_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    for edge in edges:
        path = edge.get("from_path") or ""
        if path:
            edge_counts[path] = edge_counts.get(path, 0) + 1
    for symbol in symbols:
        path = symbol.get("path") or ""
        if path:
            symbol_counts[path] = symbol_counts.get(path, 0) + 1

    top_edge_fanout = sorted(edge_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    top_symbol_density = sorted(symbol_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    edges_per_file = round(len(edges) / file_count, 3)
    symbols_per_file = round(len(symbols) / file_count, 3)
    fanout_entries = [
        {"path": path, "edges": count, "classification": _hub_classification(path, count)}
        for path, count in top_edge_fanout
    ]
    warnings: list[str] = []
    if edges_per_file > 400:
        warnings.append("high_edge_density_check_for_generated_or_overmatched_edges")
    if any(item["classification"] == "suspicious_fanout" for item in fanout_entries):
        warnings.append("single_file_edge_fanout_over_5000")
    if symbols_per_file > 200:
        warnings.append("high_symbol_density_check_for_generated_or_minified_files")

    return {
        "edges_per_file": edges_per_file,
        "symbols_per_file": symbols_per_file,
        "top_edge_fanout": fanout_entries,
        "top_symbol_density": [{"path": path, "symbols": count} for path, count in top_symbol_density],
        "warnings": warnings,
    }


def _hub_classification(path: str, edge_count: int) -> str:
    normalized = path.replace("\\", "/").lower()
    name = Path(normalized).name
    expected_names = {
        "__init__.py",
        "config.py",
        "configs.py",
        "constants.py",
        "settings.py",
        "schema.py",
        "schemas.py",
        "types.py",
        "routes.py",
        "index.ts",
        "index.tsx",
        "index.js",
        "index.jsx",
    }
    if name in expected_names or any(part in normalized for part in ("/config/", "/configs/", "/constants/")):
        return "expected_hub"
    if edge_count > 5000:
        return "suspicious_fanout"
    return "normal"


def _slice_quality(
    *,
    query_tokens: list[str],
    relevant_files: list[dict],
    relevant_symbols: list[dict],
    relationship_flow: list[dict],
    ranked_files: list[dict],
) -> dict:
    top_score = float(ranked_files[0]["score"]) if ranked_files else 0.0
    second_score = float(ranked_files[1]["score"]) if len(ranked_files) > 1 else 0.0
    combined_text = " ".join(
        [
            " ".join(str(item.get(key, "")) for key in ("path", "reason"))
            for item in relevant_files
        ]
        + [
            " ".join(str(item.get(key, "")) for key in ("path", "name", "signature", "reason"))
            for item in relevant_symbols
        ]
        + [
            " ".join(str(item.get(key, "")) for key in ("from", "to", "relation", "evidence"))
            for item in relationship_flow
        ]
    ).lower()
    matched_tokens = [token for token in query_tokens if token.lower() in combined_text]
    token_coverage = len(matched_tokens) / max(1, len(query_tokens))
    separation = 1.0 if second_score <= 0 else max(0.0, min(1.0, (top_score - second_score) / max(top_score, 1.0)))

    score = 0.0
    score += 0.15 if relevant_files else 0.0
    score += 0.20 if relevant_symbols else 0.0
    score += 0.15 if relationship_flow else 0.0
    score += 0.25 * token_coverage
    score += 0.15 if top_score > 0 else 0.0
    score += 0.10 * separation
    score = round(min(1.0, score), 3)

    warnings: list[str] = []
    recommended: list[str] = []
    if not relevant_files:
        warnings.append("no_relevant_files")
    if not relevant_symbols:
        warnings.append("no_relevant_symbols")
    if not relationship_flow:
        warnings.append("no_relationship_flow")
    if token_coverage < 0.34 and query_tokens:
        warnings.append("low_query_token_coverage")
    if top_score > 0 and second_score > 0 and separation < 0.12:
        warnings.append("ambiguous_top_files")
    if token_coverage < 0.34 and query_tokens:
        score = min(score, 0.39)

    if score >= 0.70 and not warnings:
        level = "high"
        gate = "ok_to_use_as_preflight_evidence"
        recommended.append("read_exact_source_before_editing")
    elif score >= 0.40:
        level = "medium"
        gate = "use_with_source_reads_and_broader_scan"
        recommended.extend(
            [
                "read_exact_source_before_editing",
                "check_readme_agents_and_neighbor_files",
                "rerun_with_more_specific_query_if_target_is_unclear",
            ]
        )
    else:
        level = "low"
        gate = "do_not_patch_from_this_slice_alone"
        recommended.extend(
            [
                "broaden_query_terms",
                "read_AGENTS_README_and_recently_changed_files_first",
                "fallback_to_legacy_code_map_or_full_repo_scan_if_available",
            ]
        )

    return {
        "level": level,
        "score": score,
        "gate": gate,
        "token_coverage": round(token_coverage, 3),
        "matched_tokens": matched_tokens,
        "top_score": round(top_score, 3),
        "second_score": round(second_score, 3),
        "top_separation": round(separation, 3),
        "warnings": warnings,
        "recommended_next_steps": recommended,
    }


def _infer_output_kind(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    if suffix in {".md", ".txt", ".rst"}:
        return "report"
    if suffix in {".py", ".js", ".ts", ".sh"}:
        return "script"
    if suffix in {".csv", ".json", ".jsonl", ".parquet"}:
        return "dataset"
    return "config"


def _parse_sqlite_ts(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)


def _slice_eviction_score(row: sqlite3.Row, now: dt.datetime) -> float:
    created_at = _parse_sqlite_ts(row["created_at"])
    age_days = max(0.0, (now - created_at).total_seconds() / 86_400)
    body = _deserialize_body(row["body"]) or {}
    confidence = body.get("slice_confidence", {}) if isinstance(body, dict) else {}
    try:
        score = float(confidence.get("score", 0.5))
    except (TypeError, ValueError):
        score = 0.5
    level = str(confidence.get("level", "medium"))
    penalty = 1.0 + max(0.0, min(1.0, 1.0 - score))
    if level == "low":
        penalty += 2.0
    elif level == "medium":
        penalty += 0.5
    return age_days * penalty


class TimelineCodeMap:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = str(Path(db_path).expanduser())

    def record(
        self,
        *,
        domain: str,
        kind: str,
        title: str | None = None,
        body: Any = None,
        file_path: str | None = None,
        line_start: int | None = None,
        author: str = "unknown",
        confidence: float = 1.0,
        goal_id: str | None = None,
        prev_id: str | None = None,
    ) -> str:
        body_str = _serialize_body(body)
        node_id = str(uuid.uuid4())
        ts = _now_iso()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if prev_id is None and goal_id:
                row = conn.execute(
                    "SELECT last_node_id FROM goal_state WHERE goal_id=?",
                    (goal_id,),
                ).fetchone()
                prev_id = row["last_node_id"] if row else None

            prev_hash = None
            if prev_id:
                row = conn.execute(
                    "SELECT hash_chain FROM nodes WHERE id=?",
                    (prev_id,),
                ).fetchone()
                prev_hash = row["hash_chain"] if row else None

            content_for_hash = "|".join(
                [
                    domain or "",
                    kind or "",
                    title or "",
                    body_str or "",
                    file_path or "",
                    str(line_start or ""),
                    author or "",
                    prev_hash or "genesis",
                ]
            )
            hash_chain = _compute_hash(node_id, content_for_hash, prev_hash)
            host_id = _host_id()
            origin_db = _origin_db(self.db_path)
            logical_clock = _next_logical_clock(conn, host_id)

            conn.execute(
                """
                INSERT INTO nodes (
                    id, domain, kind, title, body, file_path, line_start, ts,
                    confidence, author, goal_id, prev_id, hash_chain,
                    host_id, origin_db, logical_clock
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    node_id,
                    domain,
                    kind,
                    title,
                    body_str,
                    file_path,
                    line_start,
                    ts,
                    confidence,
                    author,
                    goal_id,
                    prev_id,
                    hash_chain,
                    host_id,
                    origin_db,
                    logical_clock,
                ),
            )

            if prev_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO edges (
                        from_id, to_id, relation, weight, author,
                        host_id, origin_db, logical_clock
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (prev_id, node_id, "next", 1.0, author, host_id, origin_db, _next_logical_clock(conn, host_id)),
                )

            if goal_id:
                conn.execute(
                    """
                    INSERT INTO goal_state (goal_id, last_node_id, updated_at)
                    VALUES (?,?,?)
                    ON CONFLICT(goal_id) DO UPDATE SET
                        last_node_id=excluded.last_node_id,
                        updated_at=excluded.updated_at
                    """,
                    (goal_id, node_id, ts),
                )

            index_neural_node(
                conn,
                node_id,
                host_id=host_id,
                origin_db=origin_db,
                next_logical_clock=lambda: _next_logical_clock(conn, host_id),
            )

            conn.commit()
        return node_id

    def link(
        self,
        from_id: str,
        to_id: str,
        relation: str,
        *,
        weight: float = 1.0,
        author: str | None = None,
    ) -> None:
        with _connect(self.db_path) as conn:
            host_id = _host_id()
            origin_db = _origin_db(self.db_path)
            logical_clock = _next_logical_clock(conn, host_id)
            conn.execute(
                """
                INSERT OR REPLACE INTO edges (
                    from_id, to_id, relation, weight, author,
                    host_id, origin_db, logical_clock
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (from_id, to_id, relation, weight, author, host_id, origin_db, logical_clock),
            )
            conn.commit()

    def get_node(self, node_id: str) -> dict | None:
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def search(
        self,
        *,
        domain: str | None = None,
        kind: str | None = None,
        text: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        with _connect(self.db_path) as conn:
            params: list[Any] = []
            if text:
                query = """
                    SELECT n.*
                    FROM nodes n
                    JOIN nodes_fts f ON n.rowid = f.rowid
                    WHERE nodes_fts MATCH ?
                """
                params.append(text)
            else:
                query = "SELECT * FROM nodes n WHERE 1=1"

            if domain:
                query += " AND n.domain=?"
                params.append(domain)
            if kind:
                query += " AND n.kind=?"
                params.append(kind)
            if since:
                query += " AND COALESCE(n.ts, n.created_at) >= ?"
                params.append(since)
            if until:
                query += " AND COALESCE(n.ts, n.created_at) <= ?"
                params.append(until)

            query += " ORDER BY COALESCE(n.ts, n.created_at) DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_row_to_node(row) for row in rows]

    def verify_chain(self, node_id: str) -> dict:
        with _connect(self.db_path) as conn:
            current_id = node_id
            depth = 0
            while current_id:
                row = conn.execute(
                    """
                    SELECT id, domain, kind, title, body, file_path, line_start, author,
                           prev_id, hash_chain
                    FROM nodes
                    WHERE id=?
                    """,
                    (current_id,),
                ).fetchone()
                if row is None:
                    return {"valid": False, "reason": f"node {current_id} not found", "depth": depth}

                prev_hash = None
                if row["prev_id"]:
                    prev_row = conn.execute(
                        "SELECT hash_chain FROM nodes WHERE id=?",
                        (row["prev_id"],),
                    ).fetchone()
                    if prev_row is None:
                        return {
                            "valid": False,
                            "reason": f"prev node {row['prev_id']} missing",
                            "depth": depth,
                        }
                    prev_hash = prev_row["hash_chain"]

                content_for_hash = "|".join(
                    [
                        row["domain"] or "",
                        row["kind"] or "",
                        row["title"] or "",
                        row["body"] or "",
                        row["file_path"] or "",
                        str(row["line_start"] or ""),
                        row["author"] or "",
                        prev_hash or "genesis",
                    ]
                )
                recalculated = _compute_hash(row["id"], content_for_hash, prev_hash)
                if recalculated != row["hash_chain"]:
                    return {
                        "valid": False,
                        "reason": f"hash mismatch at node {row['id']}",
                        "depth": depth,
                    }

                current_id = row["prev_id"]
                depth += 1
        return {"valid": True, "depth": depth}

    def verify_all(self) -> dict:
        with _connect(self.db_path) as conn:
            node_ids = [row["id"] for row in conn.execute("SELECT id FROM nodes").fetchall()]

        invalid = []
        for node_id in node_ids:
            result = self.verify_chain(node_id)
            if not result["valid"]:
                invalid.append({"node_id": node_id, **result})
        return {"total": len(node_ids), "invalid_count": len(invalid), "invalid": invalid}

    def _collect_goal_chain(self, conn: sqlite3.Connection, goal_id: str, limit: int | None = None) -> list[dict]:
        row = conn.execute(
            "SELECT last_node_id FROM goal_state WHERE goal_id=?",
            (goal_id,),
        ).fetchone()
        if row is None or not row["last_node_id"]:
            return []

        nodes: list[dict] = []
        current_id = row["last_node_id"]
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            node_row = conn.execute("SELECT * FROM nodes WHERE id=?", (current_id,)).fetchone()
            if node_row is None:
                break
            nodes.append(_row_to_node(node_row))
            current_id = node_row["prev_id"]
            if limit is not None and len(nodes) >= limit:
                break
        return nodes

    def _expand_related(
        self,
        conn: sqlite3.Connection,
        seed_ids: list[str],
        *,
        relations: tuple[str, ...] | None = None,
        domain: str | None = None,
        depth: int = 1,
        direction: str = "both",
    ) -> list[dict]:
        if not seed_ids:
            return []

        visited = set(seed_ids)
        frontier = set(seed_ids)
        for _ in range(depth):
            if not frontier:
                break

            placeholders = ",".join("?" * len(frontier))
            clauses = []
            params: list[Any] = []
            if direction in {"outgoing", "both"}:
                clauses.append(f"from_id IN ({placeholders})")
                params.extend(frontier)
            if direction in {"incoming", "both"}:
                clauses.append(f"to_id IN ({placeholders})")
                params.extend(frontier)

            query = f"SELECT * FROM edges WHERE ({' OR '.join(clauses)})"
            if relations:
                relation_placeholders = ",".join("?" * len(relations))
                query += f" AND relation IN ({relation_placeholders})"
                params.extend(relations)

            rows = conn.execute(query, tuple(params)).fetchall()
            next_frontier: set[str] = set()
            for row in rows:
                if row["from_id"] in frontier and row["to_id"] not in visited:
                    visited.add(row["to_id"])
                    next_frontier.add(row["to_id"])
                if row["to_id"] in frontier and row["from_id"] not in visited:
                    visited.add(row["from_id"])
                    next_frontier.add(row["from_id"])
            frontier = next_frontier

        related_ids = list(visited - set(seed_ids))
        if not related_ids:
            return []

        placeholders = ",".join("?" * len(related_ids))
        query = f"SELECT * FROM nodes WHERE id IN ({placeholders})"
        params = list(related_ids)
        if domain:
            query += " AND domain=?"
            params.append(domain)
        rows = conn.execute(query, tuple(params)).fetchall()
        return [_row_to_node(row) for row in rows]

    def get_context(self, goal_id: str, *, depth: int = 2, recent_limit: int = 10) -> dict:
        with _connect(self.db_path) as conn:
            recent_nodes = self._collect_goal_chain(conn, goal_id, limit=recent_limit)
            recent_ids = [node["id"] for node in recent_nodes]
            prior_judgments = self._expand_related(
                conn,
                recent_ids,
                relations=("concludes_from", "supports", "contradicts", "derives_from"),
                domain="reasoning",
                depth=depth,
                direction="incoming",
            )
            code_context = self._expand_related(
                conn,
                recent_ids,
                relations=("calls", "imports", "reads", "writes"),
                domain="code",
                depth=depth,
                direction="both",
            )
            associative_memory = recall_from_node_ids(
                conn,
                recent_ids,
                limit=8,
                max_depth=max(1, min(5, depth)),
            )
        return {
            "goal_id": goal_id,
            "recent_actions": recent_nodes,
            "prior_judgments": prior_judgments,
            "code_context": code_context,
            "associative_memory": associative_memory,
        }

    def recall_neural_context(
        self,
        query: str,
        *,
        limit: int = 6,
        max_chars: int = 1800,
        max_depth: int | None = None,
        candidate_mode: bool = False,
        include_expired: bool | None = None,
    ) -> dict:
        with _connect(self.db_path) as conn:
            return recall_query(
                conn,
                query,
                limit=limit,
                max_chars=max_chars,
                max_depth=max_depth,
                candidate_mode=candidate_mode,
                include_expired=include_expired,
            )

    def neural_link_status(self) -> dict:
        with _connect(self.db_path) as conn:
            counts = {
                "nodes": conn.execute("SELECT count(*) FROM nodes").fetchone()[0],
                "indexed_nodes": conn.execute("SELECT count(*) FROM neural_node_features").fetchone()[0],
                "feature_terms": conn.execute("SELECT count(*) FROM neural_feature_terms").fetchone()[0],
                "neural_edges": conn.execute(
                    "SELECT count(*) FROM edges WHERE relation IN ('same_workflow','same_entity','same_concept','associates')"
                ).fetchone()[0],
                "current_feature_nodes": conn.execute(
                    "SELECT count(DISTINCT node_id) FROM neural_feature_terms WHERE term_type='meta' AND term=?",
                    (FEATURE_VERSION_MARKER,),
                ).fetchone()[0],
            }
            freshness_rows = conn.execute(
                """
                SELECT freshness_class, count(*) AS total,
                       sum(CASE WHEN expires_at IS NOT NULL AND julianday(expires_at) < julianday('now')
                                THEN 1 ELSE 0 END) AS expired
                FROM neural_node_features
                GROUP BY freshness_class
                ORDER BY freshness_class
                """
            ).fetchall()
            counts["freshness"] = {
                row["freshness_class"]: {"total": row["total"], "expired": row["expired"]}
                for row in freshness_rows
            }
            counts["pending_nodes"] = counts["nodes"] - counts["current_feature_nodes"]
            return counts

    def backfill_neural_links(self, *, limit: int = 0, batch_size: int = 100) -> dict:
        total_indexed = 0
        total_refreshed = 0
        total_links = 0
        remaining_limit = max(0, limit)
        batch_size = max(1, min(1000, batch_size))
        while True:
            take = batch_size if not remaining_limit else min(batch_size, remaining_limit)
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT n.id
                    FROM nodes n
                    LEFT JOIN neural_node_features f ON f.node_id=n.id
                    WHERE f.node_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM neural_feature_terms t
                           WHERE t.node_id=n.id AND t.term_type='meta' AND t.term=?
                       )
                    ORDER BY COALESCE(n.created_at, n.ts) ASC
                    LIMIT ?
                    """,
                    (FEATURE_VERSION_MARKER, take),
                ).fetchall()
                if not rows:
                    break
                conn.execute("BEGIN IMMEDIATE")
                host_id = _host_id()
                origin_db = _origin_db(self.db_path)
                for row in rows:
                    result = index_neural_node(
                        conn,
                        row["id"],
                        host_id=host_id,
                        origin_db=origin_db,
                        next_logical_clock=lambda: _next_logical_clock(conn, host_id),
                    )
                    total_indexed += int(result["indexed"])
                    total_refreshed += int(result.get("refreshed", False))
                    total_links += result["links_created"]
                conn.commit()
            if remaining_limit:
                remaining_limit -= len(rows)
                if remaining_limit <= 0:
                    break
            if len(rows) < take:
                break
        status = self.neural_link_status()
        return {
            "status": "ok",
            "indexed": total_indexed,
            "refreshed": total_refreshed,
            "links_created": total_links,
            **status,
        }

    def load_session(self, *, since: str | None = None, goal_id: str | None = None) -> dict:
        with _connect(self.db_path) as conn:
            query = "SELECT * FROM nodes WHERE 1=1"
            params: list[Any] = []
            if since:
                query += " AND created_at >= ?"
                params.append(since)
            if goal_id:
                query += " AND goal_id = ?"
                params.append(goal_id)
            query += " ORDER BY created_at ASC"
            nodes = [_row_to_node(row) for row in conn.execute(query, tuple(params)).fetchall()]

            node_ids = [node["id"] for node in nodes]
            if not node_ids:
                return {"nodes": [], "edges": [], "node_count": 0, "edge_count": 0}

            placeholders = ",".join("?" * len(node_ids))
            edge_query = f"""
                SELECT * FROM edges
                WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})
            """
            edges = [_row_to_edge(row) for row in conn.execute(edge_query, tuple(node_ids + node_ids)).fetchall()]
        return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}

    def trace_audit(self, reasoning_node_id: str) -> dict:
        with _connect(self.db_path) as conn:
            root = conn.execute("SELECT * FROM nodes WHERE id=?", (reasoning_node_id,)).fetchone()
            if root is None:
                return {"error": "node not found"}

            visited = set()
            frontier = {reasoning_node_id}
            edge_map: dict[tuple[str, str, str], dict] = {}
            while frontier:
                placeholders = ",".join("?" * len(frontier))
                relation_placeholders = ",".join("?" * len(AUDIT_RELATIONS))
                query = f"""
                    SELECT * FROM edges
                    WHERE (from_id IN ({placeholders}) OR to_id IN ({placeholders}))
                      AND relation IN ({relation_placeholders})
                """
                rows = conn.execute(
                    query,
                    tuple(list(frontier) + list(frontier) + list(AUDIT_RELATIONS)),
                ).fetchall()

                next_frontier: set[str] = set()
                for row in rows:
                    edge = _row_to_edge(row)
                    edge_map[(edge["from_id"], edge["to_id"], edge["relation"])] = edge
                    for node_id in (edge["from_id"], edge["to_id"]):
                        if node_id not in visited and node_id not in frontier:
                            next_frontier.add(node_id)
                visited.update(frontier)
                frontier = next_frontier - visited

            node_ids = list(visited)
            placeholders = ",".join("?" * len(node_ids))
            nodes = [
                _row_to_node(row)
                for row in conn.execute(
                    f"SELECT * FROM nodes WHERE id IN ({placeholders})",
                    tuple(node_ids),
                ).fetchall()
            ]
        return {
            "root": _row_to_node(root),
            "evidence_chain": nodes,
            "edges": list(edge_map.values()),
            "node_count": len(nodes),
            "edge_count": len(edge_map),
        }

    def auto_ingest_output(
        self,
        file_path: str,
        source_action_id: str,
        *,
        reasoning_id: str | None = None,
        author: str = "hermes",
    ) -> str:
        node_id = self.record(
            domain="output",
            kind=_infer_output_kind(file_path),
            title=Path(file_path).name,
            body={"file_path": file_path},
            file_path=file_path,
            author=author,
        )
        self.link(source_action_id, node_id, "produces", author=author)
        if reasoning_id:
            self.link(node_id, reasoning_id, "derives_from", author=author)
        return node_id

    def snapshot(self, goal_id: str) -> dict:
        with _connect(self.db_path) as conn:
            seed_nodes = self._collect_goal_chain(conn, goal_id, limit=None)
            visited = {node["id"] for node in seed_nodes}
            frontier = set(visited)
            edge_map: dict[tuple[str, str, str], dict] = {}

            while frontier:
                placeholders = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"SELECT * FROM edges WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
                    tuple(list(frontier) + list(frontier)),
                ).fetchall()
                next_frontier: set[str] = set()
                for row in rows:
                    edge = _row_to_edge(row)
                    edge_map[(edge["from_id"], edge["to_id"], edge["relation"])] = edge
                    for node_id in (edge["from_id"], edge["to_id"]):
                        if node_id not in visited:
                            visited.add(node_id)
                            next_frontier.add(node_id)
                frontier = next_frontier

            if not visited:
                return {"goal_id": goal_id, "snapshot_ts": _now_iso(), "nodes": [], "edges": []}

            placeholders = ",".join("?" * len(visited))
            nodes = [
                _row_to_node(row)
                for row in conn.execute(
                    f"SELECT * FROM nodes WHERE id IN ({placeholders})",
                    tuple(visited),
                ).fetchall()
            ]
        return {
            "goal_id": goal_id,
            "snapshot_ts": _now_iso(),
            "nodes": nodes,
            "edges": list(edge_map.values()),
        }

    def export_graph(self, *, goal_id: str | None = None) -> dict:
        if goal_id:
            return self.snapshot(goal_id)
        return self.load_session()

    def index_repository(
        self,
        repo_root: str,
        *,
        include_artifacts: bool = False,
        max_file_bytes: int = 512_000,
        max_files: int = 20_000,
        author: str = "hermes",
        record_summary: bool = False,
        goal_id: str | None = None,
    ) -> dict:
        from .code_index import build_code_index

        index = build_code_index(
            repo_root,
            include_artifacts=include_artifacts,
            max_file_bytes=max_file_bytes,
            max_files=max_files,
        )
        run_id = str(uuid.uuid4())
        repo_root_resolved = index["repo_root"]
        created_at = _now_iso()
        health = _code_index_health(index)
        repo_state = _repo_state(repo_root_resolved)
        manifest = {
            "repo_root": repo_root_resolved,
            "repo_name": index["repo_name"],
            "counts": index["counts"],
            "limits": index["limits"],
            "health": health,
            "repo_state": repo_state,
            "author": author,
        }

        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE code_index_runs SET active=0 WHERE repo_root=?",
                (repo_root_resolved,),
            )
            conn.execute("DELETE FROM code_files WHERE repo_root=?", (repo_root_resolved,))
            conn.execute("DELETE FROM code_symbols WHERE repo_root=?", (repo_root_resolved,))
            conn.execute("DELETE FROM code_edges WHERE repo_root=?", (repo_root_resolved,))
            conn.execute(
                """
                INSERT INTO code_index_runs (
                    id, repo_root, repo_name, created_at, file_count,
                    symbol_count, edge_count, active, manifest_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    repo_root_resolved,
                    index["repo_name"],
                    created_at,
                    index["counts"]["files"],
                    index["counts"]["symbols"],
                    index["counts"]["edges"],
                    1,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.executemany(
                """
                INSERT INTO code_files (
                    run_id, repo_root, path, abs_path, language, suffix, size,
                    line_count, sha256, summary
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        repo_root_resolved,
                        item["path"],
                        item["abs_path"],
                        item["language"],
                        item["suffix"],
                        item["size"],
                        item["line_count"],
                        item["sha256"],
                        item["summary"],
                    )
                    for item in index["files"]
                ],
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO code_symbols (
                    run_id, repo_root, path, name, kind, line_start, line_end,
                    signature, parent, text
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        repo_root_resolved,
                        item["path"],
                        item["name"],
                        item["kind"],
                        item["line_start"],
                        item["line_end"],
                        item["signature"],
                        item["parent"],
                        item["text"],
                    )
                    for item in index["symbols"]
                ],
            )
            conn.executemany(
                """
                INSERT INTO code_edges (
                    run_id, repo_root, from_path, from_symbol, to_path,
                    to_symbol, relation, line_start, evidence, weight
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        repo_root_resolved,
                        item["from_path"],
                        item["from_symbol"],
                        item["to_path"],
                        item["to_symbol"],
                        item["relation"],
                        item["line_start"],
                        item["evidence"],
                        item["weight"],
                    )
                    for item in index["edges"]
                ],
            )
            conn.commit()

        summary_node_id = None
        if record_summary:
            summary_node_id = self.record(
                domain="code",
                kind="index_run",
                title=f"{index['repo_name']} code index",
                body=manifest,
                file_path=repo_root_resolved,
                author=author,
                goal_id=goal_id,
            )

        return {
            "run_id": run_id,
            "repo_root": repo_root_resolved,
            "repo_name": index["repo_name"],
            "counts": index["counts"],
            "limits": index["limits"],
            "health": health,
            "repo_state": repo_state,
            "summary_node_id": summary_node_id,
        }

    def list_code_indexes(self) -> list[dict]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, repo_root, repo_name, created_at, file_count,
                       symbol_count, edge_count, active, manifest_json
                FROM code_index_runs
                WHERE active=1
                ORDER BY repo_name
                """
            ).fetchall()
        return [
            {
                "run_id": row["id"],
                "repo_root": row["repo_root"],
                "repo_name": row["repo_name"],
                "created_at": row["created_at"],
                "file_count": row["file_count"],
                "symbol_count": row["symbol_count"],
                "edge_count": row["edge_count"],
                "active": bool(row["active"]),
                "manifest": _deserialize_body(row["manifest_json"]),
            }
            for row in rows
        ]

    def query_code_slice(
        self,
        repo_root: str,
        query: str,
        *,
        limit: int = 12,
        store_slice: bool = True,
        goal_id: str | None = None,
        author: str = "hermes",
        rebuild_if_missing: bool = False,
    ) -> dict:
        from .code_index import score_text, summarize_reasons, tokenize

        repo_root_resolved = str(Path(repo_root).expanduser().resolve())
        with _connect(self.db_path) as conn:
            run = conn.execute(
                """
                SELECT * FROM code_index_runs
                WHERE repo_root=? AND active=1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (repo_root_resolved,),
            ).fetchone()

        if run is None and rebuild_if_missing:
            self.index_repository(repo_root_resolved, author=author)
            with _connect(self.db_path) as conn:
                run = conn.execute(
                    """
                    SELECT * FROM code_index_runs
                    WHERE repo_root=? AND active=1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (repo_root_resolved,),
                ).fetchone()

        if run is None:
            return {
                "error": "code index not found",
                "repo_root": repo_root_resolved,
                "hint": "call index_code_repository_tool first or pass rebuild_if_missing=true",
            }

        manifest = _deserialize_body(run["manifest_json"]) or {}
        current_repo_state = _repo_state(repo_root_resolved)
        query_tokens = tokenize(query)
        with _connect(self.db_path) as conn:
            file_rows = conn.execute(
                "SELECT * FROM code_files WHERE repo_root=? AND run_id=?",
                (repo_root_resolved, run["id"]),
            ).fetchall()
            symbol_rows = conn.execute(
                "SELECT * FROM code_symbols WHERE repo_root=? AND run_id=?",
                (repo_root_resolved, run["id"]),
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT * FROM code_edges WHERE repo_root=? AND run_id=?",
                (repo_root_resolved, run["id"]),
            ).fetchall()

        file_scores: dict[str, dict[str, Any]] = {}
        for row in file_rows:
            text = " ".join(
                [
                    row["path"] or "",
                    row["language"] or "",
                    row["summary"] or "",
                ]
            )
            score, reasons = score_text(query_tokens, text, weight=1.0)
            path_boost, path_reasons = score_text(query_tokens, row["path"] or "", weight=3.0)
            score += path_boost
            reasons.extend(path_reasons)
            file_scores[row["path"]] = {
                "path": row["path"],
                "abs_path": row["abs_path"],
                "language": row["language"],
                "line_count": row["line_count"],
                "size": row["size"],
                "score": score,
                "reasons": reasons,
            }

        symbol_hits: list[dict[str, Any]] = []
        for row in symbol_rows:
            text = " ".join(
                [
                    row["path"] or "",
                    row["name"] or "",
                    row["kind"] or "",
                    row["signature"] or "",
                    row["text"] or "",
                ]
            )
            score, reasons = score_text(query_tokens, text, weight=2.0)
            name_score, name_reasons = score_text(query_tokens, row["name"] or "", weight=4.0)
            score += name_score
            reasons.extend(name_reasons)
            if score > 0 or (row["path"] in file_scores and file_scores[row["path"]]["score"] > 0):
                file_scores.setdefault(row["path"], {"score": 0.0, "reasons": []})
                file_scores[row["path"]]["score"] += score * 0.35
                file_scores[row["path"]]["reasons"].extend(reasons)
                symbol_hits.append(
                    {
                        "path": row["path"],
                        "name": row["name"],
                        "kind": row["kind"],
                        "line_start": row["line_start"],
                        "line_end": row["line_end"],
                        "signature": row["signature"],
                        "parent": row["parent"],
                        "score": round(score, 3),
                        "reason": summarize_reasons(reasons),
                    }
                )

        edge_hits: list[dict[str, Any]] = []
        for row in edge_rows:
            text = " ".join(
                [
                    row["from_path"] or "",
                    row["from_symbol"] or "",
                    row["to_path"] or "",
                    row["to_symbol"] or "",
                    row["relation"] or "",
                    row["evidence"] or "",
                ]
            )
            score, reasons = score_text(query_tokens, text, weight=1.5)
            if score > 0:
                if row["from_path"] in file_scores:
                    file_scores[row["from_path"]]["score"] += score * 0.2
                    file_scores[row["from_path"]]["reasons"].extend(reasons)
                edge_hits.append(
                    {
                        "from": f"{row['from_path']}:{row['from_symbol'] or ''}".rstrip(":"),
                        "to": row["to_path"] or row["to_symbol"],
                        "relation": row["relation"],
                        "line_start": row["line_start"],
                        "evidence": row["evidence"],
                        "score": round(score, 3),
                    }
                )

        ranked_files = sorted(
            [value for value in file_scores.values() if value.get("score", 0) > 0],
            key=lambda item: (-item["score"], item["path"]),
        )
        if not ranked_files and file_rows:
            ranked_files = [
                {
                    "path": row["path"],
                    "abs_path": row["abs_path"],
                    "language": row["language"],
                    "line_count": row["line_count"],
                    "size": row["size"],
                    "score": 0.0,
                    "reasons": ["fallback-first-files"],
                }
                for row in file_rows[:limit]
            ]

        ranked_files = ranked_files[:limit]
        selected_paths = {item["path"] for item in ranked_files}
        relevant_symbols = sorted(
            [item for item in symbol_hits if item["path"] in selected_paths],
            key=lambda item: (-item["score"], item["path"], item["line_start"] or 0),
        )[: limit * 4]

        relationship_flow = [
            item
            for item in sorted(edge_hits, key=lambda edge: (-edge["score"], edge["from"]))
            if str(item["from"]).split(":", 1)[0] in selected_paths
        ][: limit * 6]

        relevant_files = [
            {
                "path": item["path"],
                "abs_path": item["abs_path"],
                "language": item["language"],
                "line_count": item["line_count"],
                "size": item["size"],
                "score": round(item["score"], 3),
                "reason": summarize_reasons(item.get("reasons", [])),
            }
            for item in ranked_files
        ]
        affected_files = [
            {
                "path": item["path"],
                "score": item["score"],
                "reasons": item["reason"],
            }
            for item in relevant_files
        ]
        watchpoints = [
            {
                "path": item["path"],
                "reason": f"Read exact source before editing; matched {item['reason']}",
            }
            for item in relevant_files[: min(8, len(relevant_files))]
        ]
        patch_checkpoints = [
            "Read target files and surrounding callers before patching.",
            "Use query_code_slice_tool again after edits if imports, entrypoints, or MCP tool names changed.",
            "Run focused tests plus verify_all_tool before closing substantive work.",
        ]
        quality = _slice_quality(
            query_tokens=query_tokens,
            relevant_files=relevant_files,
            relevant_symbols=relevant_symbols,
            relationship_flow=relationship_flow,
            ranked_files=ranked_files,
        )
        freshness = _repo_freshness(
            manifest.get("repo_state"),
            current_repo_state,
            relevant_paths=[item["path"] for item in relevant_files],
        )
        quality = _apply_freshness_to_quality(quality, freshness)
        slice_id = str(uuid.uuid4())
        slice_body = {
            "slice_id": slice_id,
            "repo_root": repo_root_resolved,
            "repo_name": run["repo_name"],
            "run_id": run["id"],
            "query": query,
            "generated_at": _now_iso(),
            "limit": limit,
            "counts": {
                "indexed_files": run["file_count"],
                "indexed_symbols": run["symbol_count"],
                "indexed_edges": run["edge_count"],
                "relevant_files": len(relevant_files),
                "relevant_symbols": len(relevant_symbols),
                "relationship_flow": len(relationship_flow),
            },
            "relevant_files": relevant_files,
            "relevant_symbols": relevant_symbols,
            "relationship_flow": relationship_flow,
            "affected_files": affected_files,
            "watchpoints": watchpoints,
            "patch_checkpoints": patch_checkpoints,
            "freshness": freshness,
            "slice_confidence": quality,
            "warnings": quality["warnings"],
            "recommended_next_steps": quality["recommended_next_steps"],
        }

        if goal_id:
            node_id = self.record(
                domain="code",
                kind="slice",
                title=f"{run['repo_name']} slice: {query[:80]}",
                body={
                    "slice_id": slice_id,
                    "repo_root": repo_root_resolved,
                    "run_id": run["id"],
                    "query": query,
                    "counts": slice_body["counts"],
                    "top_files": [item["path"] for item in relevant_files[:5]],
                    "freshness": freshness,
                    "slice_confidence": quality,
                },
                file_path=repo_root_resolved,
                author=author,
                goal_id=goal_id,
            )
            slice_body["slice_node_id"] = node_id

        if store_slice:
            with _connect(self.db_path) as conn:
                host_id = _host_id()
                origin_db = _origin_db(self.db_path)
                logical_clock = _next_logical_clock(conn, host_id)
                conn.execute(
                    """
                    INSERT INTO code_slices (
                        id, repo_root, run_id, query, body,
                        host_id, origin_db, logical_clock
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        slice_id,
                        repo_root_resolved,
                        run["id"],
                        query,
                        json.dumps(slice_body, ensure_ascii=False, sort_keys=True),
                        host_id,
                        origin_db,
                        logical_clock,
                    ),
                )
                old_rows = conn.execute(
                    """
                    SELECT id FROM code_slices
                    WHERE repo_root=?
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET 50
                    """,
                    (repo_root_resolved,),
                ).fetchall()
                if old_rows:
                    conn.executemany("DELETE FROM code_slices WHERE id=?", [(row["id"],) for row in old_rows])
                conn.commit()

        return slice_body

    def load_code_slice(self, slice_id: str) -> dict | None:
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT body FROM code_slices WHERE id=?", (slice_id,)).fetchone()
        if row is None:
            return None
        return _deserialize_body(row["body"])

    def export_delta(
        self,
        output_path: str | None = None,
        *,
        since: str = "",
        host_id: str | None = None,
        sync_batch_id: str | None = None,
    ) -> dict:
        host_id = host_id or _host_id()
        origin_db = _origin_db(self.db_path)
        sync_batch_id = sync_batch_id or str(uuid.uuid4())
        events: list[dict] = []
        next_cursor = since or ""

        with _connect(self.db_path) as conn:
            specs = [
                (
                    "node_upsert",
                    """
                    SELECT *, COALESCE(imported_at, created_at) AS sync_ts
                    FROM nodes
                    WHERE COALESCE(imported_at, created_at) > ?
                    ORDER BY sync_ts ASC, id ASC
                    """,
                    (
                        "id",
                        "domain",
                        "kind",
                        "title",
                        "body",
                        "file_path",
                        "line_start",
                        "ts",
                        "confidence",
                        "author",
                        "goal_id",
                        "prev_id",
                        "hash_chain",
                        "created_at",
                        "host_id",
                        "origin_db",
                        "logical_clock",
                        "sync_batch_id",
                        "imported_at",
                    ),
                ),
                (
                    "edge_upsert",
                    """
                    SELECT *, COALESCE(imported_at, created_at) AS sync_ts
                    FROM edges
                    WHERE COALESCE(imported_at, created_at) > ?
                    ORDER BY sync_ts ASC, from_id ASC, to_id ASC, relation ASC
                    """,
                    (
                        "from_id",
                        "to_id",
                        "relation",
                        "weight",
                        "author",
                        "created_at",
                        "host_id",
                        "origin_db",
                        "logical_clock",
                        "sync_batch_id",
                        "imported_at",
                    ),
                ),
                (
                    "code_slice_upsert",
                    """
                    SELECT *, COALESCE(imported_at, created_at) AS sync_ts
                    FROM code_slices
                    WHERE COALESCE(imported_at, created_at) > ?
                    ORDER BY sync_ts ASC, id ASC
                    """,
                    (
                        "id",
                        "repo_root",
                        "run_id",
                        "query",
                        "created_at",
                        "body",
                        "host_id",
                        "origin_db",
                        "logical_clock",
                        "sync_batch_id",
                        "imported_at",
                    ),
                ),
            ]
            for event_type, query, columns in specs:
                for row in conn.execute(query, (since,)).fetchall():
                    payload = {column: row[column] for column in columns}
                    event = _jsonl_event(
                        event_type,
                        payload,
                        host_id=host_id,
                        origin_db=origin_db,
                        sync_batch_id=sync_batch_id,
                        source_sync_ts=row["sync_ts"],
                    )
                    events.append(event)
                    if row["sync_ts"] and row["sync_ts"] > next_cursor:
                        next_cursor = row["sync_ts"]

        if output_path:
            path = Path(output_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

        return {
            "status": "ok",
            "path": str(Path(output_path).expanduser()) if output_path else None,
            "sync_batch_id": sync_batch_id,
            "host_id": host_id,
            "origin_db": origin_db,
            "since": since,
            "next_cursor": next_cursor,
            "event_count": len(events),
            "events": events if output_path is None else [],
        }

    def import_delta(
        self,
        input_path: str,
        *,
        peer_id: str = "",
        merge_policy: str = "append_only",
    ) -> dict:
        if merge_policy != "append_only":
            return {"status": "error", "error": "only append_only merge_policy is supported"}

        path = Path(input_path).expanduser()
        imported = 0
        skipped = 0
        fork_edges = 0
        errors: list[dict] = []
        latest_cursor = ""
        batch_ids: set[str] = set()
        local_host_id = _host_id()
        local_origin_db = _origin_db(self.db_path)
        parsed_events: list[dict] = []

        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                    event_id = event["event_id"]
                    event_type = event["event_type"]
                    payload = event["payload"]
                except (KeyError, json.JSONDecodeError, TypeError) as exc:
                    errors.append({"line": line_number, "error": f"invalid_event: {exc}"})
                    continue

                if event.get("schema_version") != 1:
                    errors.append({"line": line_number, "event_id": event.get("event_id"), "error": "unsupported_schema_version"})
                    continue

                source_cursor = event.get("source_sync_ts") or event.get("source_created_at") or ""
                if source_cursor > latest_cursor:
                    latest_cursor = source_cursor

                parsed_events.append(
                    {
                        "line_number": line_number,
                        "event": event,
                        "event_id": event_id,
                        "event_type": event_type,
                        "payload": payload,
                    }
                )

        event_order = {"node_upsert": 0, "edge_upsert": 1, "code_slice_upsert": 2}
        parsed_events.sort(
            key=lambda item: (
                event_order.get(item["event_type"], 99),
                item["payload"].get("created_at") or item["event"].get("source_created_at") or "",
                item["event_id"],
            )
        )

        with _connect(self.db_path) as conn:
            for item in parsed_events:
                event = item["event"]
                event_id = item["event_id"]
                event_type = item["event_type"]
                payload = item["payload"]
                if conn.execute("SELECT 1 FROM sync_import_events WHERE event_id=?", (event_id,)).fetchone():
                    skipped += 1
                    continue

                imported_at = _now_iso()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    rowcount = 0
                    if event_type == "node_upsert":
                        rowcount, added_fork = self._import_node_event(
                            conn,
                            payload,
                            imported_at=imported_at,
                            local_host_id=local_host_id,
                            local_origin_db=local_origin_db,
                            sync_batch_id=event.get("sync_batch_id") or "",
                        )
                        fork_edges += added_fork
                    elif event_type == "edge_upsert":
                        rowcount = self._import_edge_event(conn, payload, imported_at=imported_at)
                    elif event_type == "code_slice_upsert":
                        rowcount = self._import_code_slice_event(conn, payload, imported_at=imported_at)
                    else:
                        raise ValueError(f"unsupported_event_type: {event_type}")

                    conn.execute(
                        """
                        INSERT INTO sync_import_events (
                            event_id, event_type, host_id, origin_db, sync_batch_id,
                            source_created_at, imported_at, payload_json
                        ) VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            event_id,
                            event_type,
                            event.get("host_id"),
                            event.get("origin_db"),
                            event.get("sync_batch_id"),
                            event.get("source_created_at"),
                            imported_at,
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    conn.commit()
                    if rowcount:
                        imported += 1
                    else:
                        skipped += 1
                    if event.get("sync_batch_id"):
                        batch_ids.add(event["sync_batch_id"])
                except Exception as exc:  # pragma: no cover - defensive import isolation
                    conn.rollback()
                    errors.append({"line": item["line_number"], "event_id": event_id, "error": str(exc)})

            if peer_id and latest_cursor:
                conn.execute(
                    """
                    INSERT INTO sync_cursors (peer_id, cursor, updated_at)
                    VALUES (?,?,?)
                    ON CONFLICT(peer_id) DO UPDATE SET
                        cursor=excluded.cursor,
                        updated_at=excluded.updated_at
                    """,
                    (peer_id, latest_cursor, _now_iso()),
                )
                conn.commit()

        return {
            "status": "ok" if not errors else "partial",
            "path": str(path),
            "merge_policy": merge_policy,
            "imported": imported,
            "skipped": skipped,
            "fork_edges": fork_edges,
            "batch_ids": sorted(batch_ids),
            "cursor": latest_cursor,
            "errors": errors[:20],
        }

    def _import_node_event(
        self,
        conn: sqlite3.Connection,
        payload: dict,
        *,
        imported_at: str,
        local_host_id: str,
        local_origin_db: str,
        sync_batch_id: str,
    ) -> tuple[int, int]:
        row = conn.execute("SELECT 1 FROM nodes WHERE id=?", (payload.get("id"),)).fetchone()
        if row:
            return 0, 0
        conn.execute(
            """
            INSERT OR IGNORE INTO nodes (
                id, domain, kind, title, body, file_path, line_start, ts,
                confidence, author, goal_id, prev_id, hash_chain, created_at,
                host_id, origin_db, logical_clock, sync_batch_id, imported_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.get("id"),
                payload.get("domain"),
                payload.get("kind"),
                payload.get("title"),
                payload.get("body"),
                payload.get("file_path"),
                payload.get("line_start"),
                payload.get("ts"),
                payload.get("confidence", 1.0),
                payload.get("author"),
                payload.get("goal_id"),
                payload.get("prev_id"),
                payload.get("hash_chain"),
                payload.get("created_at"),
                payload.get("host_id"),
                payload.get("origin_db"),
                payload.get("logical_clock"),
                payload.get("sync_batch_id"),
                imported_at,
            ),
        )
        fork_edges = self._preserve_goal_fork(
            conn,
            payload,
            imported_at=imported_at,
            local_host_id=local_host_id,
            local_origin_db=local_origin_db,
            sync_batch_id=sync_batch_id,
        )
        index_neural_node(
            conn,
            str(payload.get("id")),
            host_id=local_host_id,
            origin_db=local_origin_db,
            next_logical_clock=lambda: _next_logical_clock(conn, local_host_id),
        )
        return 1, fork_edges

    def _preserve_goal_fork(
        self,
        conn: sqlite3.Connection,
        payload: dict,
        *,
        imported_at: str,
        local_host_id: str,
        local_origin_db: str,
        sync_batch_id: str,
    ) -> int:
        goal_id = payload.get("goal_id")
        node_id = payload.get("id")
        prev_id = payload.get("prev_id")
        if not goal_id or not node_id:
            return 0
        current = conn.execute("SELECT last_node_id FROM goal_state WHERE goal_id=?", (goal_id,)).fetchone()
        if current is None:
            conn.execute(
                "INSERT INTO goal_state (goal_id, last_node_id, updated_at) VALUES (?,?,?)",
                (goal_id, node_id, payload.get("created_at") or imported_at),
            )
            return 0
        current_last = current["last_node_id"]
        if current_last in {node_id, prev_id}:
            conn.execute(
                """
                UPDATE goal_state
                SET last_node_id=?, updated_at=?
                WHERE goal_id=?
                """,
                (node_id, payload.get("created_at") or imported_at, goal_id),
            )
            return 0
        conn.execute(
            """
            INSERT OR IGNORE INTO edges (
                from_id, to_id, relation, weight, author, created_at,
                host_id, origin_db, logical_clock, sync_batch_id, imported_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                current_last,
                node_id,
                "fork",
                1.0,
                "sync",
                imported_at,
                local_host_id,
                local_origin_db,
                _next_logical_clock(conn, local_host_id),
                sync_batch_id,
                imported_at,
            ),
        )
        return 1

    def _import_edge_event(self, conn: sqlite3.Connection, payload: dict, *, imported_at: str) -> int:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO edges (
                from_id, to_id, relation, weight, author, created_at,
                host_id, origin_db, logical_clock, sync_batch_id, imported_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.get("from_id"),
                payload.get("to_id"),
                payload.get("relation"),
                payload.get("weight", 1.0),
                payload.get("author"),
                payload.get("created_at"),
                payload.get("host_id"),
                payload.get("origin_db"),
                payload.get("logical_clock"),
                payload.get("sync_batch_id"),
                imported_at,
            ),
        )
        return 1 if conn.total_changes > before else 0

    def _import_code_slice_event(self, conn: sqlite3.Connection, payload: dict, *, imported_at: str) -> int:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO code_slices (
                id, repo_root, run_id, query, created_at, body,
                host_id, origin_db, logical_clock, sync_batch_id, imported_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.get("id"),
                payload.get("repo_root"),
                payload.get("run_id"),
                payload.get("query"),
                payload.get("created_at"),
                payload.get("body"),
                payload.get("host_id"),
                payload.get("origin_db"),
                payload.get("logical_clock"),
                payload.get("sync_batch_id"),
                imported_at,
            ),
        )
        return 1 if conn.total_changes > before else 0

    def sync_status(self) -> dict:
        with _connect(self.db_path) as conn:
            return {
                "host_id": _host_id(),
                "origin_db": _origin_db(self.db_path),
                "nodes": conn.execute("SELECT count(*) FROM nodes").fetchone()[0],
                "edges": conn.execute("SELECT count(*) FROM edges").fetchone()[0],
                "code_slices": conn.execute("SELECT count(*) FROM code_slices").fetchone()[0],
                "imported_events": conn.execute("SELECT count(*) FROM sync_import_events").fetchone()[0],
                "cursors": [
                    {"peer_id": row["peer_id"], "cursor": row["cursor"], "updated_at": row["updated_at"]}
                    for row in conn.execute("SELECT * FROM sync_cursors ORDER BY peer_id").fetchall()
                ],
            }

    def maintain_code_map(
        self,
        *,
        max_slices_per_repo: int = 50,
        max_slice_age_days: int = 30,
        min_slices_per_repo: int = 5,
        prune_inactive_runs: bool = True,
        vacuum: bool = False,
        backup: bool = True,
    ) -> dict:
        db_path = Path(self.db_path).expanduser()
        backup_path = None
        if backup and db_path.exists():
            stamp = _now_iso().replace(":", "").replace("-", "").replace(".", "")
            backup_path = db_path.with_name(f"{db_path.name}.bak.maintenance_{stamp}")
            shutil.copy2(db_path, backup_path)

        init_db(str(db_path))
        with _connect(str(db_path)) as conn:
            before = {
                "code_index_runs": conn.execute("SELECT count(*) FROM code_index_runs").fetchone()[0],
                "code_slices": conn.execute("SELECT count(*) FROM code_slices").fetchone()[0],
                "code_files": conn.execute("SELECT count(*) FROM code_files").fetchone()[0],
                "code_symbols": conn.execute("SELECT count(*) FROM code_symbols").fetchone()[0],
                "code_edges": conn.execute("SELECT count(*) FROM code_edges").fetchone()[0],
            }
            integrity_before = conn.execute("PRAGMA integrity_check").fetchone()[0]
            deleted_slices = 0
            deleted_runs = 0
            if integrity_before != "ok":
                return {
                    "status": "integrity_failed",
                    "backup_path": str(backup_path) if backup_path else None,
                    "integrity_before": integrity_before,
                    "before": before,
                }

            conn.execute("BEGIN IMMEDIATE")
            repo_rows = conn.execute("SELECT DISTINCT repo_root FROM code_slices").fetchall()
            for repo_row in repo_rows:
                repo_root = repo_row["repo_root"]
                if max_slice_age_days > 0:
                    cutoff = _iso_days_ago(max_slice_age_days)
                    old_by_age = conn.execute(
                        """
                        SELECT id FROM code_slices
                        WHERE repo_root=?
                          AND created_at < ?
                          AND id NOT IN (
                            SELECT id FROM code_slices
                            WHERE repo_root=?
                            ORDER BY created_at DESC
                            LIMIT ?
                          )
                        """,
                        (
                            repo_root,
                            cutoff,
                            repo_root,
                            max(0, min_slices_per_repo),
                        ),
                    ).fetchall()
                    if old_by_age:
                        deleted_slices += len(old_by_age)
                        conn.executemany("DELETE FROM code_slices WHERE id=?", [(row["id"],) for row in old_by_age])

                rows = conn.execute(
                    """
                    SELECT id, created_at, body FROM code_slices
                    WHERE repo_root=?
                    ORDER BY created_at DESC
                    """,
                    (repo_root,),
                ).fetchall()
                if len(rows) > max_slices_per_repo:
                    protected_count = min(max(0, min_slices_per_repo), max_slices_per_repo)
                    protected_ids = {row["id"] for row in rows[:protected_count]}
                    candidates = [row for row in rows if row["id"] not in protected_ids]
                    now = dt.datetime.now(dt.timezone.utc)
                    candidates = sorted(
                        candidates,
                        key=lambda row: (_slice_eviction_score(row, now), row["created_at"], row["id"]),
                        reverse=True,
                    )
                    delete_count = len(rows) - max_slices_per_repo
                    old_rows = candidates[:delete_count]
                    if old_rows:
                        deleted_slices += len(old_rows)
                        conn.executemany("DELETE FROM code_slices WHERE id=?", [(row["id"],) for row in old_rows])

            if prune_inactive_runs:
                deleted_runs = conn.execute("DELETE FROM code_index_runs WHERE active=0").rowcount

            conn.commit()

            after = {
                "code_index_runs": conn.execute("SELECT count(*) FROM code_index_runs").fetchone()[0],
                "code_slices": conn.execute("SELECT count(*) FROM code_slices").fetchone()[0],
                "code_files": conn.execute("SELECT count(*) FROM code_files").fetchone()[0],
                "code_symbols": conn.execute("SELECT count(*) FROM code_symbols").fetchone()[0],
                "code_edges": conn.execute("SELECT count(*) FROM code_edges").fetchone()[0],
            }
            integrity_after = conn.execute("PRAGMA integrity_check").fetchone()[0]

        vacuumed = False
        if vacuum and integrity_after == "ok":
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                conn.execute("VACUUM")
            vacuumed = True

        verify = self.verify_all()
        return {
            "status": "ok" if integrity_after == "ok" and verify["invalid_count"] == 0 else "check_failed",
            "backup_path": str(backup_path) if backup_path else None,
            "integrity_before": integrity_before,
            "integrity_after": integrity_after,
            "deleted_slices": deleted_slices,
            "deleted_inactive_runs": deleted_runs,
            "retention_policy": {
                "max_slices_per_repo": max_slices_per_repo,
                "max_slice_age_days": max_slice_age_days,
                "min_slices_per_repo": min_slices_per_repo,
                "eviction": "age_and_confidence_weighted_after_recent_floor",
                "evidence_nodes_preserved": True,
                "code_slices_are_cache": True,
            },
            "vacuumed": vacuumed,
            "before": before,
            "after": after,
            "verify": {
                "total": verify["total"],
                "invalid_count": verify["invalid_count"],
                "invalid": verify["invalid"][:10],
            },
        }


TimelineReasoningMap = TimelineCodeMap
