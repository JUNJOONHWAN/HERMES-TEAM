from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any


NEURAL_RELATIONS = ("same_workflow", "same_entity", "same_concept", "associates")
MAX_FEATURE_TOKENS = 48
MAX_FEATURE_ENTITIES = 24
MAX_FEATURE_WORKFLOWS = 12
DEFAULT_CANDIDATE_LIMIT = 24
DEFAULT_LINK_LIMIT = 4
FEATURE_VERSION_MARKER = "feature:v5-temporal-scope"

NEURAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS neural_node_features (
    node_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    kind TEXT NOT NULL,
    goal_id TEXT,
    freshness_class TEXT NOT NULL,
    expires_at TEXT,
    activation_count INTEGER NOT NULL DEFAULT 0,
    last_activated_at TEXT,
    indexed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS neural_feature_terms (
    node_id TEXT NOT NULL,
    term_type TEXT NOT NULL,
    term TEXT NOT NULL,
    PRIMARY KEY (node_id, term_type, term)
);

CREATE INDEX IF NOT EXISTS idx_neural_terms_lookup
ON neural_feature_terms(term_type, term, node_id);
CREATE INDEX IF NOT EXISTS idx_neural_features_goal
ON neural_node_features(goal_id, indexed_at);
CREATE INDEX IF NOT EXISTS idx_neural_features_expiry
ON neural_node_features(expires_at);
"""

_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+-]{2,}|[가-힣]{2,}")
_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_.+@%=-]+){2,}|(?:[A-Za-z]:[\\/][^\s\"']+)")
_UPPER_ENTITY_RE = re.compile(r"\b[A-Z][A-Z0-9.^=-]{1,11}\b")
_ERROR_ENTITY_RE = re.compile(
    r"\b(?:HTTP\s*)?(?:4\d\d|5\d\d)\b|\b(?:ERR(?:OR)?|EXC)[_-]?[A-Z0-9_-]{2,}\b",
    re.IGNORECASE,
)
_DEEP_RECALL_RE = re.compile(
    r"전에|예전|과거|기억|더듬|이전|원래|왜.{0,8}(?:했|됐)|history|previous|earlier|remember",
    re.IGNORECASE,
)

_STOPWORDS = {
    "그리고", "그러나", "그런데", "대한", "위한", "에서", "으로", "이다", "있다", "한다",
    "이것", "저것", "그것", "the", "and", "for", "from", "with", "this", "that", "into",
    "node", "nodes", "tool", "result", "status", "action", "output", "report",
}
_ENTITY_BODY_KEYS = {
    "task_id", "run_id", "goal_id", "entity_id", "ticker", "symbol", "file", "file_path",
    "repo", "repo_root", "error", "error_code", "source", "provider", "service",
}
_WORKFLOW_BODY_KEYS = {
    "workflow", "workflow_id", "workflow_name", "playbook", "applied_playbook", "command",
    "job", "job_name", "script", "automation", "lane", "role_shell", "binding", "executor",
}
_CONCEPT_BODY_KEYS = {
    "concept", "concepts", "semantic_tag", "semantic_tags", "memory_tag", "memory_tags",
    "alias", "aliases", "keyword", "keywords", "topic", "topics", "intent", "intents",
}

_FRESHNESS_CLASSES = {"market_live", "runtime_state", "episodic", "durable"}
_TEMPORAL_SCOPE_ALIASES = {
    "current": "market_live",
    "intraday": "market_live",
    "live": "market_live",
    "market_live": "market_live",
    "short_term": "market_live",
    "runtime": "runtime_state",
    "runtime_state": "runtime_state",
    "session_state": "runtime_state",
    "episode": "episodic",
    "episodic": "episodic",
    "historical": "episodic",
    "durable": "durable",
    "long_term": "durable",
    "permanent": "durable",
    "persistent": "durable",
}
_DURABLE_DOMAINS = {"memory", "policy", "architecture", "projects", "code", "reasoning", "documentation"}
_DURABLE_KINDS = {
    "architecture", "conclusion", "contract", "decision", "knowhow", "playbook",
    "policy", "preference", "procedure", "repository", "runbook", "symbol",
}
_EPISODIC_KINDS = {
    "action", "analysis", "checkpoint", "diagnosis", "implementation", "lesson",
    "output", "post_impact_report", "pre_impact_report", "report", "research",
    "review", "summary", "verification",
}
_MARKET_LIVE_KINDS = {"bar", "intraday", "market_pulse", "orderbook", "price", "quote", "snapshot", "tick"}
_RUNTIME_STATE_KINDS = {"heartbeat", "health", "probe", "status"}


def ensure_neural_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(NEURAL_SCHEMA)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _normalize_term(value: Any, *, max_chars: int = 180) -> str:
    text = str(value or "").strip().lower()[:max_chars]
    text = re.sub(r"\s+", " ", text)
    return text


def _tokens(text: str, *, limit: int = MAX_FEATURE_TOKENS) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_RE.finditer(text[:12000]):
        token = match.group(0).strip("./:+-").lower()
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        result.append(token)
        if len(result) >= limit:
            break
    return result


def _flatten_keyed_values(value: Any, keys: set[str], *, limit: int = 32) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in keys:
                    values = child if isinstance(child, (list, tuple, set)) else [child]
                    for value in list(values)[:32]:
                        if isinstance(value, (str, int, float)):
                            normalized = _normalize_term(value)
                            if normalized:
                                found.append(normalized)
                elif isinstance(child, (dict, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item[:32]:
                visit(child)

    visit(value)
    return list(dict.fromkeys(found))[:limit]


def _body_value_and_text(body: Any) -> tuple[Any, str]:
    if body is None:
        return None, ""
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return body, body
        return parsed, body
    try:
        return body, json.dumps(body, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return body, str(body)


def _normalized_scope(value: Any) -> str | None:
    token = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    if token in _FRESHNESS_CLASSES:
        return token
    return _TEMPORAL_SCOPE_ALIASES.get(token)


def _explicit_freshness(body_value: Any) -> tuple[str | None, float | None, str | None]:
    if not isinstance(body_value, dict):
        return None, None, None
    descriptor = body_value.get("memory_descriptor")
    sources = [descriptor, body_value] if isinstance(descriptor, dict) else [body_value]
    for source in sources:
        freshness_class = _normalized_scope(source.get("freshness_class"))
        if freshness_class is None:
            freshness_class = _normalized_scope(source.get("temporal_scope"))
        if freshness_class is None:
            continue
        ttl_days: float | None = None
        try:
            raw_ttl = source.get("ttl_days")
            if raw_ttl is not None:
                candidate_ttl = float(raw_ttl)
                if 0 < candidate_ttl <= 3650:
                    ttl_days = candidate_ttl
        except (TypeError, ValueError):
            ttl_days = None
        expires_at = str(source.get("expires_at") or "").strip() or None
        if expires_at and _parse_iso(expires_at).year == 1970:
            expires_at = None
        return freshness_class, ttl_days, expires_at
    return None, None, None


def _expiry(created_at: str | None, ttl_days: float) -> str:
    created = _parse_iso(created_at)
    if created.year == 1970:
        created = dt.datetime.now(dt.timezone.utc)
    return (created + dt.timedelta(days=ttl_days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _freshness(
    domain: str,
    kind: str,
    text: str,
    created_at: str | None,
    body_value: Any,
) -> tuple[str, str | None]:
    explicit_class, explicit_ttl, explicit_expiry = _explicit_freshness(body_value)
    if explicit_class:
        if explicit_class in {"durable", "episodic"}:
            return explicit_class, None
        if explicit_expiry:
            return explicit_class, explicit_expiry
        default_ttl = 1.0 if explicit_class == "market_live" else 7.0
        return explicit_class, _expiry(created_at, explicit_ttl or default_ttl)

    normalized_domain = domain.lower()
    normalized_kind = kind.lower()
    lowered = text.lower()

    # Contracts, decisions, architecture, and accumulated know-how remain durable
    # even when their text mentions live prices or market workflows.
    if normalized_domain in _DURABLE_DOMAINS or normalized_kind in _DURABLE_KINDS:
        return "durable", None
    # Reports and completed work are historical episodes, not live quotes.
    if normalized_kind in _EPISODIC_KINDS:
        return "episodic", None
    if normalized_kind in _RUNTIME_STATE_KINDS or any(
        marker in lowered for marker in ("service status", "source_status", "runtime status")
    ):
        return "runtime_state", _expiry(created_at, 7.0)
    if normalized_kind in _MARKET_LIVE_KINDS or normalized_domain in {"market", "trading", "investment"} or any(
        marker in lowered for marker in ("quote", "intraday", "장중", "vix", "as_of", "market pulse")
    ):
        return "market_live", _expiry(created_at, 1.0)
    return "episodic", None


def extract_features(
    *,
    domain: str,
    kind: str,
    title: str | None,
    body: Any,
    file_path: str | None,
    goal_id: str | None,
    created_at: str | None,
) -> dict[str, Any]:
    body_value, body_text = _body_value_and_text(body)
    title_text = str(title or "")
    combined = "\n".join(part for part in (title_text, body_text[:10000], str(file_path or "")) if part)

    token_terms = _tokens(combined)
    entity_terms = _flatten_keyed_values(body_value, _ENTITY_BODY_KEYS, limit=MAX_FEATURE_ENTITIES)
    entity_terms.extend(_normalize_term(item) for item in _PATH_RE.findall(combined))
    entity_terms.extend(_normalize_term(item) for item in _UPPER_ENTITY_RE.findall(combined))
    entity_terms.extend(_normalize_term(item) for item in _ERROR_ENTITY_RE.findall(combined))
    if file_path:
        entity_terms.append(_normalize_term(file_path))

    workflow_terms = _flatten_keyed_values(body_value, _WORKFLOW_BODY_KEYS, limit=MAX_FEATURE_WORKFLOWS)
    concept_terms = _flatten_keyed_values(body_value, _CONCEPT_BODY_KEYS, limit=MAX_FEATURE_WORKFLOWS)
    title_tokens = _tokens(title_text, limit=12)
    if len(title_tokens) >= 2:
        workflow_terms.append("title:" + "|".join(title_tokens))
    if file_path:
        filename = str(file_path).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if filename:
            workflow_terms.append("file:" + filename)

    freshness_class, expires_at = _freshness(domain, kind, combined, created_at, body_value)
    return {
        "domain": domain or "unknown",
        "kind": kind or "unknown",
        "goal_id": goal_id,
        "freshness_class": freshness_class,
        "expires_at": expires_at,
        "terms": {
            "workflow": list(dict.fromkeys(filter(None, workflow_terms)))[:MAX_FEATURE_WORKFLOWS],
            "entity": list(dict.fromkeys(filter(None, entity_terms)))[:MAX_FEATURE_ENTITIES],
            "concept": list(dict.fromkeys(filter(None, concept_terms)))[:MAX_FEATURE_WORKFLOWS],
            "title": title_tokens,
            "token": token_terms[:MAX_FEATURE_TOKENS],
            "meta": [FEATURE_VERSION_MARKER],
        },
    }


def _term_sets(conn: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, dict[str, set[str]]]:
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT node_id, term_type, term FROM neural_feature_terms WHERE node_id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    result: dict[str, dict[str, set[str]]] = {
        node_id: {"workflow": set(), "entity": set(), "concept": set(), "title": set(), "token": set()}
        for node_id in ids
    }
    for row in rows:
        result.setdefault(row["node_id"], {}).setdefault(row["term_type"], set()).add(row["term"])
    return result


def _candidate_ids(
    conn: sqlite3.Connection,
    features: dict[str, Any],
    *,
    exclude_node_id: str | None,
    candidate_limit: int,
    include_expired: bool = False,
) -> list[str]:
    clauses: list[str] = []
    params: list[Any] = []
    for term_type in ("workflow", "entity", "concept", "title", "token"):
        values = list(features["terms"].get(term_type) or [])
        if not values:
            continue
        placeholders = ",".join("?" for _ in values)
        clauses.append(f"(t.term_type=? AND t.term IN ({placeholders}))")
        params.extend([term_type, *values])

    if clauses:
        query = f"""
            SELECT t.node_id, COUNT(*) AS matches, MAX(n.created_at) AS created_at
            FROM neural_feature_terms t
            JOIN neural_node_features f ON f.node_id=t.node_id
            JOIN nodes n ON n.id=t.node_id
            WHERE ({' OR '.join(clauses)})
        """
        if not include_expired:
            query += " AND (f.expires_at IS NULL OR f.expires_at >= ?)"
            params.append(_now_iso())
        if exclude_node_id:
            query += " AND t.node_id <> ?"
            params.append(exclude_node_id)
        if features.get("goal_id"):
            query += " AND COALESCE(f.goal_id, '') <> ?"
            params.append(features["goal_id"])
        query += " GROUP BY t.node_id ORDER BY matches DESC, created_at DESC LIMIT ?"
        params.append(max(1, candidate_limit))
        return [row["node_id"] for row in conn.execute(query, tuple(params)).fetchall()]

    query = """
        SELECT f.node_id
        FROM neural_node_features f
        JOIN nodes n ON n.id=f.node_id
        WHERE f.domain=? AND f.kind=?
    """
    params = [features["domain"], features["kind"]]
    if not include_expired:
        query += " AND (f.expires_at IS NULL OR f.expires_at >= ?)"
        params.append(_now_iso())
    if exclude_node_id:
        query += " AND f.node_id <> ?"
        params.append(exclude_node_id)
    if features.get("goal_id"):
        query += " AND COALESCE(f.goal_id, '') <> ?"
        params.append(features["goal_id"])
    query += " ORDER BY n.created_at DESC LIMIT ?"
    params.append(min(8, max(1, candidate_limit)))
    return [row["node_id"] for row in conn.execute(query, tuple(params)).fetchall()]


def _recency_score(created_at: str | None, freshness_class: str) -> float:
    age_days = max(0.0, (dt.datetime.now(dt.timezone.utc) - _parse_iso(created_at)).total_seconds() / 86400)
    half_life = {
        "market_live": 0.5,
        "runtime_state": 3.0,
        "episodic": 45.0,
        "durable": 365.0,
    }.get(freshness_class, 45.0)
    return math.exp(-math.log(2) * age_days / half_life)


def _is_expired(expires_at: str | None, *, now: dt.datetime | None = None) -> bool:
    if not expires_at:
        return False
    parsed = _parse_iso(expires_at)
    current = now or dt.datetime.now(dt.timezone.utc)
    return parsed <= current


def _score_candidate(
    source: dict[str, Any],
    candidate_terms: dict[str, set[str]],
    candidate_row: sqlite3.Row,
) -> tuple[float, str]:
    source_terms = {name: set(values) for name, values in source["terms"].items()}
    workflow_overlap = source_terms["workflow"] & candidate_terms.get("workflow", set())
    entity_overlap = source_terms["entity"] & candidate_terms.get("entity", set())
    concept_overlap = source_terms.get("concept", set()) & candidate_terms.get("concept", set())
    token_union = source_terms["token"] | candidate_terms.get("token", set())
    token_overlap = source_terms["token"] & candidate_terms.get("token", set())
    token_jaccard = len(token_overlap) / max(1, len(token_union))
    source_coverage = len(token_overlap) / max(1, len(source_terms["token"]))
    candidate_coverage = len(token_overlap) / max(1, len(candidate_terms.get("token", set())))
    title_overlap = source_terms.get("title", set()) & candidate_terms.get("title", set())
    title_source_coverage = len(title_overlap) / max(1, len(source_terms.get("title", set())))
    title_candidate_coverage = len(title_overlap) / max(1, len(candidate_terms.get("title", set())))

    score = 0.0
    relation = "associates"
    if workflow_overlap:
        score += 0.58 + min(0.12, 0.04 * len(workflow_overlap))
        relation = "same_workflow"
    if entity_overlap:
        score += min(0.34, 0.18 + 0.06 * len(entity_overlap))
        if not workflow_overlap:
            relation = "same_entity"
    if concept_overlap:
        score += 0.46 + min(0.18, 0.06 * len(concept_overlap))
        if not workflow_overlap and not entity_overlap:
            relation = "same_concept"
    if len(title_overlap) >= 2:
        score += min(0.48, title_source_coverage * 0.60 + title_candidate_coverage * 0.10)
    score += min(
        0.46,
        max(
            token_jaccard * 0.68,
            source_coverage * 0.55 + candidate_coverage * 0.08,
        ),
    )
    if source["domain"] == candidate_row["domain"]:
        score += 0.04
    if source["kind"] == candidate_row["kind"]:
        score += 0.04
    score += 0.08 * _recency_score(candidate_row["created_at"], candidate_row["freshness_class"])
    score += min(0.06, math.log1p(candidate_row["activation_count"] or 0) * 0.015)
    if _is_expired(candidate_row["expires_at"]):
        score *= 0.70
    return min(1.0, score), relation


def _rank_candidates(
    conn: sqlite3.Connection,
    features: dict[str, Any],
    *,
    exclude_node_id: str | None = None,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    candidate_ids = _candidate_ids(
        conn,
        features,
        exclude_node_id=exclude_node_id,
        candidate_limit=candidate_limit,
        include_expired=include_expired,
    )
    if not candidate_ids:
        return []
    placeholders = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"""
        SELECT n.*, f.freshness_class, f.expires_at, f.activation_count
        FROM nodes n
        JOIN neural_node_features f ON f.node_id=n.id
        WHERE n.id IN ({placeholders})
        """,
        tuple(candidate_ids),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    terms_by_id = _term_sets(conn, candidate_ids)
    ranked: list[dict[str, Any]] = []
    for node_id in candidate_ids:
        row = by_id.get(node_id)
        if row is None:
            continue
        score, relation = _score_candidate(features, terms_by_id.get(node_id, {}), row)
        ranked.append({"node_id": node_id, "score": score, "relation": relation, "row": row})
    ranked.sort(key=lambda item: (item["score"], item["row"]["created_at"] or ""), reverse=True)
    return ranked


def index_node(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    host_id: str | None = None,
    origin_db: str | None = None,
    next_logical_clock: Callable[[], int] | None = None,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    link_limit: int = DEFAULT_LINK_LIMIT,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT node_id, domain, kind, goal_id, freshness_class, expires_at "
        "FROM neural_node_features WHERE node_id=?",
        (node_id,),
    ).fetchone()
    row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if row is None:
        raise ValueError(f"node {node_id} not found")
    features = extract_features(
        domain=row["domain"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        file_path=row["file_path"],
        goal_id=row["goal_id"],
        created_at=row["created_at"] or row["ts"],
    )
    if existing:
        now = _now_iso()
        metadata_changed = any(
            (
                existing["domain"] != features["domain"],
                existing["kind"] != features["kind"],
                existing["goal_id"] != features["goal_id"],
                existing["freshness_class"] != features["freshness_class"],
                existing["expires_at"] != features["expires_at"],
            )
        )
        if metadata_changed:
            conn.execute(
                """
                UPDATE neural_node_features
                SET domain=?, kind=?, goal_id=?, freshness_class=?, expires_at=?, indexed_at=?
                WHERE node_id=?
                """,
                (
                    features["domain"], features["kind"], features["goal_id"],
                    features["freshness_class"], features["expires_at"], now, node_id,
                ),
            )
        before_changes = conn.total_changes
        for term_type, values in features["terms"].items():
            conn.executemany(
                "INSERT OR IGNORE INTO neural_feature_terms (node_id, term_type, term) VALUES (?,?,?)",
                [(node_id, term_type, value) for value in values],
            )
        terms_added = conn.total_changes - before_changes
        refreshed = metadata_changed or bool(terms_added)
        return {
            "node_id": node_id,
            "indexed": False,
            "refreshed": refreshed,
            "metadata_changed": metadata_changed,
            "terms_added": terms_added,
            "links_created": 0,
            "reason": "features_refreshed" if refreshed else "already_indexed",
        }
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO neural_node_features (
            node_id, domain, kind, goal_id, freshness_class, expires_at, indexed_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            node_id, features["domain"], features["kind"], features["goal_id"],
            features["freshness_class"], features["expires_at"], now,
        ),
    )
    for term_type, values in features["terms"].items():
        conn.executemany(
            "INSERT OR IGNORE INTO neural_feature_terms (node_id, term_type, term) VALUES (?,?,?)",
            [(node_id, term_type, value) for value in values],
        )

    ranked = _rank_candidates(
        conn,
        features,
        exclude_node_id=node_id,
        candidate_limit=candidate_limit,
    )
    links: list[dict[str, Any]] = []
    for item in ranked:
        if item["score"] < 0.50 or len(links) >= max(0, link_limit):
            continue
        logical_clock = next_logical_clock() if next_logical_clock else None
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO edges (
                from_id, to_id, relation, weight, author,
                host_id, origin_db, logical_clock, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                node_id, item["node_id"], item["relation"], item["score"], "neural-link",
                host_id, origin_db, logical_clock, now,
            ),
        )
        if cursor.rowcount:
            conn.execute(
                """
                UPDATE neural_node_features
                SET activation_count=activation_count+1, last_activated_at=?
                WHERE node_id=?
                """,
                (now, item["node_id"]),
            )
            links.append(
                {"to_id": item["node_id"], "relation": item["relation"], "weight": round(item["score"], 4)}
            )
    return {
        "node_id": node_id,
        "indexed": True,
        "refreshed": False,
        "links_created": len(links),
        "links": links,
    }


def _compact_body(body: str | None, *, max_chars: int = 280) -> str:
    if not body:
        return ""
    try:
        value = json.loads(body)
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        text = str(body)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _infer_depth(query: str) -> int:
    return 4 if _DEEP_RECALL_RE.search(query or "") else 2


def _expand_associations(
    conn: sqlite3.Connection,
    direct: list[dict[str, Any]],
    *,
    max_depth: int,
    max_nodes: int,
) -> dict[str, dict[str, Any]]:
    activations: dict[str, dict[str, Any]] = {
        item["node_id"]: {"activation": item["score"], "hop": 0, "relation": item["relation"]}
        for item in direct
    }
    frontier = list(activations)
    for hop in range(1, max(0, max_depth) + 1):
        if not frontier or len(activations) >= max_nodes:
            break
        placeholders = ",".join("?" for _ in frontier)
        relation_placeholders = ",".join("?" for _ in NEURAL_RELATIONS)
        rows = conn.execute(
            f"""
            SELECT from_id, to_id, relation, weight
            FROM edges
            WHERE from_id IN ({placeholders})
              AND relation IN ({relation_placeholders})
            ORDER BY weight DESC
            LIMIT ?
            """,
            tuple(frontier) + NEURAL_RELATIONS + (max_nodes * 2,),
        ).fetchall()
        next_frontier: list[str] = []
        for edge in rows:
            parent = activations.get(edge["from_id"])
            if parent is None:
                continue
            activation = parent["activation"] * float(edge["weight"] or 0.0) * (0.72 ** hop)
            if activation < 0.18:
                continue
            current = activations.get(edge["to_id"])
            if current is None or activation > current["activation"]:
                activations[edge["to_id"]] = {
                    "activation": activation,
                    "hop": hop,
                    "relation": edge["relation"],
                }
                next_frontier.append(edge["to_id"])
                if len(activations) >= max_nodes:
                    break
        frontier = next_frontier
    return activations


def recall_query(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 6,
    max_chars: int = 1800,
    max_depth: int | None = None,
    candidate_mode: bool = False,
    include_expired: bool | None = None,
) -> dict[str, Any]:
    historical_recall = bool(_DEEP_RECALL_RE.search(query or ""))
    include_expired_effective = historical_recall if include_expired is None else include_expired
    features = extract_features(
        domain="query",
        kind="request",
        title=query,
        body=None,
        file_path=None,
        goal_id=None,
        created_at=_now_iso(),
    )
    direct = _rank_candidates(
        conn,
        features,
        candidate_limit=DEFAULT_CANDIDATE_LIMIT,
        include_expired=include_expired_effective,
    )
    direct_threshold = 0.18 if candidate_mode else 0.30
    direct_limit = 12 if candidate_mode else 8
    direct = [item for item in direct if item["score"] >= direct_threshold][:direct_limit]
    depth = _infer_depth(query) if max_depth is None else max(0, min(5, max_depth))
    activations = _expand_associations(
        conn,
        direct,
        max_depth=depth,
        max_nodes=36 if candidate_mode else 24,
    )
    if not activations:
        return {
            "query": query, "depth": depth, "candidate_mode": candidate_mode,
            "historical_recall": historical_recall,
            "include_expired": include_expired_effective,
            "items": [], "context": "", "chars": 0,
        }

    ids = list(activations)
    placeholders = ",".join("?" for _ in ids)
    rows_query = f"""
        SELECT n.*, f.freshness_class, f.expires_at
        FROM nodes n
        JOIN neural_node_features f ON f.node_id=n.id
        WHERE n.id IN ({placeholders})
    """
    row_params: tuple[Any, ...] = tuple(ids)
    if not include_expired_effective:
        rows_query += " AND (f.expires_at IS NULL OR f.expires_at >= ?)"
        row_params += (_now_iso(),)
    rows = conn.execute(rows_query, row_params).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        info = activations[row["id"]]
        activation = float(info["activation"])
        activation *= 0.85 + 0.15 * _recency_score(row["created_at"], row["freshness_class"])
        expired = _is_expired(row["expires_at"])
        items.append(
            {
                "id": row["id"],
                "title": row["title"] or f"{row['domain']}/{row['kind']}",
                "summary": _compact_body(row["body"]),
                "domain": row["domain"],
                "kind": row["kind"],
                "goal_id": row["goal_id"],
                "created_at": row["created_at"],
                "freshness_class": row["freshness_class"],
                "expires_at": row["expires_at"],
                "freshness_status": "expired" if expired else "current",
                "activation": round(activation, 4),
                "hop": info["hop"],
                "relation": info["relation"],
            }
        )
    items.sort(key=lambda item: (item["activation"], item["created_at"] or ""), reverse=True)
    items = items[: max(0, limit)]

    header = (
        "[Timeline NeuralLink 후보 패킷: 답변 AI가 의미·시간·현재 요청으로 재선택]"
        if candidate_mode
        else "[Timeline NeuralLink: 관련 과거 맥락]"
    )
    lines = [header]
    if include_expired_effective:
        lines.append(
            "[STALE/EXPIRED 후보는 당시 증거일 뿐 현재값이 아니므로 사용 전 재검증]"
        )
    kept: list[dict[str, Any]] = []
    for item in items:
        line = (
            f"- {item['title']} (hop={item['hop']}, relation={item['relation']}, "
            f"activation={item['activation']}, at={item['created_at']}"
        )
        if item["freshness_status"] == "expired":
            line += f", freshness=STALE/EXPIRED, expired_at={item['expires_at']}"
        line += ")"
        if item["summary"]:
            line += f": {item['summary']}"
        candidate = "\n".join([*lines, line])
        if len(candidate) > max_chars:
            break
        lines.append(line)
        kept.append(item)
    context = "\n".join(lines) if kept else ""
    return {
        "query": query,
        "depth": depth,
        "candidate_mode": candidate_mode,
        "historical_recall": historical_recall,
        "include_expired": include_expired_effective,
        "items": kept,
        "context": context,
        "chars": len(context),
    }


def recall_from_node_ids(
    conn: sqlite3.Connection,
    seed_ids: Iterable[str],
    *,
    limit: int = 8,
    max_depth: int = 2,
) -> list[dict[str, Any]]:
    seeds = list(dict.fromkeys(seed_ids))
    if not seeds:
        return []
    direct = [
        {"node_id": node_id, "score": 1.0, "relation": "seed"}
        for node_id in seeds[:10]
    ]
    activations = _expand_associations(conn, direct, max_depth=max_depth, max_nodes=24)
    for node_id in seeds:
        activations.pop(node_id, None)
    if not activations:
        return []
    ids = list(activations)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT n.*, f.expires_at
        FROM nodes n JOIN neural_node_features f ON f.node_id=n.id
        WHERE n.id IN ({placeholders})
          AND (f.expires_at IS NULL OR f.expires_at >= ?)
        """,
        tuple(ids) + (_now_iso(),),
    ).fetchall()
    result = []
    for row in rows:
        info = activations[row["id"]]
        result.append(
            {
                "id": row["id"],
                "domain": row["domain"],
                "kind": row["kind"],
                "title": row["title"],
                "body": row["body"],
                "goal_id": row["goal_id"],
                "created_at": row["created_at"],
                "activation": round(info["activation"], 4),
                "hop": info["hop"],
                "relation": info["relation"],
            }
        )
    result.sort(key=lambda item: (item["activation"], item["created_at"] or ""), reverse=True)
    return result[: max(0, limit)]
