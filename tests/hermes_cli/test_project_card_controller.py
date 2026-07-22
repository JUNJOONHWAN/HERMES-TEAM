from __future__ import annotations

import json
import sqlite3
import subprocess

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

    independent = controller.add_project_card(
        project["id"],
        title="Start an independent release thread",
        shell_key="operations",
        acceptance_criteria=["Release thread is independently closable"],
    )["card"]
    assert independent["project_id"] == project["id"]
    assert independent["root_task_id"] == independent["id"]
    assert controller.inspect_card(independent["id"])["links"]["incoming"] == []

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
        },
        {
            "root_card_id": independent["id"],
            "card_ids": [independent["id"]],
        },
    ]


def test_non_git_project_path_uses_durable_directory(project_control_home, tmp_path):
    project_dir = tmp_path / "long-running-project"
    project_dir.mkdir()
    (project_dir / "PRD.md").write_text("# PRD\n", encoding="utf-8")

    started = controller.start_project(
        name="Non-Git project",
        goal="Turn the PRD into an implementation roadmap",
        shell_key="code",
        primary_path=str(project_dir),
    )

    assert started["card"]["workspace_kind"] == "dir"
    assert started["card"]["workspace_path"] == str(project_dir)
    follow = controller.continue_card(
        started["card"]["id"],
        title="Research an unresolved requirement",
        shell_key="browser",
    )["card"]
    assert follow["workspace_kind"] == "dir"
    assert follow["workspace_path"] == str(project_dir)


def test_git_project_path_keeps_code_cards_in_worktrees(
    project_control_home, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Hermes Tests"],
        check=True,
    )
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )

    started = controller.start_project(
        name="Git project",
        goal="Implement a change in isolation",
        shell_key="code",
        primary_path=str(repo),
    )

    assert started["card"]["workspace_kind"] == "worktree"
    assert started["card"]["workspace_path"] == str(repo)
    with kb.connect_closing(board="default") as conn:
        task = kb.get_task(conn, started["card"]["id"])
    assert task is not None
    resolved = kb.resolve_workspace(task, board="default")
    assert resolved == repo / ".worktrees" / task.id
    assert (resolved / ".git").exists()


def test_explicit_worktree_without_git_anchor_is_rejected_before_card_creation(
    project_control_home, tmp_path
):
    project_dir = tmp_path / "not-a-repo"
    project_dir.mkdir()
    started = controller.start_project(
        name="Invalid worktree",
        goal="Create a safely classified first card",
        shell_key="code",
        primary_path=str(project_dir),
    )
    assert started["card"]["workspace_kind"] == "dir"
    before = len(controller.inspect_project(started["project"]["id"])["cards"])
    with pytest.raises(
        controller.ProjectCardControllerError,
        match="no Git repository anchor",
    ):
        controller.add_project_card(
            started["project"]["id"],
            title="Must fail before dispatch",
            shell_key="code",
            workspace_kind="worktree",
        )
    assert len(controller.inspect_project(started["project"]["id"])["cards"]) == before


def test_recovery_is_nonblocking_and_project_close_is_guarded(project_control_home):
    started = controller.start_project(
        name="Recovery",
        goal="Fail safely",
        shell_key="operations",
    )
    project_id = started["project"]["id"]
    root_id = started["card"]["id"]
    _mark_status(root_id, "blocked")

    workspace = project_control_home / "existing-project-dir"
    workspace.mkdir()
    recovered = controller.recover_card(
        root_id,
        workspace_kind="dir",
        workspace_path=str(workspace),
    )["card"]
    assert recovered["status"] == "ready"
    assert recovered["workspace_kind"] == "dir"
    assert recovered["workspace_path"] == str(workspace)
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
    with pytest.raises(controller.ProjectCardControllerError, match="reopen"):
        controller.add_project_card(
            project_id,
            title="Must not silently reopen either",
            shell_key="operations",
        )

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
    second = json.loads(
        supervisor_tools._handle_project_cards(
            {
                "action": "add_project_card",
                "project_id": payload["project"]["id"],
                "title": "Start a separate reporting thread",
                "shell_key": "operations",
            },
            session_id="telegram-session-1",
        )
    )
    assert second["card"]["root_task_id"] == second["card"]["id"]

    _mark_status(payload["card"]["id"], "blocked")
    workspace = project_control_home / "supervisor-recovery-dir"
    workspace.mkdir()
    recovered = json.loads(
        supervisor_tools._handle_project_cards(
            {
                "action": "recover_card",
                "card_id": payload["card"]["id"],
                "workspace_kind": "dir",
                "workspace_path": str(workspace),
            },
            session_id="telegram-session-1",
        )
    )["card"]
    assert recovered["workspace_kind"] == "dir"
    assert recovered["workspace_path"] == str(workspace)

    summary = controller.inspect_project(payload["project"]["id"])
    assert [card["id"] for card in summary["cards"]] == [
        payload["card"]["id"],
        second["card"]["id"],
        recovered["id"],
    ]


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
