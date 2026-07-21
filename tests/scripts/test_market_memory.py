from __future__ import annotations

from types import SimpleNamespace

from scripts import market_memory


def test_market_memory_add_and_lexical_search(tmp_path):
    db = tmp_path / "market_memory.jsonl"
    first = market_memory.add_entry(
        SimpleNamespace(
            db=db,
            title="Earnings timing",
            body="Separate pre-market from after-hours timestamps.",
            tag=["earnings", "timestamp"],
            source=["https://example.com/official"],
            confidence=0.8,
        )
    )
    market_memory.add_entry(
        SimpleNamespace(
            db=db,
            title="Unrelated lesson",
            body="Check currency units.",
            tag=["units"],
            source=[],
            confidence=0.7,
        )
    )

    result = market_memory.search(
        SimpleNamespace(db=db, query="earnings timestamp", limit=5)
    )

    assert result["candidate_count"] == 1
    assert result["items"][0]["id"] == first["id"]
    assert first["id"].startswith("mm_")
