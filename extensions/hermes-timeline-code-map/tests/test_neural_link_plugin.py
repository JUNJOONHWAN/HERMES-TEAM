from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hermes_timeline_code_map.store import TimelineCodeMap


PLUGIN_PATH = (
    Path(__file__).parents[1]
    / "deploy"
    / "hermes_plugin"
    / "timeline-neural-link"
    / "__init__.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("timeline_neural_link_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TimelineNeuralLinkPluginTests(unittest.TestCase):
    def test_hook_reads_compact_context_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "graph.db"
            store = TimelineCodeMap(str(db_path))
            store.record(
                domain="memory",
                kind="contract",
                title="Hermes Telegram worker contract",
                body={"workflow": "telegram-worker-contract", "rule": "workers load Timeline"},
                goal_id="contract-old",
            )
            before = db_path.stat().st_mtime_ns
            original = os.environ.get("TIMELINE_CODE_MAP_DB_PATH")
            os.environ["TIMELINE_CODE_MAP_DB_PATH"] = str(db_path)
            try:
                result = _load_plugin().on_pre_llm_call(
                    user_message="전에 Hermes Telegram worker contract가 뭐였지?"
                )
            finally:
                if original is None:
                    os.environ.pop("TIMELINE_CODE_MAP_DB_PATH", None)
                else:
                    os.environ["TIMELINE_CODE_MAP_DB_PATH"] = original
            after = db_path.stat().st_mtime_ns

            self.assertIsNotNone(result)
            self.assertIn("Hermes Telegram worker contract", result["context"])
            self.assertIn("답변 AI", result["context"])
            self.assertLessEqual(len(result["context"]), 3000)
            self.assertEqual(before, after)

    def test_hook_is_fail_open_when_database_is_missing(self) -> None:
        original = os.environ.get("TIMELINE_CODE_MAP_DB_PATH")
        os.environ["TIMELINE_CODE_MAP_DB_PATH"] = "/definitely/missing/timeline.db"
        try:
            result = _load_plugin().on_pre_llm_call(user_message="과거 기억을 찾아줘")
        finally:
            if original is None:
                os.environ.pop("TIMELINE_CODE_MAP_DB_PATH", None)
            else:
                os.environ["TIMELINE_CODE_MAP_DB_PATH"] = original
        self.assertIsNone(result)

    def test_hook_labels_expired_evidence_for_explicit_historical_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "graph.db"
            store = TimelineCodeMap(str(db_path))
            node_id = store.record(
                domain="market",
                kind="quote",
                title="VIX historical spike quote",
                body={"symbol": "VIX", "quote": 31.25},
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE neural_node_features SET expires_at='2000-01-01T00:00:00.000Z' "
                    "WHERE node_id=?",
                    (node_id,),
                )

            before = db_path.stat().st_mtime_ns
            original = os.environ.get("TIMELINE_CODE_MAP_DB_PATH")
            os.environ["TIMELINE_CODE_MAP_DB_PATH"] = str(db_path)
            try:
                result = _load_plugin().on_pre_llm_call(
                    user_message="예전에 VIX historical spike quote가 얼마였지?"
                )
            finally:
                if original is None:
                    os.environ.pop("TIMELINE_CODE_MAP_DB_PATH", None)
                else:
                    os.environ["TIMELINE_CODE_MAP_DB_PATH"] = original

            self.assertIsNotNone(result)
            self.assertIn("VIX historical spike quote", result["context"])
            self.assertIn("STALE/EXPIRED", result["context"])
            self.assertEqual(before, db_path.stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
