from __future__ import annotations

import argparse
import json
from typing import Any

from .store import TimelineCodeMap


def _parse_body(value: str) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and mutate the shared timeline/code map.")
    parser.add_argument("--db-path", default=None, help="SQLite DB path. Defaults to TIMELINE_CODE_MAP_DB_PATH contract.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Create a node.")
    record.add_argument("domain")
    record.add_argument("kind")
    record.add_argument("title")
    record.add_argument("--body", default="")
    record.add_argument("--file-path", default="")
    record.add_argument("--line-start", type=int, default=0)
    record.add_argument("--author", default="unknown")
    record.add_argument("--confidence", type=float, default=1.0)
    record.add_argument("--goal-id", default="")
    record.add_argument("--prev-id", default="")

    link = subparsers.add_parser("link", help="Create or replace an edge.")
    link.add_argument("from_id")
    link.add_argument("to_id")
    link.add_argument("relation")
    link.add_argument("--weight", type=float, default=1.0)
    link.add_argument("--author", default="")

    search = subparsers.add_parser("search", help="Search nodes.")
    search.add_argument("--domain", default="")
    search.add_argument("--kind", default="")
    search.add_argument("--text", default="")
    search.add_argument("--since", default="")
    search.add_argument("--until", default="")
    search.add_argument("--limit", type=int, default=20)

    verify = subparsers.add_parser("verify", help="Verify one node or the whole graph.")
    verify.add_argument("node_id", nargs="?")

    context = subparsers.add_parser("context", help="Load Hermes goal context.")
    context.add_argument("goal_id")
    context.add_argument("--depth", type=int, default=2)
    context.add_argument("--recent-limit", type=int, default=10)

    session = subparsers.add_parser("session", help="Load a session subgraph.")
    session.add_argument("--since", default="")
    session.add_argument("--goal-id", default="")

    audit = subparsers.add_parser("audit", help="Trace a reasoning node audit chain.")
    audit.add_argument("reasoning_node_id")

    snapshot = subparsers.add_parser("snapshot", help="Snapshot a goal graph.")
    snapshot.add_argument("goal_id")

    export = subparsers.add_parser("export", help="Export all nodes/edges or one goal snapshot.")
    export.add_argument("--goal-id", default="")

    export_delta = subparsers.add_parser("export-delta", help="Export append-only sync events as JSONL.")
    export_delta.add_argument("output_path")
    export_delta.add_argument("--since", default="")
    export_delta.add_argument("--host-id", default="")
    export_delta.add_argument("--sync-batch-id", default="")

    import_delta = subparsers.add_parser("import-delta", help="Import append-only sync events from JSONL.")
    import_delta.add_argument("input_path")
    import_delta.add_argument("--peer-id", default="")
    import_delta.add_argument("--merge-policy", default="append_only")

    subparsers.add_parser("sync-status", help="Show sync metadata, imported events, and cursors.")

    ingest = subparsers.add_parser("auto-ingest-output", help="Create an output node and attach it to an action.")
    ingest.add_argument("file_path")
    ingest.add_argument("source_action_id")
    ingest.add_argument("--reasoning-id", default="")
    ingest.add_argument("--author", default="hermes")

    index_code = subparsers.add_parser("index-code", help="Build or refresh a repo code index.")
    index_code.add_argument("repo_root")
    index_code.add_argument("--include-artifacts", action="store_true")
    index_code.add_argument("--max-file-bytes", type=int, default=512000)
    index_code.add_argument("--max-files", type=int, default=20000)
    index_code.add_argument("--author", default="hermes")
    index_code.add_argument("--record-summary", action="store_true")
    index_code.add_argument("--goal-id", default="")

    query_slice = subparsers.add_parser("query-slice", help="Return a Codex-style code slice.")
    query_slice.add_argument("repo_root")
    query_slice.add_argument("query")
    query_slice.add_argument("--limit", type=int, default=12)
    query_slice.add_argument("--no-store-slice", action="store_true")
    query_slice.add_argument("--goal-id", default="")
    query_slice.add_argument("--author", default="hermes")
    query_slice.add_argument("--rebuild-if-missing", action="store_true")

    load_slice = subparsers.add_parser("load-slice", help="Load a stored code slice.")
    load_slice.add_argument("slice_id")

    subparsers.add_parser("list-code-indexes", help="List active code indexes.")

    maintain = subparsers.add_parser("maintain", help="Run code-map maintenance.")
    maintain.add_argument("--max-slices-per-repo", type=int, default=50)
    maintain.add_argument("--max-slice-age-days", type=int, default=30)
    maintain.add_argument("--min-slices-per-repo", type=int, default=5)
    maintain.add_argument("--no-prune-inactive-runs", action="store_true")
    maintain.add_argument("--vacuum", action="store_true")
    maintain.add_argument("--no-backup", action="store_true")

    recall = subparsers.add_parser(
        "recall-neural", help="Recall a bounded NeuralLink context packet."
    )
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=8)
    recall.add_argument("--max-chars", type=int, default=2600)
    recall.add_argument("--max-depth", type=int, default=0, help="0 lets historical cues choose the depth")
    recall.add_argument("--candidate-mode", action="store_true")
    expiry = recall.add_mutually_exclusive_group()
    expiry.add_argument("--include-expired", dest="include_expired", action="store_true")
    expiry.add_argument("--exclude-expired", dest="include_expired", action="store_false")
    recall.set_defaults(include_expired=None)
    subparsers.add_parser("neural-status", help="Show incremental NeuralLink index status.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = TimelineCodeMap(args.db_path) if args.db_path else TimelineCodeMap()

    if args.command == "record":
        node_id = store.record(
            domain=args.domain,
            kind=args.kind,
            title=args.title,
            body=_parse_body(args.body),
            file_path=args.file_path or None,
            line_start=args.line_start or None,
            author=args.author,
            confidence=args.confidence,
            goal_id=args.goal_id or None,
            prev_id=args.prev_id or None,
        )
        print(json.dumps({"node_id": node_id}, ensure_ascii=False))
        return 0

    if args.command == "link":
        store.link(
            args.from_id,
            args.to_id,
            args.relation,
            weight=args.weight,
            author=args.author or None,
        )
        print(json.dumps({"status": "ok"}, ensure_ascii=False))
        return 0

    if args.command == "search":
        print(
            json.dumps(
                store.search(
                    domain=args.domain or None,
                    kind=args.kind or None,
                    text=args.text or None,
                    since=args.since or None,
                    until=args.until or None,
                    limit=args.limit,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "verify":
        result = store.verify_chain(args.node_id) if args.node_id else store.verify_all()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "context":
        print(
            json.dumps(
                store.get_context(args.goal_id, depth=args.depth, recent_limit=args.recent_limit),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "recall-neural":
        print(
            json.dumps(
                store.recall_neural_context(
                    args.query,
                    limit=args.limit,
                    max_chars=args.max_chars,
                    max_depth=args.max_depth or None,
                    candidate_mode=args.candidate_mode,
                    include_expired=args.include_expired,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "neural-status":
        print(json.dumps(store.neural_link_status(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "session":
        print(
            json.dumps(
                store.load_session(since=args.since or None, goal_id=args.goal_id or None),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "audit":
        print(json.dumps(store.trace_audit(args.reasoning_node_id), ensure_ascii=False, indent=2))
        return 0

    if args.command == "snapshot":
        print(json.dumps(store.snapshot(args.goal_id), ensure_ascii=False, indent=2))
        return 0

    if args.command == "export":
        print(json.dumps(store.export_graph(goal_id=args.goal_id or None), ensure_ascii=False, indent=2))
        return 0

    if args.command == "export-delta":
        print(
            json.dumps(
                store.export_delta(
                    args.output_path,
                    since=args.since,
                    host_id=args.host_id or None,
                    sync_batch_id=args.sync_batch_id or None,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "import-delta":
        print(
            json.dumps(
                store.import_delta(
                    args.input_path,
                    peer_id=args.peer_id,
                    merge_policy=args.merge_policy,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "sync-status":
        print(json.dumps(store.sync_status(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "auto-ingest-output":
        node_id = store.auto_ingest_output(
            args.file_path,
            args.source_action_id,
            reasoning_id=args.reasoning_id or None,
            author=args.author,
        )
        print(node_id)
        return 0

    if args.command == "index-code":
        print(
            json.dumps(
                store.index_repository(
                    args.repo_root,
                    include_artifacts=args.include_artifacts,
                    max_file_bytes=args.max_file_bytes,
                    max_files=args.max_files,
                    author=args.author,
                    record_summary=args.record_summary,
                    goal_id=args.goal_id or None,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "query-slice":
        print(
            json.dumps(
                store.query_code_slice(
                    args.repo_root,
                    args.query,
                    limit=args.limit,
                    store_slice=not args.no_store_slice,
                    goal_id=args.goal_id or None,
                    author=args.author,
                    rebuild_if_missing=args.rebuild_if_missing,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "load-slice":
        print(json.dumps(store.load_code_slice(args.slice_id), ensure_ascii=False, indent=2))
        return 0

    if args.command == "list-code-indexes":
        print(json.dumps(store.list_code_indexes(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "maintain":
        print(
            json.dumps(
                store.maintain_code_map(
                    max_slices_per_repo=args.max_slices_per_repo,
                    max_slice_age_days=args.max_slice_age_days,
                    min_slices_per_repo=args.min_slices_per_repo,
                    prune_inactive_runs=not args.no_prune_inactive_runs,
                    vacuum=args.vacuum,
                    backup=not args.no_backup,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
