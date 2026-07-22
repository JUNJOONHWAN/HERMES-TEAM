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


def _approve_code_response(response: dict) -> dict:
    assert response["card"] is None
    assert response["approval_required"] is True
    approval = response["approval_request"]
    assert approval["status"] == "pending"
    return controller.approve_code_card(
        approval["id"], decided_by="test-operator"
    )["card"]


def test_controller_owns_project_and_card_thread_lifecycle(project_control_home):
    started = controller.start_project(
        name="Hermes Control",
        goal="Implement the deterministic controller",
        shell_key="code",
        acceptance_criteria=["Controller tests pass", "Evidence is recorded"],
        input_refs=["spec:controller-v1"],
    )
    project = started["project"]
    root = _approve_code_response(started)
    assert controller.inspect_project(project["id"])["project"]["status"] == "active"

    assert started["schema"] == controller.SCHEMA
    assert project["status"] == "paused"
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

    root = _approve_code_response(started)
    assert root["workspace_kind"] == "dir"
    assert root["workspace_path"] == str(project_dir)
    follow = controller.continue_card(
        root["id"],
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

    root = _approve_code_response(started)
    assert root["workspace_kind"] == "worktree"
    assert root["workspace_path"] == str(repo)
    with kb.connect_closing(board="default") as conn:
        task = kb.get_task(conn, root["id"])
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
    root = _approve_code_response(started)
    assert root["workspace_kind"] == "dir"
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


def test_code_card_requires_operator_approval_and_deduplicates(
    project_control_home,
):
    started = controller.start_project(
        name="Approval gate",
        goal="Create guarded code work",
        shell_key="operations",
    )
    project_id = started["project"]["id"]

    first = controller.add_project_card(
        project_id,
        title="Implement M1",
        shell_key="code",
        milestone="M1",
    )
    replay = controller.add_project_card(
        project_id,
        title="Implement M1",
        shell_key="code",
        milestone="M1",
    )

    assert first["card"] is None
    assert first["approval_required"] is True
    assert replay["approval_request"]["id"] == first["approval_request"]["id"]
    summary = controller.inspect_project(project_id)
    assert summary["project"]["status"] == "paused"
    assert summary["progress"]["phase"] == "awaiting_code_approval"
    assert summary["progress"]["milestone"] == "M1"
    assert [item["id"] for item in summary["approval_requests"]] == [
        first["approval_request"]["id"]
    ]
    assert len(summary["cards"]) == 1

    approved = controller.approve_code_card(
        first["approval_request"]["id"],
        decided_by="test-operator",
    )
    assert approved["card"]["project_id"] == project_id
    assert approved["approval_request"]["status"] == "consumed"
    assert controller.inspect_project(project_id)["project"]["status"] == "active"
    repeated = controller.approve_code_card(
        first["approval_request"]["id"],
        decided_by="test-operator",
    )
    assert repeated["idempotent_replay"] is True
    assert repeated["card"]["id"] == approved["card"]["id"]


def test_rejecting_code_card_never_creates_task(project_control_home):
    started = controller.start_project(
        name="Reject gate",
        goal="Operations root",
        shell_key="operations",
    )
    requested = controller.add_project_card(
        started["project"]["id"],
        title="Do not create this code card",
        shell_key="code",
    )
    rejected = controller.reject_code_card(
        requested["approval_request"]["id"],
        decided_by="test-operator",
        decision_reason="not in scope",
    )

    assert rejected["card"] is None
    assert rejected["approval_request"]["status"] == "rejected"
    summary = controller.inspect_project(started["project"]["id"])
    assert len(summary["cards"]) == 1
    assert summary["project"]["status"] == "active"


def test_direction_change_stops_source_and_waits_for_successor_approval(
    project_control_home,
):
    started = controller.start_project(
        name="Audited direction change",
        goal="Implement the original direction",
        shell_key="operations",
    )
    project_id = started["project"]["id"]
    source = started["card"]
    _mark_status(source["id"], "running")

    requested = controller.request_direction_change(
        source["id"],
        title="Implement the corrected direction",
        reason="Acceptance criteria changed",
        body="Preserve the useful work and replace the obsolete path.",
        shell_key="operations",
        acceptance_criteria=["Corrected path is verified"],
        created_by="test-operator",
    )

    assert requested["project_id"] == project_id
    assert requested["source_status"] == "archived"
    assert requested["successor_card"] is None
    assert requested["approval_required"] is True
    assert requested["checkpoint"]["status"] == "not_applicable"
    approval = requested["approval_request"]
    assert approval["action"] == "direction_change"
    assert approval["shell_key"] == "operations"
    assert approval["request"]["relation_type"] == "references"
    assert approval["request"]["direction_change"]["reason"] == (
        "Acceptance criteria changed"
    )

    pending = controller.inspect_project(project_id)
    assert pending["project"]["status"] == "paused"
    assert pending["progress"]["phase"] == "awaiting_direction_change_approval"
    assert [card["id"] for card in pending["cards"]] == [source["id"]]
    assert pending["cards"][0]["status"] == "archived"

    approved = controller.approve_project_card(
        approval["id"], decided_by="test-operator"
    )
    assert approved["action"] == "approve_project_card"
    successor = approved["card"]
    assert successor["project_id"] == project_id
    assert successor["root_task_id"] == source["id"]
    assert "Acceptance criteria changed" in successor["body"]
    incoming = controller.inspect_card(successor["id"])["links"]["incoming"]
    assert incoming == [
        {
            "task_id": source["id"],
            "relation_type": "references",
            "blocking": False,
        }
    ]
    assert controller.inspect_project(project_id)["project"]["status"] == "active"


def test_generic_card_pause_resume_and_steer_reuse_same_card(
    project_control_home,
):
    with kb.connect_closing(board="default") as conn:
        card_id = kb.create_task(
            conn,
            title="Ordinary resumable card",
            body="Keep the original work visible.",
            assignee="worker",
        )

    paused = controller.pause_card(
        card_id,
        reason="operator requested stop",
        created_by="test-operator",
    )
    assert paused["same_card_id"] == card_id
    assert paused["state"] == "paused"
    assert paused["card"]["status"] == "blocked"
    assert paused["card"]["block_kind"] == kb.OPERATOR_PAUSE_BLOCK_KIND

    resumed = controller.resume_card(
        card_id,
        reason="continue the original work",
        created_by="test-operator",
    )
    assert resumed["same_card_id"] == card_id
    assert resumed["state"] == "queued_to_resume"
    assert resumed["card"]["status"] == "ready"
    assert resumed["card"]["block_kind"] is None

    steered = controller.steer_card(
        card_id,
        instruction="Verify source B before drawing the conclusion.",
        created_by="test-operator",
    )
    assert steered["same_card_id"] == card_id
    assert steered["state"] == "queued_with_steering"
    assert steered["card"]["status"] == "ready"
    assert steered["control"]["paused_first"] is True
    with kb.connect_closing(board="default") as conn:
        context = kb.build_worker_context(conn, card_id)
    assert "Verify source B before drawing the conclusion." in context
    assert "Operator steering for the next run" in context


def test_supervisor_handler_controls_generic_card_without_project(
    project_control_home,
):
    with kb.connect_closing(board="default") as conn:
        card_id = kb.create_task(
            conn,
            title="Controller ordinary card",
            assignee="worker",
        )

    paused = json.loads(
        supervisor_tools._handle_project_cards(
            {
                "action": "pause_card",
                "card_id": card_id,
                "reason": "stop now",
            }
        )
    )
    assert paused["action"] == "pause_card"
    assert paused["same_card_id"] == card_id

    steered = json.loads(
        supervisor_tools._handle_project_cards(
            {
                "action": "steer_card",
                "card_id": card_id,
                "instruction": "Use the corrected acceptance check.",
            }
        )
    )
    assert steered["action"] == "steer_card"
    assert steered["same_card_id"] == card_id
    assert steered["card"]["status"] == "ready"


def test_direction_change_waits_for_confirmed_worker_termination(
    project_control_home,
):
    started = controller.start_project(
        name="Remote worker direction change",
        goal="Do not checkpoint while the previous worker can still write",
        shell_key="operations",
    )
    project_id = started["project"]["id"]
    source = started["card"]
    with kb.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', worker_pid = ?, "
                "claim_lock = ? WHERE id = ?",
                (999999, "foreign-host:direction", source["id"]),
            )

    with pytest.raises(
        controller.ProjectCardControllerError,
        match="worker termination is still pending",
    ):
        controller.request_direction_change(
            source["id"],
            title="Replacement after a confirmed stop",
            reason="The acceptance contract changed",
        )

    with kb.connect_closing(board="default") as conn:
        stopped = kb.get_task(conn, source["id"])
    assert stopped is not None
    assert stopped.status == "archived"
    assert stopped.worker_pid == 999999
    assert controller.inspect_project(project_id)["project"]["status"] == "paused"
    with pdb.connect_closing() as conn:
        assert pdb.list_card_approvals(conn, project_id=project_id) == []


