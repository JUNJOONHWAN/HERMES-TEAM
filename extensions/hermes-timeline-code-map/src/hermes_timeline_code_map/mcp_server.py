from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .store import DEFAULT_DB_PATH, TimelineCodeMap
from .roadmap import RoadmapStore, verify_schedule_contract

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
    FastMCP = None
    _MCP_IMPORT_ERROR = exc
else:
    _MCP_IMPORT_ERROR = None


def _db_path() -> str:
    return os.environ.get("TIMELINE_CODE_MAP_DB_PATH", DEFAULT_DB_PATH)


def _parse_body(value: str) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def create_server(db_path: str | None = None):
    if FastMCP is None:
        raise RuntimeError(
            "mcp is not installed. Run ./venv/bin/pip install 'mcp>=1.10.0' before starting the server."
        ) from _MCP_IMPORT_ERROR

    resolved_db_path = str(Path(db_path or _db_path()).expanduser())
    store = TimelineCodeMap(resolved_db_path)
    roadmap = RoadmapStore(resolved_db_path, timeline=store)
    mcp = FastMCP("hermes-timeline-code-map")

    @mcp.tool()
    def record_node(
        domain: str,
        kind: str,
        title: str = "",
        body: str = "",
        file_path: str = "",
        line_start: int = 0,
        author: str = "unknown",
        confidence: float = 1.0,
        goal_id: str = "",
        prev_id: str = "",
    ) -> dict:
        """그래프에 새 노드를 기록한다. 가능하면 body에 memory_descriptor의 summary/concepts/aliases/temporal_scope를 포함한다."""
        node_id = store.record(
            domain=domain,
            kind=kind,
            title=title or None,
            body=_parse_body(body),
            file_path=file_path or None,
            line_start=line_start or None,
            author=author,
            confidence=confidence,
            goal_id=goal_id or None,
            prev_id=prev_id or None,
        )
        return {"node_id": node_id}

    @mcp.tool()
    def link_nodes(from_id: str, to_id: str, relation: str, weight: float = 1.0, author: str = "") -> dict:
        """두 노드를 지정한 relation으로 연결한다."""
        store.link(from_id, to_id, relation, weight=weight, author=author or None)
        return {"status": "linked", "from_id": from_id, "to_id": to_id, "relation": relation}

    @mcp.tool()
    def search_nodes(
        domain: str = "",
        kind: str = "",
        text: str = "",
        since: str = "",
        until: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """도메인, 종류, 키워드로 노드를 검색한다."""
        return store.search(
            domain=domain or None,
            kind=kind or None,
            text=text or None,
            since=since or None,
            until=until or None,
            limit=limit,
        )

    @mcp.tool()
    def verify_chain_tool(node_id: str) -> dict:
        """특정 노드부터 genesis까지 무결성을 검증한다."""
        return store.verify_chain(node_id)

    @mcp.tool()
    def verify_all_tool() -> dict:
        """그래프 전체 노드의 해시 체인을 점검한다."""
        return store.verify_all()

    @mcp.tool()
    def get_context_tool(goal_id: str, depth: int = 2, recent_limit: int = 10) -> dict:
        """goal_id 기준 최근 실행 맥락과 관련 판단을 반환한다."""
        return store.get_context(goal_id, depth=depth, recent_limit=recent_limit)

    @mcp.tool()
    def recall_neural_context_tool(
        query: str,
        limit: int = 6,
        max_chars: int = 1800,
        max_depth: int = 0,
        candidate_mode: bool = False,
        include_expired: bool | None = None,
    ) -> dict:
        """다단계 기억 후보를 조회한다. 과거 회상 단서는 만료 후보를 STALE/EXPIRED로 표시한다."""
        return store.recall_neural_context(
            query,
            limit=limit,
            max_chars=max_chars,
            max_depth=max_depth or None,
            candidate_mode=candidate_mode,
            include_expired=include_expired,
        )

    @mcp.tool()
    def neural_link_status_tool() -> dict:
        """Timeline 노드별 증분 NeuralLink 인덱싱 상태를 반환한다."""
        return store.neural_link_status()

    @mcp.tool()
    def backfill_neural_links_tool(limit: int = 0, batch_size: int = 100) -> dict:
        """과거 미인덱스 노드를 작은 배치로 1회 보강한다."""
        return store.backfill_neural_links(limit=limit, batch_size=batch_size)

    @mcp.tool()
    def load_session_tool(since: str = "", goal_id: str = "") -> dict:
        """Claude나 Codex CLI가 검토할 세션 서브그래프를 로드한다."""
        return store.load_session(since=since or None, goal_id=goal_id or None)

    @mcp.tool()
    def trace_audit_tool(reasoning_node_id: str) -> dict:
        """결론 노드의 증거 체인을 양방향으로 추적한다."""
        return store.trace_audit(reasoning_node_id)

    @mcp.tool()
    def snapshot_tool(goal_id: str) -> dict:
        """goal_id 기준 그래프 스냅샷을 만든다."""
        return store.snapshot(goal_id)

    @mcp.tool()
    def auto_ingest_output_tool(
        file_path: str,
        source_action_id: str,
        reasoning_id: str = "",
        author: str = "hermes",
    ) -> dict:
        """파일 출력 직후 output 노드를 자동으로 생성하고 연결한다."""
        node_id = store.auto_ingest_output(
            file_path,
            source_action_id,
            reasoning_id=reasoning_id or None,
            author=author,
        )
        return {"node_id": node_id}

    @mcp.tool()
    def export_graph_tool(goal_id: str = "") -> dict:
        """전체 그래프 또는 goal_id 스냅샷을 내보낸다."""
        return store.export_graph(goal_id=goal_id or None)

    @mcp.tool()
    def export_delta_tool(
        output_path: str = "",
        since: str = "",
        host_id: str = "",
        sync_batch_id: str = "",
    ) -> dict:
        """append-only JSONL sync event delta를 내보낸다."""
        return store.export_delta(
            output_path or None,
            since=since,
            host_id=host_id or None,
            sync_batch_id=sync_batch_id or None,
        )

    @mcp.tool()
    def import_delta_tool(
        input_path: str,
        peer_id: str = "",
        merge_policy: str = "append_only",
    ) -> dict:
        """append-only JSONL sync event delta를 병합한다."""
        return store.import_delta(input_path, peer_id=peer_id, merge_policy=merge_policy)

    @mcp.tool()
    def sync_status_tool() -> dict:
        """sync metadata, imported event, cursor 상태를 반환한다."""
        return store.sync_status()

    @mcp.tool()
    def append_roadmap_event_tool(
        goal_id: str,
        entity_id: str,
        entity_type: str,
        event_type: str,
        expected_version: int,
        payload: str = "{}",
        actor: str = "{}",
        event_id: str = "",
        occurred_at_utc: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        policy_bundle_hash: str = "",
        author: str = "hermes-roadmap",
    ) -> dict:
        """검증된 typed Roadmap event를 idempotent하게 기록한다."""
        parsed_payload = _parse_body(payload)
        parsed_actor = _parse_body(actor)
        if not isinstance(parsed_payload, dict) or not isinstance(parsed_actor, dict):
            raise ValueError("payload and actor must be JSON objects")
        return roadmap.append_event(
            goal_id=goal_id,
            entity_id=entity_id,
            entity_type=entity_type,
            event_type=event_type,
            expected_version=expected_version,
            payload=parsed_payload,
            actor=parsed_actor,
            event_id=event_id or None,
            occurred_at_utc=occurred_at_utc or None,
            correlation_id=correlation_id or None,
            causation_id=causation_id or None,
            policy_bundle_hash=policy_bundle_hash or None,
            author=author,
        )

    @mcp.tool()
    def get_roadmap_tool(
        goal_id: str,
        entity_type: str = "",
        state: str = "",
        limit: int = 500,
    ) -> dict:
        """goal의 현재 Roadmap projection을 조회한다."""
        return roadmap.get_roadmap(
            goal_id,
            entity_type=entity_type or None,
            state=state or None,
            limit=limit,
        )

    @mcp.tool()
    def get_task_history_tool(task_id: str) -> dict:
        """한 task의 전체 event와 Timeline node를 조회한다."""
        return roadmap.get_task_history(task_id)

    @mcp.tool()
    def rebuild_roadmap_projection_tool(goal_id: str = "") -> dict:
        """Timeline event node를 replay하여 Roadmap projection을 재구축한다."""
        return roadmap.rebuild_projection(goal_id=goal_id or None)

    @mcp.tool()
    def verify_goal_contract_tool(goal_id: str) -> dict:
        """Roadmap 업무 계약과 Timeline hash 무결성을 함께 검증한다."""
        return roadmap.verify_goal_contract(goal_id)

    @mcp.tool()
    def verify_schedule_contract_tool(payload: str) -> dict:
        """KST intent와 UTC-stored RRULE 일치 여부를 검증한다."""
        parsed = _parse_body(payload)
        if not isinstance(parsed, dict):
            raise ValueError("payload must be a JSON object")
        return verify_schedule_contract(parsed)

    @mcp.tool()
    def roadmap_sync_status_tool() -> dict:
        """Roadmap event commit 상태와 projection watermark를 반환한다."""
        return roadmap.sync_status()

    @mcp.tool()
    def index_code_repository_tool(
        repo_root: str,
        include_artifacts: bool = False,
        max_file_bytes: int = 512000,
        max_files: int = 20000,
        author: str = "hermes",
        record_summary: bool = False,
        goal_id: str = "",
    ) -> dict:
        """repo 파일/심볼/관계 인덱스를 최신 hot index로 만든다."""
        return store.index_repository(
            repo_root,
            include_artifacts=include_artifacts,
            max_file_bytes=max_file_bytes,
            max_files=max_files,
            author=author,
            record_summary=record_summary,
            goal_id=goal_id or None,
        )

    @mcp.tool()
    def query_code_slice_tool(
        repo_root: str,
        query: str,
        limit: int = 12,
        store_slice: bool = True,
        goal_id: str = "",
        author: str = "hermes",
        rebuild_if_missing: bool = False,
    ) -> dict:
        """Codex code-map처럼 작업 query에 맞는 작은 코드 slice를 반환한다."""
        return store.query_code_slice(
            repo_root,
            query,
            limit=limit,
            store_slice=store_slice,
            goal_id=goal_id or None,
            author=author,
            rebuild_if_missing=rebuild_if_missing,
        )

    @mcp.tool()
    def load_code_slice_tool(slice_id: str) -> dict:
        """저장된 code slice를 slice_id로 다시 불러온다."""
        result = store.load_code_slice(slice_id)
        return result if result is not None else {"error": "slice not found", "slice_id": slice_id}

    @mcp.tool()
    def list_code_indexes_tool() -> list[dict]:
        """현재 활성화된 repo별 hot code index 목록을 반환한다."""
        return store.list_code_indexes()

    @mcp.tool()
    def maintain_code_map_tool(
        max_slices_per_repo: int = 50,
        max_slice_age_days: int = 30,
        min_slices_per_repo: int = 5,
        prune_inactive_runs: bool = True,
        vacuum: bool = False,
        backup: bool = True,
    ) -> dict:
        """코드맵 hot index/slice 유지보수와 DB 무결성 검사를 수행한다."""
        return store.maintain_code_map(
            max_slices_per_repo=max_slices_per_repo,
            max_slice_age_days=max_slice_age_days,
            min_slices_per_repo=min_slices_per_repo,
            prune_inactive_runs=prune_inactive_runs,
            vacuum=vacuum,
            backup=backup,
        )

    return mcp


def main() -> None:
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
