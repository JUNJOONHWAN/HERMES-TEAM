from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_timeline_code_map.roadmap import (
    RoadmapConflict,
    RoadmapInProgress,
    RoadmapStore,
    RoadmapValidationError,
    verify_schedule_contract,
)


class RoadmapStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "graph.db"
        self.roadmap = RoadmapStore(str(self.db_path))
        self.goal = "goal-roadmap-test"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def create_task(self, task_id: str = "task-1", event_id: str = "evt-create") -> dict:
        return self.roadmap.append_event(
            goal_id=self.goal,
            entity_id=task_id,
            entity_type="task",
            event_type="task.created",
            expected_version=0,
            event_id=event_id,
            actor={"type": "user", "id": "operator"},
            payload={"title": "Build roadmap", "state": "INBOX"},
        )

    def test_append_is_idempotent_and_optimistically_versioned(self) -> None:
        first = self.create_task()
        second = self.create_task()
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["node_id"], second["node_id"])

        with self.assertRaises(RoadmapConflict):
            self.roadmap.append_event(
                goal_id=self.goal,
                entity_id="task-1",
                entity_type="task",
                event_type="task.state_changed",
                expected_version=0,
                payload={"state": "PLANNED"},
            )

    def test_task_transition_and_history(self) -> None:
        self.create_task()
        planned = self.roadmap.append_event(
            goal_id=self.goal,
            entity_id="task-1",
            entity_type="task",
            event_type="task.state_changed",
            expected_version=1,
            payload={"state": "PLANNED"},
        )
        self.assertEqual(planned["entity_version"], 2)
        history = self.roadmap.get_task_history("task-1")
        self.assertEqual(len(history["events"]), 2)
        self.assertEqual(history["task"]["state"], "PLANNED")
        self.assertTrue(all(item["timeline_node"] for item in history["events"]))

        with self.assertRaises(RoadmapValidationError):
            self.roadmap.append_event(
                goal_id=self.goal,
                entity_id="task-1",
                entity_type="task",
                event_type="task.state_changed",
                expected_version=2,
                payload={"state": "DONE"},
            )

    def test_projection_rebuild_round_trip(self) -> None:
        self.create_task()
        self.roadmap.append_event(
            goal_id=self.goal,
            entity_id="task-1",
            entity_type="task",
            event_type="task.state_changed",
            expected_version=1,
            payload={"state": "PLANNED", "priority": 3},
        )
        before = self.roadmap.get_roadmap(self.goal)
        rebuilt = self.roadmap.rebuild_projection(goal_id=self.goal)
        after = self.roadmap.get_roadmap(self.goal)
        self.assertEqual(rebuilt["errors"], [])
        self.assertEqual(rebuilt["events_rebuilt"], 2)
        self.assertEqual(before["entities"][0]["current_version"], after["entities"][0]["current_version"])
        self.assertEqual(after["entities"][0]["payload"]["priority"], 3)

    def test_goal_ids_are_recoverable_from_timeline_projection(self) -> None:
        self.create_task()
        self.roadmap.append_event(
            goal_id="goal-second",
            entity_id="task-second",
            entity_type="task",
            event_type="task.created",
            expected_version=0,
            payload={"title": "Second", "state": "INBOX"},
        )
        self.assertEqual(self.roadmap.list_goal_ids(), ["goal-roadmap-test", "goal-second"])

    def test_done_requires_verification(self) -> None:
        self.create_task()
        sequence = [
            ("task.state_changed", "PLANNED"),
            ("task.state_changed", "READY"),
            ("task.state_changed", "ROUTED"),
            ("task.state_changed", "RUNNING"),
            ("task.state_changed", "REVIEW"),
        ]
        version = 1
        for event_type, state in sequence:
            self.roadmap.append_event(
                goal_id=self.goal,
                entity_id="task-1",
                entity_type="task",
                event_type=event_type,
                expected_version=version,
                payload={"state": state},
            )
            version += 1
        self.roadmap.append_event(
            goal_id=self.goal,
            entity_id="task-1",
            entity_type="task",
            event_type="task.state_changed",
            expected_version=version,
            payload={"state": "DONE"},
        )
        failed = self.roadmap.verify_goal_contract(self.goal)
        self.assertFalse(failed["valid"])
        self.assertIn("task-1:done_without_verification", failed["errors"])

    def test_schedule_contract(self) -> None:
        valid = {
            "intended_timezone": "Asia/Seoul",
            "intended_local_time": "09:00",
            "stored_rrule_timezone": "UTC",
            "scheduler_execution_timezone": "UTC",
            "stored_rrule": "FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
            "reporting_timezone": "KST",
            "out_of_window_action": "scheduler_timezone_mismatch",
            "effective_local_date": "2026-07-17",
        }
        self.assertTrue(verify_schedule_contract(valid)["valid"])
        invalid = {**valid, "stored_rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"}
        self.assertFalse(verify_schedule_contract(invalid)["valid"])

        weekly_kst = {
            **valid,
            "intended_local_time": "01:00",
            "intended_byday": ["MO"],
            "stored_rrule": "FREQ=WEEKLY;BYHOUR=16;BYMINUTE=0;BYDAY=SU",
        }
        self.assertTrue(verify_schedule_contract(weekly_kst)["valid"])
        self.assertFalse(
            verify_schedule_contract({**weekly_kst, "stored_rrule": "FREQ=WEEKLY;BYHOUR=16;BYMINUTE=0;BYDAY=MO"})["valid"]
        )

    def test_timezone_mismatch_event_rebuilds_schedule_projection(self) -> None:
        self.create_task()
        self.roadmap.append_event(
            goal_id=self.goal,
            entity_id="task-1",
            entity_type="task",
            event_type="task.state_changed",
            expected_version=1,
            payload={"state": "PLANNED"},
        )
        schedule = {
            "state": "SCHEDULED",
            "intended_timezone": "Asia/Seoul",
            "intended_local_time": "09:00",
            "stored_rrule_timezone": "UTC",
            "scheduler_execution_timezone": "UTC",
            "stored_rrule": "FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
            "reporting_timezone": "KST",
            "out_of_window_action": "scheduler_timezone_mismatch",
            "effective_local_date": "2026-07-17",
            "next_run_utc": "2026-07-18T00:00:00Z",
            "invocation_status": "out_of_window_kst_invocation",
        }
        self.roadmap.append_event(
            goal_id=self.goal,
            entity_id="task-1",
            entity_type="task",
            event_type="scheduler.timezone_mismatch",
            expected_version=2,
            payload=schedule,
        )
        rebuilt = self.roadmap.rebuild_projection(goal_id=self.goal)
        self.assertEqual(rebuilt["errors"], [])
        self.assertEqual(self.roadmap.get_roadmap(self.goal)["schedules"][0]["next_run_utc"], "2026-07-18T00:00:00Z")

    def test_history_dereferences_artifact_evidence(self) -> None:
        self.create_task()
        artifact = self.roadmap.timeline.record(
            domain="roadmap",
            kind="executor_result",
            title="evidence",
            body={"ok": True},
            goal_id=self.goal,
        )
        self.roadmap.append_event(
            goal_id=self.goal,
            entity_id="task-1",
            entity_type="task",
            event_type="task.evidence",
            expected_version=1,
            payload={"artifact_node_id": artifact},
        )
        history = self.roadmap.get_task_history("task-1")
        self.assertEqual(history["events"][-1]["evidence_nodes"]["artifact_node_id"]["id"], artifact)

    def test_ready_task_can_be_deferred_to_schedule(self) -> None:
        self.create_task()
        version = 1
        for state in ("PLANNED", "READY", "SCHEDULED"):
            result = self.roadmap.append_event(
                goal_id=self.goal,
                entity_id="task-1",
                entity_type="task",
                event_type="task.state_changed",
                expected_version=version,
                payload={"state": state},
            )
            version = result["entity_version"]
        self.assertEqual(self.roadmap.get_task_history("task-1")["task"]["state"], "SCHEDULED")

    def test_concurrent_writers_allow_only_one_expected_version(self) -> None:
        self.create_task()

        def write(index: int) -> str:
            try:
                self.roadmap.append_event(
                    goal_id=self.goal,
                    entity_id="task-1",
                    entity_type="task",
                    event_type="task.state_changed",
                    expected_version=1,
                    event_id=f"evt-concurrent-{index}",
                    payload={"state": "PLANNED"},
                )
                return "committed"
            except (RoadmapConflict, RoadmapInProgress, RoadmapValidationError):
                return "rejected"

        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(write, range(4)))
        self.assertEqual(outcomes.count("committed"), 1)
        self.assertEqual(outcomes.count("rejected"), 3)


if __name__ == "__main__":
    unittest.main()
