from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import project_card_controller as controller
from hermes_cli import projects_db as pdb
from hermes_cli import supervisor_registry as registry
from tools import supervisor_tools


@pytest.fixture()
def project_control_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()
    with kb.connect_closing(board="default") as conn:
        for shell_key in ("code", "browser", "verification", "operations"):
            registry.register_shell_version(
                conn,
                shell_key=shell_key,
                name=shell_key.title(),
                contract={"allowed_adapters": ["codex", "opencode"]},
                allowed_capabilities=["terminal"],
                evidence_policy={},
            )
    yield home
    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()


def _mark_status(task_id: str, status: str) -> None:
    with kb.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def test_controller_owns_project_and_card_thread_lifecycle(project_control_home):
    started = controller.start_project(
        name="Hermes Control",
        goal="Implement the deterministic controller",
        shell_key="code",
        acceptance_criteria=["Controller tests pass", "Evidence is recorded"],
        input_refs=["spec:controller-v1"],
    )
    project = started["project"]
    root = started["card"]

    assert started["schema"] == controller.SCHEMA
    assert project["status"] == "active"
    assert project["board_slug"] == "default"
    assert root["project_id"] == project["id"]
    assert root["root_task_id"] == root["id"]
    assert root["acceptance_criteria"] == [
        "Controller tests pass",
        "Evidence is recorded",
    ]
    assert root["input_refs"] == ["spec:controller-v1"]
    # A code project without a repository is valid scratch work, not a broken
    # worktree with no repo anchor.
    assert root["workspace_kind"] == "scratch"

    _mark_status(root["id"], "done")
    follow = controller.continue_card(
        root["id"],
        title="Add the web and Telegram surfaces",
        shell_key="browser",
    )["card"]
    assert follow["root_task_id"] == root["id"]
    assert follow["project_id"] == project["id"]
    assert follow["status"] == "ready"

    inspected = controller.inspect_card(follow["id"])
    assert inspected["links"]["incoming"] == [
        {
            "task_id": root["id"],
            "relation_type": "follows",
            "blocking": True,
        }
    ]
    assert inspected["card"]["input_refs"] == [f"card:{root['id']}"]

    split = controller.split_card(
        follow["id"],
        cards=[
            {"title": "Implement web actions", "shell_key": "browser"},
            {"title": "Implement supervisor action", "shell_key": "operations"},
        ],
    )["cards"]
    assert len(split) == 2
    assert {card["status"] for card in split} == {"ready"}
    assert {card["root_task_id"] for card in split} == {root["id"]}
    split_link = controller.inspect_card(split[0]["id"])["links"]["incoming"][0]
    assert split_link["relation_type"] == "references"
    assert split_link["blocking"] is False

    before_invalid_split = len(controller.inspect_project(project["id"])["cards"])
    with pytest.raises(controller.ProjectCardControllerError, match="unknown role shell"):
        controller.split_card(
            follow["id"],
            cards=[
                {"title": "Would otherwise be created", "shell_key": "browser"},
                {"title": "Invalid sibling", "shell_key": "missing-shell"},
            ],
        )
    assert len(controller.inspect_project(project["id"])["cards"]) == before_invalid_split

    # Verification is a real blocking review relation: the source must finish.
    verification = controller.verify_card(follow["id"])["card"]
    assert verification["status"] == "todo"
    assert controller.inspect_card(verification["id"])["links"]["incoming"][0][
        "relation_type"
    ] == "reviews"

    summary = controller.inspect_project(project["id"])
    assert summary["threads"] == [
        {
            "root_card_id": root["id"],
            "card_ids": [
                root["id"],
                follow["id"],
                split[0]["id"],
                split[1]["id"],
                verification["id"],
            ],
        }
    ]


def test_recovery_is_nonblocking_and_project_close_is_guarded(project_control_home):
    started = controller.start_project(
        name="Recovery",
        goal="Fail safely",
        shell_key="operations",
    )
    project_id = started["project"]["id"]
    root_id = started["card"]["id"]
    _mark_status(root_id, "blocked")

    recovered = controller.recover_card(root_id)["card"]
    assert recovered["status"] == "ready"
    inspected = controller.inspect_card(recovered["id"])
    assert inspected["links"]["incoming"][0] == {
        "task_id": root_id,
        "relation_type": "recovers",
        "blocking": False,
    }
    assert inspected["recovery_source_task_ids"] == [root_id]

    with pytest.raises(controller.ProjectCardControllerError, match="open cards"):
        controller.close_project(project_id)

    _mark_status(root_id, "done")
    _mark_status(recovered["id"], "done")
    closed = controller.close_project(project_id)
    assert closed["project"]["status"] == "completed"
    assert closed["project"]["completed_at"] is not None

    with pytest.raises(controller.ProjectCardControllerError, match="reopen"):
        controller.continue_card(root_id, title="Must not silently reopen")

    reopened = controller.reopen_project(project_id)
    assert reopened["project"]["status"] == "active"
    assert reopened["project"]["completed_at"] is None


def test_supervisor_common_gateway_calls_native_controller(project_control_home):
    payload = json.loads(
        supervisor_tools._handle_project_cards(
            {
                "action": "start_project",
                "project_name": "Telegram Team Project",
                "title": "Create the first operations card",
                "shell_key": "operations",
                "acceptance_criteria": ["Card is visible from every surface"],
            },
            session_id="telegram-session-1",
        )
    )

    assert payload["action"] == "start_project"
    assert payload["card"]["session_id"] == "telegram-session-1"
    assert payload["card"]["created_by"] == "hermes-project-card-controller"
    summary = controller.inspect_project(payload["project"]["id"])
    assert [card["id"] for card in summary["cards"]] == [payload["card"]["id"]]


def test_legacy_schema_migrates_to_independent_threads_and_typed_links(
    tmp_path, monkeypatch
):
    home = tmp_path / "legacy-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    with kb.connect_closing(board="default") as current:
        current.execute(
            "INSERT INTO tasks(id, title, status, workspace_kind, created_at) "
            "VALUES ('legacy-parent', 'Parent', 'done', 'scratch', 1), "
            "('legacy-child', 'Child', 'todo', 'scratch', 2)"
        )
        current.execute(
            "INSERT INTO task_links(parent_id, child_id) "
            "VALUES ('legacy-parent', 'legacy-child')"
        )
        current.commit()

    db_path = kb.kanban_db_path(board="default")
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP INDEX IF EXISTS idx_tasks_project_root")
    conn.execute("DROP INDEX IF EXISTS idx_links_relation")
    conn.execute("ALTER TABLE tasks DROP COLUMN root_task_id")
    conn.execute("ALTER TABLE tasks DROP COLUMN acceptance_criteria")
    conn.execute("ALTER TABLE tasks DROP COLUMN input_refs")
    conn.execute("ALTER TABLE task_links DROP COLUMN relation_type")
    conn.commit()
    conn.close()
    kb._INITIALIZED_PATHS.clear()

    with kb.connect_closing(board="default") as migrated:
        rows = migrated.execute(
            "SELECT id, root_task_id FROM tasks ORDER BY id"
        ).fetchall()
        link = migrated.execute(
            "SELECT relation_type FROM task_links WHERE child_id='legacy-child'"
        ).fetchone()

    assert [(row["id"], row["root_task_id"]) for row in rows] == [
        ("legacy-child", "legacy-child"),
        ("legacy-parent", "legacy-parent"),
    ]
    assert link["relation_type"] == "depends_on"
