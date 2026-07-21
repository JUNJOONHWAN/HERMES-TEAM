from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import DEFAULT_DB_PATH, TimelineCodeMap, _connect


ROADMAP_SCHEMA_VERSION = "hermes.roadmap_event.v1"

ROADMAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS roadmap_entities (
    entity_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    head_node_id TEXT,
    pending_event_id TEXT,
    state TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roadmap_events (
    event_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_version INTEGER NOT NULL,
    expected_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    node_id TEXT,
    status TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    correlation_id TEXT,
    causation_id TEXT,
    policy_bundle_hash TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE(entity_id, entity_version)
);

CREATE TABLE IF NOT EXISTS roadmap_dependencies (
    entity_id TEXT NOT NULL,
    depends_on_entity_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'depends_on',
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(entity_id, depends_on_entity_id, relation)
);

CREATE TABLE IF NOT EXISTS roadmap_schedules (
    entity_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    intended_timezone TEXT NOT NULL,
    intended_local_time TEXT NOT NULL,
    intended_byday_json TEXT NOT NULL DEFAULT '[]',
    stored_rrule_timezone TEXT NOT NULL,
    scheduler_execution_timezone TEXT NOT NULL,
    stored_rrule TEXT NOT NULL,
    reporting_timezone TEXT NOT NULL,
    out_of_window_action TEXT NOT NULL,
    effective_local_date TEXT,
    next_run_utc TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_roadmap_entities_goal_state
    ON roadmap_entities(goal_id, state, entity_type, updated_at);
CREATE INDEX IF NOT EXISTS idx_roadmap_events_goal_entity
    ON roadmap_events(goal_id, entity_id, entity_version);
CREATE INDEX IF NOT EXISTS idx_roadmap_events_status
    ON roadmap_events(status, created_at);
CREATE INDEX IF NOT EXISTS idx_roadmap_dependencies_target
    ON roadmap_dependencies(depends_on_entity_id, relation);
"""


TASK_STATES = {
    "INBOX",
    "CLARIFYING",
    "ANALYSIS_PENDING",
    "ANALYZING",
    "PLANNED",
    "AWAITING_APPROVAL",
    "SCHEDULED",
    "READY",
    "ROUTED",
    "RUNNING",
    "REVIEW",
    "DONE",
    "BLOCKED",
    "PAUSED",
    "FAILED",
    "CANCELLED",
    "ARCHIVED",
}

TASK_TRANSITIONS = {
    "INBOX": {"CLARIFYING", "ANALYSIS_PENDING", "PLANNED", "CANCELLED"},
    "CLARIFYING": {"ANALYSIS_PENDING", "PLANNED", "BLOCKED", "CANCELLED"},
    "ANALYSIS_PENDING": {"ANALYZING", "BLOCKED", "CANCELLED"},
    "ANALYZING": {"PLANNED", "BLOCKED", "FAILED", "CANCELLED"},
    "PLANNED": {"AWAITING_APPROVAL", "SCHEDULED", "READY", "CANCELLED"},
    "AWAITING_APPROVAL": {"SCHEDULED", "READY", "BLOCKED", "CANCELLED"},
    "SCHEDULED": {"AWAITING_APPROVAL", "READY", "PAUSED", "BLOCKED", "CANCELLED"},
    "READY": {"SCHEDULED", "ROUTED", "PAUSED", "BLOCKED", "CANCELLED"},
    "ROUTED": {"RUNNING", "BLOCKED", "FAILED", "CANCELLED"},
    "RUNNING": {"REVIEW", "PAUSED", "BLOCKED", "FAILED", "CANCELLED"},
    "REVIEW": {"DONE", "RUNNING", "BLOCKED", "FAILED", "CANCELLED"},
    "BLOCKED": {"CLARIFYING", "PLANNED", "READY", "RUNNING", "CANCELLED"},
    "PAUSED": {"SCHEDULED", "READY", "RUNNING", "CANCELLED"},
    "FAILED": {"PLANNED", "READY", "CANCELLED"},
    "DONE": {"ARCHIVED"},
    "CANCELLED": {"ARCHIVED"},
    "ARCHIVED": set(),
}


class RoadmapError(RuntimeError):
    pass


class RoadmapConflict(RoadmapError):
    pass


class RoadmapValidationError(RoadmapError):
    pass


class RoadmapInProgress(RoadmapError):
    pass


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def init_roadmap_schema(db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(ROADMAP_SCHEMA)
        schedule_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(roadmap_schedules)").fetchall()
        }
        if "intended_byday_json" not in schedule_columns:
            conn.execute(
                "ALTER TABLE roadmap_schedules ADD COLUMN intended_byday_json TEXT NOT NULL DEFAULT '[]'"
            )
        conn.commit()


def _parse_rrule(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, item = part.split("=", 1)
        result[key.strip().upper()] = item.strip()
    return result


def verify_schedule_contract(payload: dict[str, Any]) -> dict[str, Any]:
    required = (
        "intended_timezone",
        "intended_local_time",
        "stored_rrule_timezone",
        "scheduler_execution_timezone",
        "stored_rrule",
        "reporting_timezone",
        "out_of_window_action",
    )
    errors = [f"missing:{key}" for key in required if not payload.get(key)]
    if errors:
        return {"valid": False, "errors": errors}

    if payload["stored_rrule_timezone"] != "UTC":
        errors.append("stored_rrule_timezone_must_be_UTC")
    if payload["scheduler_execution_timezone"] != "UTC":
        errors.append("scheduler_execution_timezone_must_be_UTC")
    if payload["reporting_timezone"] not in {"KST", "Asia/Seoul"}:
        errors.append("reporting_timezone_must_be_KST")
    if payload["out_of_window_action"] != "scheduler_timezone_mismatch":
        errors.append("out_of_window_action_must_fail_closed")

    try:
        hour_text, minute_text = str(payload["intended_local_time"]).split(":", 1)
        local_hour = int(hour_text)
        local_minute = int(minute_text)
        local_date = dt.date.fromisoformat(
            str(payload.get("effective_local_date") or dt.datetime.now(ZoneInfo("Asia/Seoul")).date())
        )
        zone = ZoneInfo(str(payload["intended_timezone"]))
        local_dt = dt.datetime.combine(local_date, dt.time(local_hour, local_minute), zone)
        utc_dt = local_dt.astimezone(dt.timezone.utc)
        rule = _parse_rrule(str(payload["stored_rrule"]))
        stored_hours = {int(item) for item in rule.get("BYHOUR", "").split(",") if item != ""}
        stored_minutes = {int(item) for item in rule.get("BYMINUTE", "0").split(",") if item != ""}
        if utc_dt.hour not in stored_hours:
            errors.append("stored_rrule_BYHOUR_does_not_match_KST_intent")
        if utc_dt.minute not in stored_minutes:
            errors.append("stored_rrule_BYMINUTE_does_not_match_KST_intent")
        intended_byday = payload.get("intended_byday") or []
        if isinstance(intended_byday, str):
            intended_byday = [item for item in intended_byday.split(",") if item]
        if intended_byday:
            day_codes = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
            if any(item not in day_codes for item in intended_byday):
                errors.append("invalid_intended_byday")
            else:
                date_delta = (utc_dt.date() - local_dt.date()).days
                expected_utc_days = {
                    day_codes[(day_codes.index(item) + date_delta) % 7]
                    for item in intended_byday
                }
                stored_utc_days = {item for item in rule.get("BYDAY", "").split(",") if item}
                if stored_utc_days != expected_utc_days:
                    errors.append("stored_rrule_BYDAY_does_not_match_KST_intent")
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        errors.append(f"invalid_schedule:{exc}")

    return {"valid": not errors, "errors": errors}


class RoadmapStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH, *, timeline: TimelineCodeMap | None = None):
        self.db_path = str(Path(db_path).expanduser())
        self.timeline = timeline or TimelineCodeMap(self.db_path)
        init_roadmap_schema(self.db_path)

    def append_event(
        self,
        *,
        goal_id: str,
        entity_id: str,
        entity_type: str,
        event_type: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
        actor: dict[str, Any] | None = None,
        event_id: str | None = None,
        occurred_at_utc: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        policy_bundle_hash: str | None = None,
        author: str = "hermes-roadmap",
    ) -> dict[str, Any]:
        if not goal_id or not entity_id or not entity_type or not event_type:
            raise RoadmapValidationError("goal_id, entity_id, entity_type, and event_type are required")
        if expected_version < 0:
            raise RoadmapValidationError("expected_version must be non-negative")

        payload = dict(payload or {})
        actor = dict(actor or {"type": "system", "id": author})
        event_id = event_id or f"evt_{uuid.uuid4().hex}"
        occurred_at_utc = occurred_at_utc or _now_iso()
        now = _now_iso()
        old_head: str | None = None
        old_payload: dict[str, Any] = {}

        with _connect(self.db_path) as conn:
            conn.executescript(ROADMAP_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM roadmap_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "committed":
                    conn.commit()
                    return self._event_result(existing, idempotent=True)
                if existing["status"] == "reserved":
                    conn.rollback()
                    raise RoadmapInProgress(f"event {event_id} is already reserved")
                if existing["node_id"]:
                    conn.rollback()
                    raise RoadmapInProgress(f"event {event_id} requires recovery")
                conn.execute("DELETE FROM roadmap_events WHERE event_id=?", (event_id,))

            entity = conn.execute(
                "SELECT * FROM roadmap_entities WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
            current_version = int(entity["current_version"]) if entity else 0
            if current_version != expected_version:
                conn.rollback()
                raise RoadmapConflict(
                    f"entity {entity_id} expected version {expected_version}, current version {current_version}"
                )
            if entity and entity["pending_event_id"]:
                conn.rollback()
                raise RoadmapInProgress(
                    f"entity {entity_id} has pending event {entity['pending_event_id']}"
                )

            old_state = str(entity["state"]) if entity else self._initial_state(entity_type, payload)
            new_state = str(payload.get("state") or old_state).upper()
            self._validate_state(entity_type, old_state, new_state, first_event=entity is None)
            old_head = str(entity["head_node_id"]) if entity and entity["head_node_id"] else None
            old_payload = _load(entity["payload_json"], {}) if entity else {}
            next_version = current_version + 1
            title = str(payload.get("title") or (entity["title"] if entity else entity_id))

            if entity is None:
                conn.execute(
                    """
                    INSERT INTO roadmap_entities (
                        entity_id, goal_id, entity_type, current_version, head_node_id,
                        pending_event_id, state, title, payload_json, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (entity_id, goal_id, entity_type, 0, None, event_id, old_state, title, _json({}), now),
                )
            else:
                if entity["goal_id"] != goal_id or entity["entity_type"] != entity_type:
                    conn.rollback()
                    raise RoadmapConflict("entity identity cannot change goal_id or entity_type")
                conn.execute(
                    "UPDATE roadmap_entities SET pending_event_id=?, updated_at=? WHERE entity_id=?",
                    (event_id, now, entity_id),
                )

            conn.execute(
                """
                INSERT INTO roadmap_events (
                    event_id, goal_id, entity_id, entity_type, entity_version,
                    expected_version, event_type, node_id, status, actor_json,
                    payload_json, occurred_at_utc, correlation_id, causation_id,
                    policy_bundle_hash, error, created_at, committed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    goal_id,
                    entity_id,
                    entity_type,
                    next_version,
                    expected_version,
                    event_type,
                    None,
                    "reserved",
                    _json(actor),
                    _json(payload),
                    occurred_at_utc,
                    correlation_id,
                    causation_id,
                    policy_bundle_hash,
                    None,
                    now,
                    None,
                ),
            )
            conn.commit()

        envelope = {
            "schema": ROADMAP_SCHEMA_VERSION,
            "event_id": event_id,
            "event_type": event_type,
            "goal_id": goal_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_version": expected_version + 1,
            "expected_version": expected_version,
            "actor": actor,
            "occurred_at_utc": occurred_at_utc,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "policy_bundle_hash": policy_bundle_hash,
            "payload": payload,
        }

        node_id: str | None = None
        try:
            node_id = self.timeline.record(
                domain="roadmap",
                kind=f"{entity_type}_event",
                title=f"{event_type}: {payload.get('title') or entity_id}",
                body=envelope,
                author=author,
                goal_id=goal_id,
            )
            if old_head:
                self.timeline.link(node_id, old_head, "supersedes", author=author)
            self._link_semantic_targets(node_id, payload, author=author)
            return self._finalize_event(
                event_id=event_id,
                node_id=node_id,
                new_state=new_state,
                title=title,
                old_payload=old_payload,
                payload=payload,
            )
        except Exception as exc:
            with _connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if node_id:
                    conn.execute(
                        "UPDATE roadmap_events SET node_id=?, error=? WHERE event_id=?",
                        (node_id, f"finalization_pending:{exc}", event_id),
                    )
                else:
                    conn.execute(
                        "UPDATE roadmap_events SET status='failed', error=? WHERE event_id=?",
                        (str(exc), event_id),
                    )
                    conn.execute(
                        "UPDATE roadmap_entities SET pending_event_id=NULL WHERE entity_id=? AND pending_event_id=?",
                        (entity_id, event_id),
                    )
                conn.commit()
            raise

    def _finalize_event(
        self,
        *,
        event_id: str,
        node_id: str,
        new_state: str,
        title: str,
        old_payload: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now_iso()
        merged_payload = {**old_payload, **payload}
        with _connect(self.db_path) as conn:
            conn.executescript(ROADMAP_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute(
                "SELECT * FROM roadmap_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if event is None:
                conn.rollback()
                raise RoadmapConflict(f"reservation disappeared for event {event_id}")
            entity = conn.execute(
                "SELECT * FROM roadmap_entities WHERE entity_id=?",
                (event["entity_id"],),
            ).fetchone()
            if entity is None or entity["pending_event_id"] != event_id:
                conn.rollback()
                raise RoadmapConflict(f"event {event_id} no longer owns entity reservation")

            conn.execute(
                """
                UPDATE roadmap_events
                SET node_id=?, status='committed', error=NULL, committed_at=?
                WHERE event_id=?
                """,
                (node_id, now, event_id),
            )
            conn.execute(
                """
                UPDATE roadmap_entities
                SET current_version=?, head_node_id=?, pending_event_id=NULL,
                    state=?, title=?, payload_json=?, updated_at=?
                WHERE entity_id=?
                """,
                (
                    event["entity_version"],
                    node_id,
                    new_state,
                    title,
                    _json(merged_payload),
                    now,
                    event["entity_id"],
                ),
            )
            self._apply_event_projection(conn, dict(event), payload, now)
            conn.commit()
            committed = conn.execute(
                "SELECT * FROM roadmap_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return self._event_result(committed, idempotent=False)

    def _apply_event_projection(
        self,
        conn: sqlite3.Connection,
        event: dict[str, Any],
        payload: dict[str, Any],
        now: str,
    ) -> None:
        if event["event_type"] in {"schedule.set", "scheduler.timezone_mismatch"}:
            schedule_result = verify_schedule_contract(payload)
            if not schedule_result["valid"]:
                raise RoadmapValidationError(
                    "invalid schedule contract: " + ", ".join(schedule_result["errors"])
                )
            conn.execute(
                """
                INSERT INTO roadmap_schedules (
                    entity_id, source_event_id, intended_timezone, intended_local_time, intended_byday_json,
                    stored_rrule_timezone, scheduler_execution_timezone, stored_rrule,
                    reporting_timezone, out_of_window_action, effective_local_date,
                    next_run_utc, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    source_event_id=excluded.source_event_id,
                    intended_timezone=excluded.intended_timezone,
                    intended_local_time=excluded.intended_local_time,
                    intended_byday_json=excluded.intended_byday_json,
                    stored_rrule_timezone=excluded.stored_rrule_timezone,
                    scheduler_execution_timezone=excluded.scheduler_execution_timezone,
                    stored_rrule=excluded.stored_rrule,
                    reporting_timezone=excluded.reporting_timezone,
                    out_of_window_action=excluded.out_of_window_action,
                    effective_local_date=excluded.effective_local_date,
                    next_run_utc=excluded.next_run_utc,
                    updated_at=excluded.updated_at
                """,
                (
                    event["entity_id"],
                    event["event_id"],
                    payload["intended_timezone"],
                    payload["intended_local_time"],
                    _json(payload.get("intended_byday") or []),
                    payload["stored_rrule_timezone"],
                    payload["scheduler_execution_timezone"],
                    payload["stored_rrule"],
                    payload["reporting_timezone"],
                    payload["out_of_window_action"],
                    payload.get("effective_local_date"),
                    payload.get("next_run_utc"),
                    now,
                ),
            )
        if event["event_type"] == "dependency.added":
            target = str(payload.get("depends_on_entity_id") or "")
            if not target or target == event["entity_id"]:
                raise RoadmapValidationError("dependency requires a different depends_on_entity_id")
            conn.execute(
                """
                INSERT OR REPLACE INTO roadmap_dependencies (
                    entity_id, depends_on_entity_id, relation, source_event_id, created_at
                ) VALUES (?,?,?,?,?)
                """,
                (event["entity_id"], target, "depends_on", event["event_id"], now),
            )
        if event["event_type"] == "dependency.removed":
            target = str(payload.get("depends_on_entity_id") or "")
            conn.execute(
                "DELETE FROM roadmap_dependencies WHERE entity_id=? AND depends_on_entity_id=?",
                (event["entity_id"], target),
            )

    def _link_semantic_targets(self, node_id: str, payload: dict[str, Any], *, author: str) -> None:
        targets = (
            ("parent_entity_id", "part_of"),
            ("depends_on_entity_id", "depends_on"),
            ("dispatch_to_node_id", "dispatched_to"),
            ("artifact_node_id", "produces"),
            ("verification_node_id", "verified_by"),
        )
        with _connect(self.db_path) as conn:
            for key, relation in targets:
                target = payload.get(key)
                if not target:
                    continue
                if key.endswith("entity_id"):
                    row = conn.execute(
                        "SELECT head_node_id FROM roadmap_entities WHERE entity_id=?",
                        (str(target),),
                    ).fetchone()
                    target = row["head_node_id"] if row else None
                if target:
                    self.timeline.link(node_id, str(target), relation, author=author)

    def _initial_state(self, entity_type: str, payload: dict[str, Any]) -> str:
        if payload.get("state"):
            return str(payload["state"]).upper()
        return "INBOX" if entity_type == "task" else "DRAFT"

    def _validate_state(
        self,
        entity_type: str,
        old_state: str,
        new_state: str,
        *,
        first_event: bool,
    ) -> None:
        if entity_type != "task":
            return
        if new_state not in TASK_STATES:
            raise RoadmapValidationError(f"unknown task state {new_state}")
        if first_event:
            if new_state != "INBOX":
                raise RoadmapValidationError("new tasks must start in INBOX")
            return
        if new_state == old_state:
            return
        if new_state not in TASK_TRANSITIONS.get(old_state, set()):
            raise RoadmapValidationError(f"invalid task transition {old_state}->{new_state}")

    def _event_result(self, row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "entity_id": row["entity_id"],
            "entity_version": row["entity_version"],
            "event_type": row["event_type"],
            "node_id": row["node_id"],
            "status": row["status"],
            "idempotent": idempotent,
        }

    def get_roadmap(
        self,
        goal_id: str,
        *,
        entity_type: str | None = None,
        state: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            query = "SELECT * FROM roadmap_entities WHERE goal_id=?"
            params: list[Any] = [goal_id]
            if entity_type:
                query += " AND entity_type=?"
                params.append(entity_type)
            if state:
                query += " AND state=?"
                params.append(state.upper())
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            entities = [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]
            dependencies = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT d.* FROM roadmap_dependencies d
                    JOIN roadmap_entities e ON e.entity_id=d.entity_id
                    WHERE e.goal_id=? ORDER BY d.entity_id, d.depends_on_entity_id
                    """,
                    (goal_id,),
                ).fetchall()
            ]
            schedules = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT s.* FROM roadmap_schedules s
                    JOIN roadmap_entities e ON e.entity_id=s.entity_id
                    WHERE e.goal_id=? ORDER BY s.updated_at DESC
                    """,
                    (goal_id,),
                ).fetchall()
            ]
        for schedule in schedules:
            schedule["intended_byday"] = _load(schedule.pop("intended_byday_json", "[]"), [])
        for entity in entities:
            entity["payload"] = _load(entity.pop("payload_json"), {})
        return {
            "goal_id": goal_id,
            "entities": entities,
            "dependencies": dependencies,
            "schedules": schedules,
        }

    def list_goal_ids(self, *, limit: int = 10000) -> list[str]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT goal_id FROM roadmap_entities ORDER BY goal_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [str(row["goal_id"]) for row in rows]

    def get_entity_history(self, entity_id: str, *, entity_type: str | None = None) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            query = "SELECT * FROM roadmap_entities WHERE entity_id=?"
            params: list[Any] = [entity_id]
            if entity_type:
                query += " AND entity_type=?"
                params.append(entity_type)
            entity = conn.execute(query, tuple(params)).fetchone()
            events = conn.execute(
                "SELECT * FROM roadmap_events WHERE entity_id=? ORDER BY entity_version",
                (entity_id,),
            ).fetchall()
        result_events = []
        for event in events:
            item = dict(event)
            item["actor"] = _load(item.pop("actor_json"), {})
            item["payload"] = _load(item.pop("payload_json"), {})
            item["timeline_node"] = self.timeline.get_node(item["node_id"]) if item["node_id"] else None
            evidence_nodes: dict[str, dict[str, Any]] = {}
            for key in ("artifact_node_id", "verification_node_id", "dispatch_to_node_id"):
                node_id = item["payload"].get(key)
                if node_id:
                    node = self.timeline.get_node(str(node_id))
                    if node:
                        evidence_nodes[key] = node
            item["evidence_nodes"] = evidence_nodes
            result_events.append(item)
        entity_result = _row(entity)
        if entity_result:
            entity_result["payload"] = _load(entity_result.pop("payload_json"), {})
        return {"entity": entity_result, "events": result_events}

    def get_task_history(self, task_id: str) -> dict[str, Any]:
        result = self.get_entity_history(task_id, entity_type="task")
        return {"task": result["entity"], "events": result["events"]}

    def sync_status(self) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            counts = {
                row["status"]: row["count"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM roadmap_events GROUP BY status"
                ).fetchall()
            }
            watermark = conn.execute(
                """
                SELECT event_id, node_id, committed_at
                FROM roadmap_events WHERE status='committed'
                ORDER BY committed_at DESC LIMIT 1
                """
            ).fetchone()
        return {"event_counts": counts, "watermark": _row(watermark)}

    def rebuild_projection(self, *, goal_id: str | None = None) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            query = "SELECT * FROM nodes WHERE domain='roadmap'"
            params: list[Any] = []
            if goal_id:
                query += " AND goal_id=?"
                params.append(goal_id)
            query += " ORDER BY COALESCE(ts, created_at), logical_clock, id"
            nodes = conn.execute(query, tuple(params)).fetchall()

        parsed: list[tuple[sqlite3.Row, dict[str, Any]]] = []
        errors: list[str] = []
        for node in nodes:
            body = _load(node["body"], {})
            if body.get("schema") != ROADMAP_SCHEMA_VERSION:
                continue
            try:
                parsed.append((node, body))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"node:{node['id']}:{exc}")

        with _connect(self.db_path) as conn:
            conn.executescript(ROADMAP_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            if goal_id:
                entity_ids = [
                    row["entity_id"]
                    for row in conn.execute(
                        "SELECT entity_id FROM roadmap_entities WHERE goal_id=?",
                        (goal_id,),
                    ).fetchall()
                ]
                for entity_id in entity_ids:
                    conn.execute("DELETE FROM roadmap_dependencies WHERE entity_id=?", (entity_id,))
                    conn.execute("DELETE FROM roadmap_schedules WHERE entity_id=?", (entity_id,))
                conn.execute("DELETE FROM roadmap_events WHERE goal_id=?", (goal_id,))
                conn.execute("DELETE FROM roadmap_entities WHERE goal_id=?", (goal_id,))
            else:
                conn.execute("DELETE FROM roadmap_dependencies")
                conn.execute("DELETE FROM roadmap_schedules")
                conn.execute("DELETE FROM roadmap_events")
                conn.execute("DELETE FROM roadmap_entities")

            grouped: dict[str, list[tuple[sqlite3.Row, dict[str, Any]]]] = {}
            for node, body in parsed:
                grouped.setdefault(str(body.get("entity_id")), []).append((node, body))

            for entity_id, items in grouped.items():
                items.sort(key=lambda pair: int(pair[1].get("entity_version", 0)))
                expected = 1
                current_payload: dict[str, Any] = {}
                for node, body in items:
                    version = int(body.get("entity_version", 0))
                    if version != expected:
                        errors.append(f"entity:{entity_id}:version_gap:{expected}->{version}")
                        expected = version
                    payload = dict(body.get("payload") or {})
                    current_payload.update(payload)
                    now = str(node["ts"] or node["created_at"] or _now_iso())
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO roadmap_events (
                            event_id, goal_id, entity_id, entity_type, entity_version,
                            expected_version, event_type, node_id, status, actor_json,
                            payload_json, occurred_at_utc, correlation_id, causation_id,
                            policy_bundle_hash, error, created_at, committed_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            body["event_id"],
                            body["goal_id"],
                            entity_id,
                            body["entity_type"],
                            version,
                            body.get("expected_version", version - 1),
                            body["event_type"],
                            node["id"],
                            "committed",
                            _json(body.get("actor") or {}),
                            _json(payload),
                            body.get("occurred_at_utc") or now,
                            body.get("correlation_id"),
                            body.get("causation_id"),
                            body.get("policy_bundle_hash"),
                            None,
                            now,
                            now,
                        ),
                    )
                    state = str(payload.get("state") or current_payload.get("state") or (
                        "INBOX" if body["entity_type"] == "task" else "DRAFT"
                    )).upper()
                    title = str(payload.get("title") or current_payload.get("title") or entity_id)
                    conn.execute(
                        """
                        INSERT INTO roadmap_entities (
                            entity_id, goal_id, entity_type, current_version, head_node_id,
                            pending_event_id, state, title, payload_json, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(entity_id) DO UPDATE SET
                            current_version=excluded.current_version,
                            head_node_id=excluded.head_node_id,
                            pending_event_id=NULL,
                            state=excluded.state,
                            title=excluded.title,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            entity_id,
                            body["goal_id"],
                            body["entity_type"],
                            version,
                            node["id"],
                            None,
                            state,
                            title,
                            _json(current_payload),
                            now,
                        ),
                    )
                    event_record = {
                        "event_id": body["event_id"],
                        "entity_id": entity_id,
                        "event_type": body["event_type"],
                    }
                    try:
                        self._apply_event_projection(conn, event_record, payload, now)
                    except RoadmapValidationError as exc:
                        errors.append(f"event:{body['event_id']}:{exc}")
                    expected += 1
            conn.commit()
        return {
            "goal_id": goal_id,
            "nodes_scanned": len(nodes),
            "events_rebuilt": len(parsed),
            "entity_count": len(grouped),
            "errors": errors,
        }

    def verify_goal_contract(self, goal_id: str) -> dict[str, Any]:
        errors: list[str] = []
        with _connect(self.db_path) as conn:
            entities = conn.execute(
                "SELECT * FROM roadmap_entities WHERE goal_id=?",
                (goal_id,),
            ).fetchall()
            dependencies = conn.execute(
                """
                SELECT d.* FROM roadmap_dependencies d
                JOIN roadmap_entities e ON e.entity_id=d.entity_id
                WHERE e.goal_id=?
                """,
                (goal_id,),
            ).fetchall()
            for entity in entities:
                events = conn.execute(
                    "SELECT * FROM roadmap_events WHERE entity_id=? ORDER BY entity_version",
                    (entity["entity_id"],),
                ).fetchall()
                versions = [int(event["entity_version"]) for event in events if event["status"] == "committed"]
                expected_versions = list(range(1, int(entity["current_version"]) + 1))
                if versions != expected_versions:
                    errors.append(f"{entity['entity_id']}:version_sequence:{versions}")
                if entity["pending_event_id"]:
                    errors.append(f"{entity['entity_id']}:pending_event:{entity['pending_event_id']}")
                if entity["head_node_id"] and not self.timeline.get_node(entity["head_node_id"]):
                    errors.append(f"{entity['entity_id']}:missing_head_node")
                event_types = [event["event_type"] for event in events if event["status"] == "committed"]
                if entity["entity_type"] == "task" and entity["state"] == "DONE":
                    if "verification.passed" not in event_types:
                        errors.append(f"{entity['entity_id']}:done_without_verification")
                    if "dispatch.created" in event_types and "executor.result" not in event_types:
                        errors.append(f"{entity['entity_id']}:done_without_executor_result")

                for event in events:
                    if event["event_type"] in {"schedule.set", "scheduler.timezone_mismatch"} and event["status"] == "committed":
                        result = verify_schedule_contract(_load(event["payload_json"], {}))
                        errors.extend(
                            f"{entity['entity_id']}:schedule:{item}" for item in result["errors"]
                        )

            graph: dict[str, set[str]] = {}
            entity_ids = {str(entity["entity_id"]) for entity in entities}
            for dependency in dependencies:
                source = str(dependency["entity_id"])
                target = str(dependency["depends_on_entity_id"])
                if target not in entity_ids:
                    errors.append(f"{source}:missing_dependency:{target}")
                graph.setdefault(source, set()).add(target)

            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(node: str) -> None:
                if node in visiting:
                    errors.append(f"dependency_cycle:{node}")
                    return
                if node in visited:
                    return
                visiting.add(node)
                for target in graph.get(node, set()):
                    visit(target)
                visiting.remove(node)
                visited.add(node)

            for entity_id in entity_ids:
                visit(entity_id)

        timeline_verify = self.timeline.verify_all()
        if timeline_verify["invalid_count"]:
            errors.append(f"timeline_invalid_count:{timeline_verify['invalid_count']}")
        return {
            "goal_id": goal_id,
            "valid": not errors,
            "entity_count": len(entities),
            "errors": errors,
            "timeline_verify": timeline_verify,
        }
