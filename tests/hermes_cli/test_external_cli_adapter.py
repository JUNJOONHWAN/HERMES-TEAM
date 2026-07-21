from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli import executor_adapter
from hermes_cli import external_cli_adapter as external
from hermes_cli import kanban_db as kb
from hermes_cli import supervisor_registry as registry


def test_external_cli_bridge_closes_market_card_with_optional_policy_and_receipt(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kb.connect(db_path)
    shell = registry.register_shell_version(
        conn,
        shell_key="market",
        name="Market",
        contract={
            "allowed_adapters": ["command"],
            "instructions": "market only",
        },
        required_capabilities=(
            "kanban",
            "hermes-timeline-code-map",
            "web",
        ),
        allowed_capabilities=(
            "kanban",
            "hermes-timeline-code-map",
            "web",
        ),
        evidence_policy={
            "timeline_required": True,
            "code_slice_required": False,
            "verify_all_invalid_count": 0,
            "outputs_required": True,
        },
    )
    engine = registry.register_executor(
        conn,
        executor_id="executor_external",
        name="external",
        adapter_type="command",
        launch_config={
            "argv": ["bridge", "{prompt_file}"],
            "capability_enforcement": "env",
        },
        capabilities=(
            "kanban",
            "hermes-timeline-code-map",
            "web",
        ),
        heartbeat_required=False,
    )
    binding = registry.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=engine.id,
        responsibility="primary",
    )
    task_id = kb.create_task(conn, title="market card", role_shell_id=shell.id)
    task = kb.claim_task(conn, task_id)
    run = conn.execute(
        "SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)
    ).fetchone()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt_dir = workspace / ".hermes-supervisor"
    prompt_dir.mkdir()
    prompt_file = prompt_dir / "assignment.md"
    prompt_file.write_text("Analyze the market.", encoding="utf-8")
    selection = registry.Selection(
        shell=shell,
        executor=engine,
        binding=binding,
        effective_capabilities=[
            "hermes-timeline-code-map",
            "kanban",
            "web",
        ],
        active_runs=1,
    )
    worker_prompt = executor_adapter._worker_prompt(conn, task, selection)
    assert "external command adapter does not expose kanban_complete" in worker_prompt
    assert "Do not add a warning that a close tool is unavailable" in worker_prompt
    assert "submit a receipt through kanban_complete" not in worker_prompt
    environ = executor_adapter._binding_env(
        task, run, selection, "default", str(workspace)
    )
    policy = tmp_path / "research_policy.json"
    policy.write_text(
        json.dumps(
            {
                "source_priority": ["official", "public-cross-check"],
                "forbidden_actions": ["trade-write"],
            }
        ),
        encoding="utf-8",
    )
    node_ids = iter(["node-action", "node-output"])

    def fake_timeline(_args, operation, *_operation_args):
        if operation == "record":
            return {"node_id": next(node_ids)}
        if operation == "verify-all":
            return {"invalid_count": 0, "total": 10}
        return {"status": "ok"}

    monkeypatch.setattr(external, "_timeline", fake_timeline)
    monkeypatch.setattr(
        external,
        "_run_engine",
        lambda *_args, **_kwargs: (0, "Verified market result", ""),
    )
    args = SimpleNamespace(
        prompt_file=str(prompt_file),
        engine_name="test-engine",
        engine_argv_json='["unused", "{prompt_file}"]',
        engine_timeout_seconds=30,
        timeline_client="unused",
        timeline_python="unused",
        timeline_db=None,
        timeline_timeout_seconds=30,
        research_policy=str(policy),
    )

    result = external.run(args, environ=environ)

    assert result["status"] == "completed"
    closed = kb.get_task(conn, task_id)
    assert closed.status == "done"
    assert closed.result == "Verified market result"
    receipt_row = conn.execute(
        "SELECT receipt_json FROM run_receipts WHERE task_id=?", (task_id,)
    ).fetchone()
    stored_receipt = json.loads(receipt_row["receipt_json"])
    assert stored_receipt["timeline"]["node_ids"] == [
        "node-action",
        "node-output",
    ]
    assert stored_receipt["outputs"][1]["value"]["path"] == str(policy.resolve())
    assert stored_receipt["outputs"][1]["kind"] == "research_policy_preflight"
    assert stored_receipt["timeline"]["verify_all"]["verified_count"] == 10
