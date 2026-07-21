#!/usr/bin/env python3
"""Optional, operator-owned JSONL memory for the public market Role Shell."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "hermes.market-memory.entry.v1"


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[\w.-]{2,}", text, flags=re.UNICODE)
    }


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.stat().st_size > 20_000_000:
        raise RuntimeError("market memory exceeds the 20 MB local safety limit")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSONL at line {number}: {exc}") from exc
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            raise RuntimeError(f"invalid market-memory entry at line {number}")
        rows.append(row)
    return rows


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def add_entry(args: argparse.Namespace) -> dict[str, Any]:
    row = {
        "schema": SCHEMA,
        "id": f"mm_{uuid.uuid4().hex[:16]}",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "title": args.title.strip(),
        "body": args.body.strip(),
        "tags": sorted(set(args.tag or [])),
        "sources": sorted(set(args.source or [])),
        "confidence": args.confidence,
    }
    if not row["title"] or not row["body"]:
        raise ValueError("title and body must not be empty")
    _append(args.db, row)
    return row


def search(args: argparse.Namespace) -> dict[str, Any]:
    query_tokens = _tokens(args.query)
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for row in _read(args.db):
        haystack = " ".join(
            [
                str(row.get("title") or ""),
                str(row.get("body") or ""),
                " ".join(str(value) for value in row.get("tags") or []),
            ]
        )
        score = len(query_tokens & _tokens(haystack))
        if score:
            candidates.append((score, str(row.get("created_at_utc") or ""), row))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    items = [row for _score, _stamp, row in candidates[: max(1, args.limit)]]
    return {
        "schema": "hermes.market-memory.search.v1",
        "query": args.query,
        "candidate_count": len(candidates),
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    add = commands.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--body", required=True)
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--source", action="append", default=[])
    add.add_argument("--confidence", type=float, default=0.7)
    find = commands.add_parser("search")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    args.db = args.db.expanduser().resolve()
    if args.command == "init":
        args.db.parent.mkdir(parents=True, exist_ok=True)
        args.db.touch(mode=0o600, exist_ok=True)
        result = {"status": "initialized", "db": str(args.db), "entries": len(_read(args.db))}
    elif args.command == "add":
        result = add_entry(args)
    else:
        result = search(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