def test_rejected_direction_change_keeps_source_archived_without_successor(
    project_control_home,
):
    started = controller.start_project(
        name="Rejected direction change",
        goal="Run the original task",
        shell_key="operations",
    )
    source = started["card"]
    requested = controller.request_direction_change(
        source["id"],
        title="Unapproved replacement",
        reason="Operator is considering another scope",
    )
    rejected = controller.reject_project_card(
        requested["approval_request"]["id"],
        decided_by="test-operator",
        decision_reason="keep the project stopped",
    )

    assert rejected["card"] is None
    assert rejected["action"] == "reject_project_card"
    summary = controller.inspect_project(started["project"]["id"])
    assert len(summary["cards"]) == 1
    assert summary["cards"][0]["id"] == source["id"]
    assert summary["cards"][0]["status"] == "archived"
    assert summary["project"]["status"] == "active"


def test_operator_pause_is_a_durable_card_creation_gate(project_control_home):
    started = controller.start_project(
        name="Paused project",
        goal="Operations root",
        shell_key="operations",
    )
    project_id = started["project"]["id"]
    paused = controller.pause_project(project_id)

    assert paused["project"]["status"] == "paused"
    assert paused["progress"]["phase"] == "paused"
    with pytest.raises(controller.ProjectCardControllerError, match="paused"):
        controller.add_project_card(
            project_id,
            title="Must not issue while paused",
            shell_key="operations",
        )
    reopened = controller.reopen_project(project_id)
    assert reopened["project"]["status"] == "active"


