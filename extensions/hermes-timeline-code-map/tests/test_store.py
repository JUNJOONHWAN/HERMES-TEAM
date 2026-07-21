from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from hermes_timeline_code_map.store import TimelineCodeMap, _code_index_health


class TimelineCodeMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "graph.db"
        self.store = TimelineCodeMap(str(self.db_path))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_smoke_context_search_audit_and_snapshot(self) -> None:
        goal = "goal-refactor-hermes-agent"
        a1 = self.store.record(
            domain="timeline",
            kind="action",
            title="run pytest",
            body={"exit_code": 1},
            author="hermes",
            goal_id=goal,
        )
        a2 = self.store.record(
            domain="timeline",
            kind="action",
            title="edit null guard",
            body={"file": "worker.py"},
            author="hermes",
            goal_id=goal,
        )
        code = self.store.record(
            domain="code",
            kind="symbol",
            title="validate_task_payload",
            file_path="worker.py",
            line_start=42,
            author="hermes",
        )
        self.store.link(a2, code, "calls", author="hermes")
        reasoning = self.store.record(
            domain="reasoning",
            kind="conclusion",
            title="missing null guard caused the failure",
            body={"confidence": 0.91},
            author="claude",
        )
        self.store.link(reasoning, a1, "concludes_from", author="claude")
        self.store.link(reasoning, a2, "concludes_from", author="claude")
        self.store.link(reasoning, code, "supports", author="claude")
        output = self.store.auto_ingest_output(
            "/tmp/fix_summary.md",
            a2,
            reasoning_id=reasoning,
            author="hermes",
        )

        context = self.store.get_context(goal)
        self.assertEqual(len(context["recent_actions"]), 2)
        self.assertEqual(len(context["prior_judgments"]), 1)
        self.assertEqual(len(context["code_context"]), 1)

        hits = self.store.search(text="pytest")
        self.assertGreaterEqual(len(hits), 1)

        audit = self.store.trace_audit(reasoning)
        titles = {node["title"] for node in audit["evidence_chain"]}
        self.assertIn("run pytest", titles)
        self.assertIn("edit null guard", titles)
        self.assertIn("validate_task_payload", titles)
        self.assertIn("fix_summary.md", titles)

        snapshot = self.store.snapshot(goal)
        snapshot_titles = {node["title"] for node in snapshot["nodes"]}
        self.assertIn("run pytest", snapshot_titles)
        self.assertIn("edit null guard", snapshot_titles)
        self.assertIn("fix_summary.md", snapshot_titles)

        session = self.store.load_session()
        self.assertEqual(session["node_count"], 5)
        self.assertGreaterEqual(session["edge_count"], 6)

        node = self.store.get_node(output)
        self.assertEqual(node["kind"], "report")
        self.assertEqual(node["file_path"], "/tmp/fix_summary.md")

        verify = self.store.verify_all()
        self.assertEqual(verify["invalid_count"], 0)

    def test_verify_chain_detects_tampering(self) -> None:
        goal = "goal-tamper-check"
        first = self.store.record(
            domain="timeline",
            kind="action",
            title="first step",
            author="hermes",
            goal_id=goal,
        )
        second = self.store.record(
            domain="timeline",
            kind="action",
            title="second step",
            author="hermes",
            goal_id=goal,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE nodes SET title='tampered step' WHERE id=?", (first,))
            conn.commit()
        finally:
            conn.close()

        result = self.store.verify_chain(second)
        self.assertFalse(result["valid"])
        self.assertIn("hash mismatch", result["reason"])

    def test_concurrent_goal_writes_remain_one_valid_chain(self) -> None:
        goal = "goal-concurrent-writers"

        def write_node(idx: int) -> str:
            return self.store.record(
                domain="timeline",
                kind="action",
                title=f"concurrent step {idx}",
                author="test",
                goal_id=goal,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            node_ids = list(pool.map(write_node, range(24)))

        self.assertEqual(len(set(node_ids)), 24)
        verify = self.store.verify_all()
        self.assertEqual(verify["invalid_count"], 0)

        snapshot = self.store.snapshot(goal)
        goal_nodes = [node for node in snapshot["nodes"] if node["goal_id"] == goal]
        self.assertEqual(len(goal_nodes), 24)
        prev_ids = {node["prev_id"] for node in goal_nodes if node["prev_id"]}
        self.assertEqual(len(prev_ids), 23)

    def test_code_index_query_slice_and_load(self) -> None:
        repo = Path(self.tmpdir.name) / "sample_repo"
        repo.mkdir()
        worker = repo / "worker.py"
        worker.write_text(
            "\n".join(
                [
                    "import json",
                    "",
                    "class TaskRunner:",
                    "    def validate_task_payload(self, payload):",
                    "        if payload is None:",
                    "            raise ValueError('missing payload')",
                    "        return json.dumps(payload)",
                    "",
                    "def run_worker(payload):",
                    "    return TaskRunner().validate_task_payload(payload)",
                ]
            )
            + "\n"
        )
        (repo / "README.md").write_text("# Sample Repo\n\nPayload validation worker.\n")

        index = self.store.index_repository(str(repo), author="test")
        self.assertEqual(index["repo_name"], "sample_repo")
        self.assertGreaterEqual(index["counts"]["files"], 2)
        self.assertGreaterEqual(index["counts"]["symbols"], 2)
        self.assertGreaterEqual(index["counts"]["edges"], 2)
        self.assertIn("health", index)
        self.assertIn("edges_per_file", index["health"])

        slice_result = self.store.query_code_slice(
            str(repo),
            "validate payload worker",
            goal_id="goal-code-slice",
            author="test",
        )
        self.assertEqual(slice_result["repo_name"], "sample_repo")
        self.assertIn("slice_node_id", slice_result)
        self.assertTrue(any(item["path"] == "worker.py" for item in slice_result["relevant_files"]))
        self.assertTrue(
            any("validate_task_payload" in item["name"] for item in slice_result["relevant_symbols"])
        )
        self.assertIn(slice_result["slice_confidence"]["level"], {"medium", "high"})
        self.assertNotEqual(slice_result["slice_confidence"]["gate"], "do_not_patch_from_this_slice_alone")

        loaded = self.store.load_code_slice(slice_result["slice_id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["slice_id"], slice_result["slice_id"])

        indexes = self.store.list_code_indexes()
        self.assertEqual(len(indexes), 1)
        self.assertEqual(indexes[0]["repo_name"], "sample_repo")

        verify = self.store.verify_all()
        self.assertEqual(verify["invalid_count"], 0)

    def test_maintain_code_map_prunes_old_slices_and_keeps_integrity(self) -> None:
        repo = Path(self.tmpdir.name) / "maint_repo"
        repo.mkdir()
        (repo / "app.py").write_text(
            "def alpha_feature():\n"
            "    return 'alpha'\n\n"
            "def beta_feature():\n"
            "    return alpha_feature()\n"
        )
        self.store.index_repository(str(repo), author="test")
        for idx in range(6):
            self.store.query_code_slice(str(repo), f"alpha beta feature {idx}", limit=2)

        result = self.store.maintain_code_map(
            max_slices_per_repo=3,
            prune_inactive_runs=True,
            vacuum=False,
            backup=False,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["integrity_after"], "ok")
        self.assertEqual(result["after"]["code_slices"], 3)
        self.assertEqual(result["verify"]["invalid_count"], 0)
        self.assertEqual(result["retention_policy"]["max_slices_per_repo"], 3)
        self.assertTrue(result["retention_policy"]["code_slices_are_cache"])

    def test_slice_quality_flags_weak_queries(self) -> None:
        repo = Path(self.tmpdir.name) / "quality_repo"
        repo.mkdir()
        (repo / "worker.py").write_text("def validate_payload(payload):\n    return payload is not None\n")
        self.store.index_repository(str(repo), author="test")

        slice_result = self.store.query_code_slice(str(repo), "completely unrelated feature", limit=2)

        self.assertEqual(slice_result["slice_confidence"]["level"], "low")
        self.assertEqual(slice_result["slice_confidence"]["gate"], "do_not_patch_from_this_slice_alone")
        self.assertIn("fallback_to_legacy_code_map_or_full_repo_scan_if_available", slice_result["recommended_next_steps"])

    def test_maintain_prunes_old_slices_but_keeps_recent_floor(self) -> None:
        repo = Path(self.tmpdir.name) / "aging_repo"
        repo.mkdir()
        (repo / "app.py").write_text("def alpha_feature():\n    return 'alpha'\n")
        self.store.index_repository(str(repo), author="test")
        for idx in range(7):
            self.store.query_code_slice(str(repo), f"alpha feature {idx}", limit=2)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE code_slices SET created_at='2000-01-01T00:00:00.000Z'"
            )
            conn.commit()
        finally:
            conn.close()

        result = self.store.maintain_code_map(
            max_slices_per_repo=50,
            max_slice_age_days=30,
            min_slices_per_repo=2,
            prune_inactive_runs=True,
            vacuum=False,
            backup=False,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["after"]["code_slices"], 2)
        self.assertEqual(result["deleted_slices"], 5)

    def test_slice_confidence_marks_stale_index_after_git_change(self) -> None:
        repo = Path(self.tmpdir.name) / "git_repo"
        repo.mkdir()
        target = repo / "worker.py"
        target.write_text("def validate_payload(payload):\n    return payload is not None\n")
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "add", "worker.py"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test User",
                "commit",
                "-m",
                "initial",
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.store.index_repository(str(repo), author="test")
        fresh = self.store.query_code_slice(str(repo), "validate payload", limit=2)
        self.assertFalse(fresh["freshness"]["stale"])
        self.assertNotEqual(fresh["slice_confidence"]["gate"], "do_not_patch_from_this_slice_alone")

        target.write_text("def validate_payload(payload):\n    return payload is not None and payload != {}\n")
        stale = self.store.query_code_slice(str(repo), "validate payload", limit=2)

        self.assertTrue(stale["freshness"]["stale"])
        self.assertEqual(stale["slice_confidence"]["level"], "low")
        self.assertEqual(stale["slice_confidence"]["gate"], "do_not_patch_from_this_slice_alone")
        self.assertIn("index_stale_repo_changed_since_run", stale["warnings"])
        self.assertIn("rerun_index_repository_before_patching", stale["recommended_next_steps"])

    def test_slice_confidence_ignores_unrelated_dirty_files(self) -> None:
        repo = Path(self.tmpdir.name) / "git_repo_unrelated_dirty"
        repo.mkdir()
        target = repo / "worker.py"
        notes = repo / "README.md"
        target.write_text("def validate_payload(payload):\n    return payload is not None\n")
        notes.write_text("# Notes\n")
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "add", "worker.py", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test User",
                "commit",
                "-m",
                "initial",
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.store.index_repository(str(repo), author="test")
        notes.write_text("# Notes\n\nUnrelated work in progress.\n")
        result = self.store.query_code_slice(str(repo), "validate payload worker", limit=2)

        self.assertFalse(result["freshness"]["stale"])
        self.assertEqual(result["freshness"]["level"], "fresh")
        self.assertIn("repo_has_unrelated_dirty_changes_outside_slice", result["freshness"]["warnings"])
        self.assertNotEqual(result["slice_confidence"]["gate"], "do_not_patch_from_this_slice_alone")

    def test_maintain_keeps_exact_recent_floor(self) -> None:
        repo = Path(self.tmpdir.name) / "floor_repo"
        repo.mkdir()
        (repo / "app.py").write_text("def alpha_feature():\n    return 'alpha'\n")
        self.store.index_repository(str(repo), author="test")
        for idx in range(5):
            self.store.query_code_slice(str(repo), f"alpha feature {idx}", limit=2)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE code_slices SET created_at='2000-01-01T00:00:00.000Z'")
            conn.commit()
        finally:
            conn.close()

        result = self.store.maintain_code_map(
            max_slices_per_repo=50,
            max_slice_age_days=30,
            min_slices_per_repo=5,
            prune_inactive_runs=True,
            vacuum=False,
            backup=False,
        )

        self.assertEqual(result["after"]["code_slices"], 5)
        self.assertEqual(result["deleted_slices"], 0)

    def test_code_index_health_handles_empty_repo(self) -> None:
        repo = Path(self.tmpdir.name) / "empty_repo"
        repo.mkdir()

        index = self.store.index_repository(str(repo), author="test")

        self.assertEqual(index["counts"]["files"], 0)
        self.assertEqual(index["health"]["edges_per_file"], 0.0)
        self.assertEqual(index["health"]["symbols_per_file"], 0.0)

    def test_expected_hub_fanout_does_not_raise_suspicious_warning(self) -> None:
        index = {
            "files": [{"path": "src/config.py"}],
            "symbols": [],
            "edges": [{"from_path": "src/config.py"} for _ in range(5001)],
        }

        health = _code_index_health(index)

        self.assertEqual(health["top_edge_fanout"][0]["classification"], "expected_hub")
        self.assertNotIn("single_file_edge_fanout_over_5000", health["warnings"])

    def test_suspicious_fanout_still_warns(self) -> None:
        index = {
            "files": [{"path": "src/feature.py"}],
            "symbols": [],
            "edges": [{"from_path": "src/feature.py"} for _ in range(5001)],
        }

        health = _code_index_health(index)

        self.assertEqual(health["top_edge_fanout"][0]["classification"], "suspicious_fanout")
        self.assertIn("single_file_edge_fanout_over_5000", health["warnings"])

    def test_jsonl_delta_import_is_idempotent(self) -> None:
        source_db = Path(self.tmpdir.name) / "source.db"
        target_db = Path(self.tmpdir.name) / "target.db"
        source = TimelineCodeMap(str(source_db))
        target = TimelineCodeMap(str(target_db))
        goal = "goal-sync-idempotent"
        first = source.record(domain="timeline", kind="action", title="source step", goal_id=goal, author="host-a")
        second = source.record(domain="timeline", kind="action", title="source followup", goal_id=goal, author="host-a")
        source.link(first, second, "supports", author="host-a")
        event_path = Path(self.tmpdir.name) / "delta.jsonl"

        exported = source.export_delta(str(event_path), host_id="host-a-test")
        first_import = target.import_delta(str(event_path), peer_id="host-a-test")
        second_import = target.import_delta(str(event_path), peer_id="host-a-test")

        self.assertEqual(exported["event_count"], 4)
        self.assertEqual(first_import["status"], "ok")
        self.assertEqual(first_import["imported"], 4)
        self.assertEqual(second_import["imported"], 0)
        self.assertGreaterEqual(second_import["skipped"], 4)
        target_session = target.load_session(goal_id=goal)
        self.assertEqual(target_session["node_count"], 2)
        self.assertGreaterEqual(target_session["edge_count"], 2)
        status = target.sync_status()
        self.assertEqual(status["imported_events"], 4)
        self.assertEqual(status["cursors"][0]["peer_id"], "host-a-test")

    def test_jsonl_delta_import_handles_out_of_order_events(self) -> None:
        source = TimelineCodeMap(str(Path(self.tmpdir.name) / "out_of_order_source.db"))
        target = TimelineCodeMap(str(Path(self.tmpdir.name) / "out_of_order_target.db"))
        goal = "goal-sync-out-of-order"
        first = source.record(domain="timeline", kind="action", title="first", goal_id=goal, author="host-a")
        second = source.record(domain="timeline", kind="action", title="second", goal_id=goal, author="host-a")
        source.link(first, second, "supports", author="host-a")
        event_path = Path(self.tmpdir.name) / "out_of_order_delta.jsonl"
        source.export_delta(str(event_path), host_id="host-a-test")
        lines = event_path.read_text().splitlines()
        event_path.write_text("\n".join(reversed(lines)) + "\n")

        result = target.import_delta(str(event_path), peer_id="host-a-test")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported"], 4)
        self.assertEqual(result["fork_edges"], 0)
        target_session = target.load_session(goal_id=goal)
        self.assertEqual(target_session["node_count"], 2)
        self.assertEqual(target.verify_all()["invalid_count"], 0)

    def test_jsonl_delta_import_reports_malformed_lines_and_keeps_valid_events(self) -> None:
        source = TimelineCodeMap(str(Path(self.tmpdir.name) / "malformed_source.db"))
        target = TimelineCodeMap(str(Path(self.tmpdir.name) / "malformed_target.db"))
        source.record(domain="timeline", kind="action", title="valid event", goal_id="goal-malformed", author="host-a")
        event_path = Path(self.tmpdir.name) / "malformed_delta.jsonl"
        source.export_delta(str(event_path), host_id="host-a-test")
        valid_lines = event_path.read_text().splitlines()
        event_path.write_text(valid_lines[0] + "\n" + "{not valid json\n")

        result = target.import_delta(str(event_path), peer_id="host-a-test")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(target.verify_all()["invalid_count"], 0)

    def test_jsonl_delta_edge_conflict_is_first_write_wins(self) -> None:
        target = TimelineCodeMap(str(Path(self.tmpdir.name) / "edge_policy_target.db"))
        event_path = Path(self.tmpdir.name) / "edge_policy_delta.jsonl"
        events = [
            {
                "schema_version": 1,
                "event_id": "edge-first",
                "event_type": "edge_upsert",
                "host_id": "host-a-test",
                "origin_db": "memory://test",
                "sync_batch_id": "batch-edge-policy",
                "source_created_at": "2026-01-01T00:00:00.000Z",
                "source_sync_ts": "2026-01-01T00:00:00.000Z",
                "payload": {
                    "from_id": "node-a",
                    "to_id": "node-b",
                    "relation": "supports",
                    "weight": 0.25,
                    "author": "first",
                    "created_at": "2026-01-01T00:00:00.000Z",
                },
            },
            {
                "schema_version": 1,
                "event_id": "edge-second",
                "event_type": "edge_upsert",
                "host_id": "host-a-test",
                "origin_db": "memory://test",
                "sync_batch_id": "batch-edge-policy",
                "source_created_at": "2026-01-01T00:00:01.000Z",
                "source_sync_ts": "2026-01-01T00:00:01.000Z",
                "payload": {
                    "from_id": "node-a",
                    "to_id": "node-b",
                    "relation": "supports",
                    "weight": 0.95,
                    "author": "second",
                    "created_at": "2026-01-01T00:00:01.000Z",
                },
            },
        ]
        event_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

        result = target.import_delta(str(event_path), peer_id="host-a-test")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)
        conn = sqlite3.connect(target.db_path)
        try:
            row = conn.execute("SELECT weight, author FROM edges WHERE from_id='node-a' AND to_id='node-b'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 0.25)
        self.assertEqual(row[1], "first")

    def test_jsonl_delta_preserves_goal_chain_fork(self) -> None:
        source = TimelineCodeMap(str(Path(self.tmpdir.name) / "fork_source.db"))
        target = TimelineCodeMap(str(Path(self.tmpdir.name) / "fork_target.db"))
        goal = "goal-sync-fork"
        source.record(domain="timeline", kind="action", title="host-a branch", goal_id=goal, author="hermes")
        target.record(domain="timeline", kind="action", title="mac branch", goal_id=goal, author="claude")
        event_path = Path(self.tmpdir.name) / "fork_delta.jsonl"

        source.export_delta(str(event_path), host_id="host-a-test")
        result = target.import_delta(str(event_path), peer_id="host-a-test")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fork_edges"], 1)
        graph = target.export_graph()
        fork_edges = [edge for edge in graph["edges"] if edge["relation"] == "fork"]
        self.assertEqual(len(fork_edges), 1)
        self.assertEqual(target.verify_all()["invalid_count"], 0)

    def test_neural_links_are_created_with_nodes_and_recalled_across_goals(self) -> None:
        first = self.store.record(
            domain="timeline", kind="report", title="오늘의 미국장 점검",
            body={"workflow": "us-market-daily", "result": "gap-and-fade"},
            goal_id="market-run-1", author="worker-a",
        )
        second = self.store.record(
            domain="timeline", kind="report", title="오늘의 미국장 점검",
            body={"workflow": "us-market-daily", "result": "risk-off drift"},
            goal_id="market-run-2", author="worker-b",
        )
        status = self.store.neural_link_status()
        self.assertEqual(status["indexed_nodes"], 2)
        self.assertEqual(status["pending_nodes"], 0)
        conn = sqlite3.connect(self.db_path)
        try:
            edge = conn.execute(
                "SELECT relation, weight FROM edges WHERE from_id=? AND to_id=?", (second, first)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(edge)
        self.assertEqual(edge[0], "same_workflow")
        self.assertGreaterEqual(edge[1], 0.5)
        recalled_ids = {item["id"] for item in self.store.get_context("market-run-2", depth=2)["associative_memory"]}
        self.assertIn(first, recalled_ids)

    def test_neural_index_failure_rolls_back_the_timeline_node(self) -> None:
        with mock.patch(
            "hermes_timeline_code_map.store.index_neural_node",
            side_effect=RuntimeError("forced neural index failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced neural index failure"):
                self.store.record(
                    domain="timeline", kind="action", title="must stay atomic", goal_id="atomic-neural-link"
                )
        conn = sqlite3.connect(self.db_path)
        try:
            node_count = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
            feature_count = conn.execute("SELECT count(*) FROM neural_node_features").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(node_count, 0)
        self.assertEqual(feature_count, 0)

    def test_neural_recall_walks_a_bounded_multi_hop_chain(self) -> None:
        first = self.store.record(
            domain="timeline", kind="decision", title="Hermes Telegram worker contract",
            body={"workflow": "telegram-worker-contract", "revision": 1}, goal_id="contract-1",
        )
        second = self.store.record(
            domain="timeline", kind="decision", title="Hermes Telegram worker contract",
            body={"workflow": "telegram-worker-contract", "revision": 2}, goal_id="contract-2",
        )
        third = self.store.record(
            domain="timeline", kind="decision", title="Hermes Telegram worker contract",
            body={"workflow": "telegram-worker-contract", "revision": 3}, goal_id="contract-3",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM edges WHERE from_id=? AND to_id=? AND relation IN ('same_workflow','same_entity','associates')",
                (third, first),
            )
            conn.commit()
        finally:
            conn.close()
        by_id = {item["id"]: item for item in self.store.get_context("contract-3", depth=3)["associative_memory"]}
        self.assertIn(second, by_id)
        self.assertIn(first, by_id)
        self.assertEqual(by_id[first]["hop"], 2)

    def test_neural_recall_filters_expired_live_state_without_deleting_history(self) -> None:
        node_id = self.store.record(
            domain="market", kind="quote", title="VIX intraday quote",
            body={"symbol": "VIX", "quote": 18.56}, goal_id="market-live-1",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE neural_node_features SET expires_at='2000-01-01T00:00:00.000Z' WHERE node_id=?",
                (node_id,),
            )
            conn.commit()
        finally:
            conn.close()
        recalled = self.store.recall_neural_context("VIX intraday quote")
        self.assertNotIn(node_id, {item["id"] for item in recalled["items"]})
        self.assertIsNotNone(self.store.get_node(node_id))

    def test_neural_recall_uses_short_query_coverage_against_long_reports(self) -> None:
        node_id = self.store.record(
            domain="timeline",
            kind="report",
            title="2026-07-20 미국장 점검 일중 흐름",
            body={
                "workflow": "us-market-daily",
                "details": " ".join(f"detail-{idx}" for idx in range(80)),
            },
            goal_id="market-long-report",
        )
        unrelated = self.store.record(
            domain="timeline",
            kind="action",
            title="adapter 상태 점검 및 코드맵 루프 파일 분석",
            goal_id="adapter-check",
        )
        recalled = self.store.recall_neural_context("오늘의 미국장 점검")
        self.assertIn(node_id, {item["id"] for item in recalled["items"]})
        self.assertNotIn(unrelated, {item["id"] for item in recalled["items"]})

    def test_neural_recall_weights_partial_title_cues(self) -> None:
        node_id = self.store.record(
            domain="architecture",
            kind="implementation",
            title="Timeline NeuralLink incremental memory deployed and verified",
            body={"candidate_limit": 24, "default_depth": 2},
            goal_id="neural-link-design",
        )
        recalled = self.store.recall_neural_context("전에 Timeline NeuralLink 설계가 뭐였지?")
        self.assertIn(node_id, {item["id"] for item in recalled["items"]})

    def test_semantic_capsules_link_different_wording_without_a_model_in_the_hot_path(self) -> None:
        older = self.store.record(
            domain="memory",
            kind="decision",
            title="텔레그램 연쇄 연결 단절 조사",
            body={
                "memory_descriptor": {
                    "concepts": ["transport cascade", "service continuity"],
                    "aliases": ["메시징 경로 연쇄 장애"],
                }
            },
            goal_id="incident-ko",
        )
        newer = self.store.record(
            domain="memory",
            kind="decision",
            title="worker transport cascade failure analysis",
            body={
                "memory_descriptor": {
                    "concepts": ["transport cascade", "service continuity"],
                    "aliases": ["worker MCP disconnect"],
                }
            },
            goal_id="incident-en",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            edge = conn.execute(
                "SELECT relation FROM edges WHERE from_id=? AND to_id=?", (newer, older)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(edge)
        self.assertEqual(edge[0], "same_concept")

        recalled = self.store.recall_neural_context(
            "worker transport cascade failure", max_depth=3
        )
        by_id = {item["id"]: item for item in recalled["items"]}
        self.assertIn(newer, by_id)
        self.assertIn(older, by_id)
        self.assertGreaterEqual(by_id[older]["hop"], 1)

    def test_candidate_mode_returns_an_ai_rerank_packet(self) -> None:
        self.store.record(
            domain="memory",
            kind="contract",
            title="Hermes worker transport contract",
            body={"memory_descriptor": {"concepts": ["service continuity"]}},
            goal_id="candidate-packet",
        )
        result = self.store.recall_neural_context(
            "Hermes worker transport",
            candidate_mode=True,
            limit=10,
            max_chars=2600,
        )
        self.assertTrue(result["candidate_mode"])
        self.assertIn("후보 패킷", result["context"])
        self.assertGreaterEqual(len(result["items"]), 1)

    def test_neural_backfill_refreshes_old_feature_versions(self) -> None:
        node_id = self.store.record(
            domain="timeline", kind="decision", title="title feature migration", goal_id="feature-v1"
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM neural_feature_terms WHERE node_id=? AND term_type IN ('title','meta')", (node_id,)
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.store.neural_link_status()["pending_nodes"], 1)
        result = self.store.backfill_neural_links(batch_size=1)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["pending_nodes"], 0)

    def test_neural_backfill_repairs_preexisting_unindexed_nodes(self) -> None:
        first = self.store.record(domain="timeline", kind="action", title="repair KIS auth", goal_id="old-a")
        second = self.store.record(domain="timeline", kind="action", title="repair KIS auth", goal_id="old-b")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM neural_feature_terms WHERE node_id IN (?,?)", (first, second))
            conn.execute("DELETE FROM neural_node_features WHERE node_id IN (?,?)", (first, second))
            conn.execute(
                "DELETE FROM edges WHERE (from_id IN (?,?) OR to_id IN (?,?)) AND relation IN ('same_workflow','same_entity','associates')",
                (first, second, first, second),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.store.neural_link_status()["pending_nodes"], 2)
        result = self.store.backfill_neural_links(batch_size=1)
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["pending_nodes"], 0)
        self.assertGreaterEqual(result["links_created"], 1)


if __name__ == "__main__":
    unittest.main()
