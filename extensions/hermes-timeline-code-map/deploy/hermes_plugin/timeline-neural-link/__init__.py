from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _ensure_timeline_import_path() -> None:
    configured = os.environ.get("TIMELINE_CODE_MAP_SOURCE", "")
    candidates = [Path(configured).expanduser() if configured else None]
    for candidate in candidates:
        if candidate and candidate.is_dir():
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
            return


def _db_path() -> Path:
    configured = os.environ.get("TIMELINE_CODE_MAP_DB_PATH", "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes" / "timeline_code_map" / "graph.db"


def _recall(user_message: str) -> str:
    _ensure_timeline_import_path()
    from hermes_timeline_code_map.neural_links import recall_query

    path = _db_path()
    if not path.is_file():
        return ""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.25)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=250")
        required = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='neural_node_features'"
        ).fetchone()
        if required is None:
            return ""
        result = recall_query(
            conn,
            user_message,
            limit=10,
            max_chars=2600,
            candidate_mode=True,
        )
        return str(result.get("context") or "")
    finally:
        conn.close()


def on_pre_llm_call(*, user_message: str = "", **_: Any) -> dict[str, str] | None:
    message = str(user_message or "").strip()
    if len(message) < 3:
        return None
    try:
        context = _recall(message)
    except (OSError, sqlite3.Error, ImportError, ValueError) as exc:
        logger.warning("Timeline NeuralLink recall skipped: %s", exc)
        return None
    if not context:
        return None
    return {
        "context": (
            context
            + "\n위 항목은 결정론적 최종 답이 아니라 원본 Timeline 후보다. 답변 AI가 의미·유사어·현재 요청으로 "
            + "직접 재랭킹하여 관련된 계약·실패·결정만 사용한다. 후보가 부족하면 "
            + "recall_neural_context_tool에 유사어·상위개념을 포함한 probe를 만들어 다시 조회한다. "
            + "시간 민감 사실은 현재 데이터로 재검증한다."
        )
    }


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