def test_repository_init_and_card_branch_commit_push_are_project_audited(
    project_control_home, tmp_path
):
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
    )
    started = controller.start_project(
        name="Git governed project",
        goal="Implement one isolated change",
        shell_key="code",
        primary_path=str(repo),
        repo_mode="init_local",
        milestone="M1",
    )
    assert started["repository"]["status"] == "ready"
    assert (repo / ".git").exists()
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Hermes Tests"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    card = _approve_code_response(started)
    with kb.connect_closing(board="default") as conn:
        task = kb.get_task(conn, card["id"])
    workspace = kb.resolve_workspace(task, board="default")
    (workspace / "feature.txt").write_text("done\n", encoding="utf-8")
    checkpoint = controller.checkpoint_card_git(card["id"], push=True)

    assert checkpoint["pushed"] is True
    assert checkpoint["branch"].startswith("git-governed-project/")
    assert checkpoint["repository"]["last_commit_sha"] == checkpoint["sha"]
    assert checkpoint["repository"]["last_pushed_sha"] == checkpoint["sha"]
    remote_branches = subprocess.run(
        ["git", "--git-dir", str(remote), "branch", "--list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert checkpoint["branch"] in remote_branches
    summary = controller.inspect_project(started["project"]["id"])
    assert summary["git_events"][0]["event_type"] == "card_branch_pushed"

    direction = controller.request_direction_change(
        card["id"],
        title="Replace the committed implementation direction",
        reason="The integration contract changed",
    )
    assert direction["checkpoint"]["status"] == "committed"
    assert direction["checkpoint"]["sha"] == checkpoint["sha"]
    assert controller.inspect_card(card["id"])["card"]["status"] == "archived"
    assert direction["approval_request"]["status"] == "pending"


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
