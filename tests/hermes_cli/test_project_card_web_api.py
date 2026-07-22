from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_cli import supervisor_registry as registry


@pytest.fixture()
def project_api_client(monkeypatch, tmp_path):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    home = tmp_path / "project-api-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()
    with kb.connect_closing(board="default") as conn:
        for shell_key in ("code", "verification"):
            registry.register_shell_version(
                conn,
                shell_key=shell_key,
                name=shell_key.title(),
                contract={"allowed_adapters": ["codex"]},
                allowed_capabilities=["terminal"],
                evidence_policy={},
            )

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    yield client
    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()


def test_web_api_uses_same_native_project_card_controller(
    project_api_client, tmp_path
):
    started = project_api_client.post(
        "/api/plugins/kanban/projects/start?board=default",
        json={
            "name": "Public Control",
            "goal": "Expose card threads in the Kanban UI",
            "shell_key": "code",
            "acceptance_criteria": ["API and UI agree"],
        },
    )
    assert started.status_code == 200, started.text
    body = started.json()
    project_id = body["project"]["id"]
    root_id = body["card"]["id"]

    projects = project_api_client.get("/api/plugins/kanban/projects")
    assert projects.status_code == 200
    assert projects.json()["projects"][0]["id"] == project_id

    independent = project_api_client.post(
        f"/api/plugins/kanban/projects/{project_id}/cards",
        json={
            "title": "Start an independent verification stream",
            "shell_key": "verification",
            "acceptance_criteria": ["Independent root is preserved"],
        },
    )
    assert independent.status_code == 200, independent.text
    independent_card = independent.json()["card"]
    assert independent_card["root_task_id"] == independent_card["id"]

    follow = project_api_client.post(
        f"/api/plugins/kanban/cards/{root_id}/continue",
        json={"title": "Add an intuitive follow-up action"},
    )
    assert follow.status_code == 200, follow.text
    follow_id = follow.json()["card"]["id"]

    inspected = project_api_client.get(
        f"/api/plugins/kanban/cards/{follow_id}/inspect"
    )
    assert inspected.status_code == 200
    assert inspected.json()["links"]["incoming"][0] == {
        "task_id": root_id,
        "relation_type": "follows",
        "blocking": True,
    }

    with kb.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?", (root_id,)
            )
    workspace = tmp_path / "dashboard-recovery-dir"
    workspace.mkdir()
    recovered = project_api_client.post(
        f"/api/plugins/kanban/cards/{root_id}/recover",
        json={
            "workspace_kind": "dir",
            "workspace_path": str(workspace),
        },
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["card"]["workspace_kind"] == "dir"
    assert recovered.json()["card"]["workspace_path"] == str(workspace)

    # Closing remains a controller invariant, not a UI-side optimistic flag.
    closed = project_api_client.post(
        f"/api/plugins/kanban/projects/{project_id}/close"
    )
    assert closed.status_code == 409
    assert "open cards" in closed.json()["detail"]


def test_web_bundle_exposes_independent_root_card_action():
    bundle = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "dist"
        / "index.js"
    ).read_text(encoding="utf-8")

    assert "/projects/${encodeURIComponent(projectId)}/cards" in bundle
    assert "+ New root card" in bundle
