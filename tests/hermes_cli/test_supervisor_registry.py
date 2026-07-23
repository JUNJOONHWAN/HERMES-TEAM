from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
import yaml

from hermes_cli import kanban_db as kb
from hermes_cli import executor_adapter as ea
from hermes_cli import supervisor_bootstrap as bootstrap
from hermes_cli import supervisor_cli
from hermes_cli import supervisor_registry as sr
from scripts import hermes_supervisor_heartbeat as heartbeat_script
from tools import supervisor_tools


def _conn(tmp_path):
    return kb.connect(tmp_path / "kanban.db")


def _shell(conn, key="code", allowed=("file", "terminal", "kanban")):
    return sr.register_shell_version(
        conn,
        shell_key=key,
        name=key.title(),
        contract={
            "allowed_adapters": ["hermes_profile", "command"],
            "instructions": f"Perform {key} work only through this role.",
        },
        required_capabilities=("kanban",),
        allowed_capabilities=allowed,
        evidence_policy={"timeline_required": True, "code_slice_required": True},
    )


def _executor(conn, executor_id="executor_real", *, capacity=2, heartbeat=True):
    item = sr.register_executor(
        conn,
        executor_id=executor_id,
        name=executor_id,
        adapter_type="hermes_profile",
        launch_config={"profile": "default"},
        capabilities=("file", "terminal", "kanban", "web"),
        capacity=capacity,
        heartbeat_required=heartbeat,
    )
    if heartbeat:
        assert sr.heartbeat_executor(conn, item.id)
    return sr.get_executor(conn, item.id)


def _valid_receipt(run_id, task_id, shell_id, executor_id, binding_id):
    return {
        "run_id": run_id,
        "task_id": task_id,
        "role_shell_id": shell_id,
        "executor_id": executor_id,
        "binding_id": binding_id,
        "outputs": [{"kind": "test", "value": "passed"}],
        "timeline": {
            "goal_id": sr.timeline_goal_id(task_id, run_id),
            "context_loaded": True,
            "neural_recall": {
                "performed": True,
                "query": "receipt task",
                "candidate_count": 0,
                "context_chars": 0,
            },
            "slice_ids": ["slice-1"],
            "node_ids": ["node-1", "node-2"],
            "verify_all": {"invalid_count": 0, "verified_count": 2},
        },
    }


def test_shell_versions_are_immutable_and_keep_lineage(tmp_path):
    conn = _conn(tmp_path)
    first = _shell(conn)
    second = sr.register_shell_version(
        conn,
        shell_key="code",
        name="Code v2",
        contract={"allowed_adapters": ["hermes_profile"]},
        required_capabilities=("kanban",),
        allowed_capabilities=("file", "kanban"),
        evidence_policy={"timeline_required": True},
    )

    assert second.version == 2
    assert second.supersedes_shell_id == first.id
    assert sr.get_shell(conn, shell_key="code").id == second.id
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE role_shells SET name='mutated' WHERE id=?", (first.id,))


def test_many_to_many_bindings_allow_executor_reuse_and_shell_candidates(tmp_path):
    conn = _conn(tmp_path)
    code = _shell(conn, "code")
    research = _shell(conn, "research", allowed=("web", "kanban"))
    shared = _executor(conn, "executor_shared")
    backup = _executor(conn, "executor_backup")

    b1 = sr.bind_executor(conn, shell_id=code.id, executor_id=shared.id, priority=10)
    b2 = sr.bind_executor(conn, shell_id=research.id, executor_id=shared.id, priority=10)
    b3 = sr.bind_executor(conn, shell_id=code.id, executor_id=backup.id, priority=1)

    assert {b.executor_id for b in sr.list_bindings(conn, shell_id=code.id)} == {
        shared.id,
        backup.id,
    }
    assert {b.shell_id for b in sr.list_bindings(conn, executor_id=shared.id)} == {
        code.id,
        research.id,
    }
    assert {b1.id, b2.id, b3.id}


def test_hermes_maintainer_runtime_and_replacement_certification_fail_closed(tmp_path):
    conn = _conn(tmp_path)
    runtime = {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "codex_app_server",
        "reasoning_effort": "high",
    }
    shell = sr.register_shell_version(
        conn,
        shell_key="hermes-repair",
        name="Hermes Maintainer",
        contract={
            "allowed_adapters": ["command"],
            "runtime_requirements": runtime,
            "replacement_gate": {
                "baseline_executor_id": "executor_baseline",
                "baseline_runtime": runtime,
                "minimum_cases": 20,
                "minimum_overall_pass_rate": 0.95,
                "critical_pass_rate": 1.0,
                "benchmark_suite": "hermes-repair-v1",
                "minimum_baseline_score_ratio": 0.95,
                "maximum_median_latency_ratio": 1.5,
                "require_benchmark_artifact": True,
                "maximum_default_branch_writes": 0,
                "require_operator_approval": True,
                "require_conflicting_adapter_result_case": True,
                "require_config_and_source_cases": True,
                "require_branch_push_and_rollback": True,
                "require_timeline_invalid_count": 0,
            },
        },
        required_capabilities=("file", "terminal", "kanban"),
        allowed_capabilities=("file", "terminal", "kanban"),
        evidence_policy={"timeline_required": True},
    )
    baseline = sr.register_executor(
        conn,
        executor_id="executor_baseline",
        name="baseline",
        adapter_type="command",
        launch_config={
            **runtime,
            "argv": ["true", "{prompt_file}"],
            "capability_enforcement": "env",
        },
        capabilities=("file", "terminal", "kanban"),
        heartbeat_required=False,
    )
    assert sr.assign_adapter(
        conn, shell_value=shell.id, executor_value=baseline.id
    ).executor_id == baseline.id

    uncertified = sr.register_executor(
        conn,
        executor_id="executor_candidate",
        name="candidate",
        adapter_type="command",
        launch_config={
            **runtime,
            "argv": ["true", "{prompt_file}"],
            "capability_enforcement": "env",
        },
        capabilities=("file", "terminal", "kanban"),
        heartbeat_required=False,
    )
    with pytest.raises(sr.SupervisorRegistryError, match="certification is required"):
        sr.assign_adapter(
            conn, shell_value=shell.id, executor_value=uncertified.id
        )

    certified_launch = {
        **runtime,
        "argv": ["true", "{prompt_file}"],
        "capability_enforcement": "env",
        "hermes_repair_certification": {
            "operator_approved": True,
            "case_count": 20,
            "overall_pass_rate": 0.95,
            "critical_pass_rate": 1.0,
            "default_branch_writes": 0,
            "timeline_invalid_count": 0,
            "baseline_score_ratio": 0.98,
            "median_latency_ratio": 1.1,
            "benchmark_suite": "hermes-repair-v1",
            "benchmark_report_sha256": "a" * 64,
            "evaluated_executor_id": "executor_certified",
            "conflicting_adapter_result_case": True,
            "config_and_source_cases": True,
            "branch_push_and_rollback": True,
        },
    }
    certified = sr.register_executor(
        conn,
        executor_id="executor_certified",
        name="certified",
        adapter_type="command",
        launch_config=certified_launch,
        capabilities=("file", "terminal", "kanban"),
        heartbeat_required=False,
    )
    assert sr.assign_adapter(
        conn, shell_value=shell.id, executor_value=certified.id
    ).executor_id == certified.id

    alternate_runtime = sr.register_executor(
        conn,
        executor_id="executor_wrong_model",
        name="wrong-model",
        adapter_type="command",
        launch_config={
            **certified_launch,
            "model": "alternate-maintainer-model",
            "hermes_repair_certification": {
                **certified_launch["hermes_repair_certification"],
                "evaluated_executor_id": "executor_wrong_model",
            },
        },
        capabilities=("file", "terminal", "kanban"),
        heartbeat_required=False,
    )
    assert sr.assign_adapter(
        conn, shell_value=shell.id, executor_value=alternate_runtime.id
    ).executor_id == alternate_runtime.id


def test_primary_owner_precedes_higher_priority_candidates(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    candidate = _executor(conn, "executor_candidate")
    primary = _executor(conn, "executor_primary")
    sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=candidate.id,
        priority=999,
        responsibility="candidate",
    )
    sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=primary.id,
        priority=1,
        responsibility="primary",
    )

    assert sr.select_binding(conn, shell.id).executor.id == primary.id


def test_assign_adapter_changes_primary_and_keeps_auditable_candidates(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    first = _executor(conn, "executor_first")
    second = _executor(conn, "executor_second")
    sr.bind_executor(
        conn, shell_id=shell.id, executor_id=first.id,
        priority=100, responsibility="primary",
    )
    sr.bind_executor(
        conn, shell_id=shell.id, executor_id=second.id,
        priority=1, responsibility="candidate",
    )

    changed = sr.assign_adapter(
        conn,
        shell_value="code",
        executor_value=second.id,
        responsibility="primary",
        note="operator promotion",
        assigned_by="test",
    )

    assert changed.responsibility == "primary"
    ownership = {b.executor_id: b.responsibility for b in sr.list_bindings(conn)}
    assert ownership == {first.id: "candidate", second.id: "primary"}
    assert sr.select_binding(conn, shell.id).executor.id == second.id
    assert sr.list_adapter_events(conn)[0]["kind"] == "adapter_assigned"


def test_task_override_precedes_shell_and_all_then_consumes_at_card_completion(
    tmp_path,
):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    general = _executor(conn, "executor_general", capacity=4)
    role = _executor(conn, "executor_role", capacity=4)
    task_exec = _executor(conn, "executor_task", capacity=4)
    for executor, priority in ((general, 100), (role, 50), (task_exec, 1)):
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=executor.id,
            priority=priority,
        )
    task_id = kb.create_task(conn, title="forced", role_shell_id=shell.id)
    sr.create_adapter_override(
        conn,
        target="all",
        executor_value=general.id,
        mode="permanent",
        created_by="test",
    )
    sr.create_adapter_override(
        conn,
        target="code",
        executor_value=role.id,
        mode="temporary",
        duration_seconds=3600,
        created_by="test",
    )
    task_override = sr.create_adapter_override(
        conn,
        target=task_id,
        executor_value=task_exec.id,
        mode="once",
        created_by="test",
    )

    assert sr.select_binding(conn, shell.id, task_id=task_id).executor.id == task_exec.id
    assert kb.claim_task(conn, task_id) is not None
    run = kb.latest_run(conn, task_id)
    assert run.executor_id == task_exec.id
    assert run.adapter_override_id == task_override.id
    assert sr.get_adapter_override(conn, task_override.id).active() is True
    assert kb.complete_task(
        conn,
        task_id,
        summary="forced adapter completed",
        expected_run_id=run.id,
        receipt=_valid_receipt(
            run.id, task_id, shell.id, task_exec.id, run.binding_id
        ),
    )
    assert sr.get_adapter_override(conn, task_override.id).active() is False
    assert any(
        event["kind"] == "adapter_override_selected"
        for event in sr.list_adapter_events(conn, task_id=task_id)
    )
    assert any(
        event["kind"] == "adapter_override_consumed"
        for event in sr.list_adapter_events(conn, task_id=task_id)
    )


def test_task_once_override_survives_failure_and_pins_retry(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    primary = _executor(conn, "executor_primary", capacity=4)
    forced = _executor(conn, "executor_forced", capacity=4)
    primary_binding = sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=primary.id,
        priority=100,
    )
    forced_binding = sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=forced.id,
        priority=1,
    )
    task_id = kb.create_task(conn, title="retry pinned", role_shell_id=shell.id)
    override = sr.create_adapter_override(
        conn,
        target=task_id,
        executor_value=forced.id,
        mode="once",
        created_by="test",
    )

    assert kb.claim_task(conn, task_id) is not None
    first_run = kb.latest_run(conn, task_id)
    assert first_run.executor_id == forced.id
    assert first_run.binding_id == forced_binding.id
    assert kb._record_task_failure(
        conn,
        task_id,
        "worker crashed before completion",
        outcome="spawn_failed",
        failure_limit=3,
        release_claim=True,
        end_run=True,
    ) is False
    assert sr.get_adapter_override(conn, override.id).active() is True

    assert kb.claim_task(conn, task_id) is not None
    retry_run = kb.latest_run(conn, task_id)
    assert retry_run.id != first_run.id
    assert retry_run.executor_id == forced.id
    assert retry_run.binding_id == forced_binding.id
    assert retry_run.binding_id != primary_binding.id
    assert retry_run.adapter_override_id == override.id

    assert kb.complete_task(
        conn,
        task_id,
        summary="retry completed on forced adapter",
        expected_run_id=retry_run.id,
        receipt=_valid_receipt(
            retry_run.id,
            task_id,
            shell.id,
            forced.id,
            forced_binding.id,
        ),
    )
    assert sr.get_adapter_override(conn, override.id).active() is False


def test_task_once_override_is_consumed_when_card_is_archived(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn, "executor_archive", capacity=2)
    sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="archive pinned", role_shell_id=shell.id)
    override = sr.create_adapter_override(
        conn,
        target=task_id,
        executor_value=executor.id,
        mode="once",
        created_by="test",
    )

    assert sr.get_adapter_override(conn, override.id).active() is True
    assert kb.archive_task(conn, task_id) is True
    assert sr.get_adapter_override(conn, override.id).active() is False


def test_forced_ineligible_adapter_fails_closed_without_fallback(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    primary = _executor(conn, "executor_primary")
    forced = _executor(conn, "executor_forced")
    sr.bind_executor(
        conn, shell_id=shell.id, executor_id=primary.id,
        priority=100, responsibility="primary",
    )
    sr.bind_executor(conn, shell_id=shell.id, executor_id=forced.id, priority=1)
    task_id = kb.create_task(conn, title="no fallback", role_shell_id=shell.id)
    sr.create_adapter_override(
        conn,
        target=task_id,
        executor_value=forced.id,
        mode="once",
        created_by="test",
    )
    assert sr.set_executor_enabled(conn, forced.id, False)

    assert kb.claim_task(conn, task_id) is None
    assert kb.get_task(conn, task_id).status == "ready"
    with pytest.raises(sr.NoEligibleExecutor, match="fallback is disabled"):
        sr.select_binding(conn, shell.id, task_id=task_id)


def test_completed_card_reissue_preserves_original_and_creates_revision(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    first = _executor(conn, "executor_first")
    second = _executor(conn, "executor_second")
    first_binding = sr.bind_executor(
        conn, shell_id=shell.id, executor_id=first.id,
        priority=100, responsibility="primary",
    )
    sr.bind_executor(conn, shell_id=shell.id, executor_id=second.id, priority=1)
    task_id = kb.create_task(conn, title="finished work", role_shell_id=shell.id)
    assert kb.claim_task(conn, task_id) is not None
    run = kb.latest_run(conn, task_id)
    receipt = _valid_receipt(
        run.id, task_id, shell.id, first.id, first_binding.id
    )
    assert kb.complete_task(
        conn, task_id, summary="first pass", expected_run_id=run.id, receipt=receipt
    )

    result = sr.reissue_task_with_adapter(
        conn,
        task_id=task_id,
        executor_value=second.id,
        reason="learn again",
        created_by="test",
    )
    revision_id = result["revision_task_id"]
    assert kb.get_task(conn, task_id).status == "done"
    assert kb.get_task(conn, revision_id).status == "ready"
    assert kb.parent_ids(conn, revision_id) == [task_id]
    inspection = sr.inspect_task_adapter(conn, revision_id)
    assert inspection["effective_override"]["executor_id"] == second.id
    assert inspection["effective_override"]["mode"] == "once"
    assert inspection["effective_override"]["remaining_uses"] == 1
    assert result["override_id"] == inspection["effective_override"]["override_id"]
    assert any(
        event.kind == "adapter_rerun_requested"
        for event in kb.list_events(conn, task_id)
    )


def test_adapter_view_exposes_controller_plus_seven_runtime_slots(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes-home"
    profile_home = home / "profiles" / "worker-vllm"
    profile_home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "openai_runtime": "codex_app_server",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                "agent": {"reasoning_effort": "medium"},
            }
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "Qwen3-Coder-32B",
                    "provider": "vllm",
                    "base_url": "http://127.0.0.1:8000/v1?token=do-not-expose",
                },
                "agent": {"reasoning_effort": "high"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    conn = _conn(tmp_path)
    executor = sr.register_executor(
        conn,
        executor_id="executor_vllm",
        name="worker-vllm",
        adapter_type="hermes_profile",
        launch_config={"profile": "worker-vllm"},
        capabilities=("file", "terminal", "kanban", "web"),
        heartbeat_required=False,
    )
    for key in (
        "code", "operations", "market", "browser-research", "report",
        "verification", "tool-management",
    ):
        shell = _shell(conn, key, allowed=("file", "terminal", "kanban", "web"))
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=executor.id,
            responsibility="primary",
        )

    view = sr.adapter_registry_view(conn)

    assert view["schema"] == "hermes.supervisor.adapters.v2"
    assert view["control_slot_count"] == len(view["control_slots"]) == 8
    assert [slot["slot_key"] for slot in view["control_slots"]] == [
        "hermes", "browser-research", "code", "market", "operations", "report",
        "tool-management", "verification",
    ]
    controller = view["controller"]
    assert controller["delegation_only"] is True
    assert controller["runtime"]["backend"] == "codex_app_server"
    assert controller["runtime"]["model"] == "gpt-5.6-sol"
    assert controller["runtime"]["reasoning_effort"] == "medium"
    code_slot = next(slot for slot in view["control_slots"] if slot["slot_key"] == "code")
    assert code_slot["runtime"]["backend_label"] == "vLLM"
    assert code_slot["runtime"]["model"] == "Qwen3-Coder-32B"
    assert code_slot["runtime"]["reasoning_effort"] == "high"
    assert code_slot["runtime"]["endpoint"] == "127.0.0.1:8000"
    assert "do-not-expose" not in json.dumps(view)


def test_controller_adapters_switch_once_audit_and_fall_back_to_codex(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "openai_runtime": "codex_app_server",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                "agent": {"reasoning_effort": "medium"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    conn = _conn(tmp_path)
    codex = sr.upsert_controller_adapter(
        conn,
        controller_adapter_id="controller_codex",
        name="Codex controller",
        provider="openai-codex",
        model="gpt-5.6-sol",
        api_mode="codex_app_server",
        verified_healthy=True,
    )
    gemma = sr.upsert_controller_adapter(
        conn,
        controller_adapter_id="controller_openrouter_gemma4",
        name="OpenRouter Gemma 4 controller",
        provider="openrouter",
        model="google/gemma-4-26b-a4b-it:free",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        key_env="OPENROUTER_API_KEY",
        fallback_adapter_id=codex.id,
        verified_healthy=True,
    )

    override = sr.create_controller_override(
        conn,
        controller_adapter_value=gemma.id,
        mode="once",
        session_id="session-a",
        created_by="test",
    )
    selected = sr.resolve_controller_selection(conn, session_id="session-a")
    assert selected is not None
    assert selected[0].id == override.id
    assert selected[1].id == gemma.id
    assert [item.id for item in sr.controller_fallback_chain(conn, gemma)] == [
        codex.id
    ]

    view = sr.adapter_registry_view(conn, session_id="session-a")
    assert view["controller"]["effective_controller_adapter_id"] == gemma.id
    assert view["controller"]["selection_source"] == "session_override"
    assert view["controller"]["supported_switch_modes"] == [
        "once", "temporary", "permanent"
    ]
    assert {row["controller_adapter_id"] for row in view["controller_adapters"]} == {
        codex.id, gemma.id
    }

    assert sr.record_controller_override_turn(
        conn,
        override_id=override.id,
        session_id="session-a",
        actual_provider="openai-codex",
        actual_model="gpt-5.6-sol",
        fallback_active=True,
        failed=False,
    )
    assert sr.resolve_controller_selection(conn, session_id="session-a") is None
    consumed = sr.get_controller_override(conn, override.id)
    assert consumed is not None and consumed.remaining_uses == 0
    assert consumed.enabled is False
    assert sr.list_adapter_events(conn)[0]["kind"] == "controller_override_failback"


def test_controller_health_gate_requires_declared_secret(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    adapter = sr.upsert_controller_adapter(
        conn,
        controller_adapter_id="controller_openrouter",
        name="OpenRouter",
        provider="openrouter",
        model="google/gemma-4-26b-a4b-it:free",
        health_url="https://openrouter.ai/api/v1/models",
        key_env="OPENROUTER_API_KEY",
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    state = sr.set_controller_adapter_operational_state(
        conn, adapter.id, enabled=True, changed_by="test"
    )

    assert state["enabled"] is False
    assert state["health_state"] == "unhealthy"
    assert "OPENROUTER_API_KEY" in state["health_gate"]["reason"]


def test_controller_health_gate_requires_real_tool_call_when_declared(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    adapter = sr.upsert_controller_adapter(
        conn,
        controller_adapter_id="controller_openrouter",
        name="OpenRouter",
        provider="openrouter",
        model="google/gemma-4-26b-a4b-it:free",
        base_url="https://openrouter.ai/api/v1",
        health_url="https://openrouter.ai/api/v1/models",
        key_env="OPENROUTER_API_KEY",
        metadata={"require_tool_smoke": True},
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self.payload).encode()

    def successful_urlopen(request, timeout):
        assert timeout == 5.0
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": adapter.model}]})
        assert request.full_url.endswith("/chat/completions")
        assert json.loads(request.data)["tool_choice"] == "required"
        return Response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "hermes_health"}}
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(sr, "urlopen", successful_urlopen)
    passed = sr.set_controller_adapter_operational_state(
        conn, adapter.id, enabled=True, changed_by="test"
    )
    assert passed["enabled"] is True
    assert passed["health_gate"]["tool_smoke_passed"] is True

    def missing_tool_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": adapter.model}]})
        return Response({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(sr, "urlopen", missing_tool_urlopen)
    rejected = sr.set_controller_adapter_operational_state(
        conn, adapter.id, enabled=True, changed_by="test"
    )
    assert rejected["enabled"] is False
    assert rejected["health_state"] == "unhealthy"
    assert rejected["health_gate"]["tool_smoke_passed"] is False


def test_openrouter_free_health_refreshes_ordered_live_catalog_once(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    primary = bootstrap.OPENROUTER_FREE_MODEL_PRIORITY[0]
    secondary = bootstrap.OPENROUTER_FREE_MODEL_PRIORITY[2]
    discovered = "vendor/new-tool-model:free"
    adapter = sr.upsert_controller_adapter(
        conn,
        controller_adapter_id="controller_openrouter_free_test",
        name="OpenRouter Free",
        provider="openrouter",
        model=primary,
        base_url="https://openrouter.ai/api/v1",
        health_url="https://openrouter.ai/api/v1/models",
        key_env="OPENROUTER_API_KEY",
        metadata={
            "require_model_in_catalog": True,
            "require_tool_smoke": True,
            "tool_smoke_choice": "auto",
            "dynamic_free_model_fallback": True,
            "openrouter_free_router": True,
            "server_side_model_fallback": True,
            "free_model_suffix": ":free",
            "model_fallback_candidates": [primary, secondary],
        },
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")

    def row(model_id, **overrides):
        payload = {
            "id": model_id,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["tools", "tool_choice"],
            "architecture": {"output_modalities": ["text"]},
            "context_length": 100_000,
        }
        payload.update(overrides)
        return payload

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self.payload).encode()

    smoke_payloads = []

    def openrouter_urlopen(request, timeout):
        assert timeout == 5.0
        if request.full_url.endswith("/models"):
            return Response(
                {
                    "data": [
                        row(discovered),
                        row(secondary),
                        row(primary),
                        row("vendor/paid:free", pricing={"prompt": "0.1", "completion": "0"}),
                        row("vendor/no-tools:free", supported_parameters=[]),
                    ]
                }
            )
        payload = json.loads(request.data)
        smoke_payloads.append(payload)
        return Response(
            {
                "model": secondary,
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "hermes_health"}}
                            ]
                        }
                    }
                ],
            }
        )

    monkeypatch.setattr(sr, "urlopen", openrouter_urlopen)
    state = sr.set_controller_adapter_operational_state(
        conn, adapter.id, enabled=True, changed_by="test"
    )
    assert state["enabled"] is True
    assert len(smoke_payloads) == 1
    assert smoke_payloads[0]["model"] == primary
    assert smoke_payloads[0]["models"] == [secondary, discovered]
    assert smoke_payloads[0]["provider"] == {
        "allow_fallbacks": True,
        "require_parameters": True,
    }
    refreshed = sr.get_controller_adapter(conn, adapter.id)
    assert refreshed is not None
    assert refreshed.metadata["model_fallback_candidates"] == [
        primary,
        secondary,
        discovered,
    ]


def test_controller_health_gate_selects_tool_capable_free_fallback(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    adapter = sr.upsert_controller_adapter(
        conn,
        controller_adapter_id="controller_opencode_hy3",
        name="OpenCode Free",
        provider="opencode-zen",
        model="hy3-free",
        base_url="https://opencode.ai/zen/v1",
        health_url="https://opencode.ai/zen/v1/models",
        metadata={
            "anonymous_api": True,
            "require_model_in_catalog": True,
            "require_tool_smoke": True,
            "tool_smoke_choice": "auto",
            "tool_smoke_max_tokens": 128,
            "dynamic_free_model_fallback": True,
            "free_model_suffix": "-free",
            "free_model_ids": ["big-pickle"],
            "model_fallback_candidates": [
                "big-pickle",
                "nemotron-3-ultra-free",
            ],
        },
    )

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self.payload).encode()

    called_models = []

    def urlopen_with_fallback(request, timeout):
        assert timeout == 5.0
        assert request.get_header("User-agent") == "HermesAgent/supervisor-health"
        assert request.get_header("Authorization") is None
        if request.full_url.endswith("/models"):
            return Response(
                {
                    "data": [
                        {"id": "hy3-free"},
                        {"id": "big-pickle"},
                        {"id": "nemotron-3-ultra-free"},
                        {"id": "future-free"},
                    ]
                }
            )
        payload = json.loads(request.data)
        assert payload["tool_choice"] == "auto"
        assert payload["max_tokens"] == 128
        called_models.append(payload["model"])
        if payload["model"] == "big-pickle":
            return Response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"name": "hermes_health"}}
                                ]
                            }
                        }
                    ]
                }
            )
        return Response({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(sr, "urlopen", urlopen_with_fallback)
    state = sr.set_controller_adapter_operational_state(
        conn, adapter.id, enabled=True, changed_by="test"
    )

    assert state["enabled"] is True
    assert state["model_changed"] is True
    assert state["effective_model"] == "big-pickle"
    assert state["health_gate"]["configured_model_present"] is True
    assert state["health_gate"]["tool_smoke_passed"] is True
    assert called_models == ["hy3-free", "big-pickle"]
    persisted = sr.get_controller_adapter(conn, adapter.id)
    assert persisted is not None
    assert persisted.model == "big-pickle"
    assert persisted.routable() is True


def test_controller_model_change_is_health_gated_and_audited(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    adapter = sr.upsert_controller_adapter(
        conn,
        controller_adapter_id="controller_opencode_hy3",
        name="OpenCode Free",
        provider="opencode-zen",
        model="hy3-free",
        base_url="https://opencode.ai/zen/v1",
        health_url="https://opencode.ai/zen/v1/models",
        metadata={
            "anonymous_api": True,
            "require_model_in_catalog": True,
            "require_tool_smoke": True,
            "free_model_suffix": "-free",
        },
        verified_healthy=True,
    )

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self.payload).encode()

    def model_change_urlopen(request, timeout):
        assert timeout == 5.0
        if request.full_url.endswith("/models"):
            return Response(
                {
                    "data": [
                        {"id": "nemotron-3-ultra-free"},
                        {"id": "north-mini-code-free"},
                    ]
                }
            )
        model = json.loads(request.data)["model"]
        if model == "nemotron-3-ultra-free":
            return Response(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"name": "hermes_health"}}
                                ]
                            }
                        }
                    ]
                }
            )
        return Response({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(sr, "urlopen", model_change_urlopen)
    changed = sr.set_controller_adapter_model(
        conn,
        adapter.id,
        model="nemotron-3-ultra-free",
        changed_by="test",
    )
    assert changed["changed"] is True
    assert changed["effective_model"] == "nemotron-3-ultra-free"

    rejected = sr.set_controller_adapter_model(
        conn,
        adapter.id,
        model="north-mini-code-free",
        changed_by="test",
    )
    assert rejected["changed"] is False
    persisted = sr.get_controller_adapter(conn, adapter.id)
    assert persisted is not None
    assert persisted.model == "nemotron-3-ultra-free"
    assert [
        row["kind"] for row in sr.list_adapter_events(conn, limit=2)
    ] == [
        "controller_adapter_model_change_rejected",
        "controller_adapter_model_changed",
    ]


def test_recent_task_memory_prefers_session_and_latest_rerun_reuses_adapter(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn, "executor_previous")
    binding = sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=executor.id,
        responsibility="primary",
    )

    first_id = kb.create_task(
        conn, title="conversation task", role_shell_id=shell.id, session_id="session-a"
    )
    assert kb.claim_task(conn, first_id) is not None
    first_run = kb.latest_run(conn, first_id)
    assert kb.complete_task(
        conn,
        first_id,
        summary="done",
        expected_run_id=first_run.id,
        receipt=_valid_receipt(
            first_run.id, first_id, shell.id, executor.id, binding.id
        ),
    )

    other_id = kb.create_task(
        conn, title="other conversation", role_shell_id=shell.id, session_id="session-b"
    )
    assert kb.claim_task(conn, other_id) is not None
    other_run = kb.latest_run(conn, other_id)
    assert kb.complete_task(
        conn,
        other_id,
        summary="done",
        expected_run_id=other_run.id,
        receipt=_valid_receipt(
            other_run.id, other_id, shell.id, executor.id, binding.id
        ),
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET completed_at=completed_at+10 WHERE id=?", (other_id,)
        )

    recent = sr.list_recent_adapter_tasks(conn, session_id="session-a", limit=1)
    assert recent[0]["task_id"] == first_id
    assert recent[0]["session_match"] is True
    fallback = sr.list_recent_adapter_tasks(conn, session_id="unknown", limit=1)
    assert fallback[0]["task_id"] == other_id
    assert fallback[0]["session_match"] is False

    resolution = sr.resolve_adapter_task_reference(
        conn, "latest", session_id="session-a", completed_only=True
    )
    assert resolution["task_id"] == first_id
    rerun = sr.reissue_task_with_adapter(
        conn,
        task_id=resolution["task_id"],
        reason="repeat the last task",
        created_by="test",
    )
    assert rerun["executor_id"] == executor.id
    assert rerun["executor_source"] == "previous_run"
    assert kb.get_task(conn, rerun["revision_task_id"]).session_id == "session-a"


def test_missing_heartbeat_is_not_treated_as_fresh(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn, heartbeat=False)
    # Re-register a heartbeat-required executor with no heartbeat.
    required = sr.register_executor(
        conn,
        executor_id="executor_requires_heartbeat",
        name="requires heartbeat",
        adapter_type="hermes_profile",
        launch_config={"profile": "default"},
        capabilities=("file", "terminal", "kanban"),
        heartbeat_required=True,
    )
    sr.bind_executor(conn, shell_id=shell.id, executor_id=required.id)
    with pytest.raises(sr.NoEligibleExecutor):
        sr.select_binding(conn, shell.id)

    sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id, priority=-1)
    selected = sr.select_binding(conn, shell.id)
    assert selected.executor.id == executor.id


def test_claim_records_shell_executor_binding_on_run_and_capacity_uses_runs(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn, capacity=1)
    binding = sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    first_id = kb.create_task(conn, title="first", role_shell_id=shell.id)
    second_id = kb.create_task(conn, title="second", role_shell_id=shell.id)

    claimed = kb.claim_task(conn, first_id)
    assert claimed is not None
    run = kb.latest_run(conn, first_id)
    assert (run.role_shell_id, run.executor_id, run.binding_id) == (
        shell.id,
        executor.id,
        binding.id,
    )
    assert sr.active_run_count(conn, executor.id) == 1
    with pytest.raises(sr.NoEligibleExecutor):
        sr.select_binding(conn, shell.id)
    assert kb.claim_task(conn, second_id) is None


def test_effective_capabilities_are_intersection_not_union(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn, allowed=("file", "terminal", "kanban"))
    executor = _executor(conn)
    sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=executor.id,
        capability_cap=("file", "kanban", "web"),
    )

    selected = sr.select_binding(conn, shell.id)
    assert selected.effective_capabilities == ["file", "kanban"]
    assert "web" not in selected.effective_capabilities
    assert "terminal" not in selected.effective_capabilities


def test_bound_completion_requires_timeline_receipt_and_stores_it_atomically(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn)
    binding = sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="receipt", role_shell_id=shell.id)
    task = kb.claim_task(conn, task_id)
    run = kb.latest_run(conn, task_id)

    with pytest.raises(sr.ReceiptValidationError, match="requires a structured receipt"):
        kb.complete_task(
            conn,
            task_id,
            summary="done",
            expected_run_id=run.id,
        )
    assert kb.get_task(conn, task_id).status == "running"

    bad = _valid_receipt(run.id, task_id, shell.id, executor.id, binding.id)
    bad["timeline"]["verify_all"]["invalid_count"] = 1
    with pytest.raises(sr.ReceiptValidationError, match="invalid_count"):
        kb.complete_task(
            conn,
            task_id,
            summary="done",
            expected_run_id=run.id,
            receipt=bad,
        )
    assert kb.get_task(conn, task_id).status == "running"

    receipt = _valid_receipt(run.id, task_id, shell.id, executor.id, binding.id)
    receipt["timeline"].pop("goal_id")
    assert kb.complete_task(
        conn,
        task_id,
        summary="done",
        expected_run_id=run.id,
        receipt=receipt,
    )
    assert kb.get_task(conn, task_id).result == "done"
    closed = kb.latest_run(conn, task_id)
    assert closed.receipt_id is not None
    stored_receipt = json.loads(
        conn.execute(
            "SELECT receipt_json FROM run_receipts WHERE id=?",
            (closed.receipt_id,),
        ).fetchone()["receipt_json"]
    )
    assert stored_receipt["timeline"]["goal_id"] == sr.timeline_goal_id(
        task_id, run.id
    )


def test_neural_recall_evidence_is_enforced_when_shell_requires_it(tmp_path):
    conn = _conn(tmp_path)
    shell = sr.register_shell_version(
        conn,
        shell_key="research",
        name="Research",
        contract={"allowed_adapters": ["hermes_profile"]},
        required_capabilities=("kanban",),
        allowed_capabilities=("kanban",),
        evidence_policy={
            "timeline_required": True,
            "neural_recall_required": True,
            "code_slice_required": False,
        },
    )
    executor = _executor(conn)
    binding = sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="neural receipt", role_shell_id=shell.id)
    kb.claim_task(conn, task_id)
    run = kb.latest_run(conn, task_id)
    receipt = _valid_receipt(run.id, task_id, shell.id, executor.id, binding.id)
    receipt["timeline"].pop("neural_recall")

    with pytest.raises(sr.ReceiptValidationError, match="neural_recall.performed"):
        kb.complete_task(
            conn,
            task_id,
            summary="done",
            expected_run_id=run.id,
            receipt=receipt,
        )
    assert kb.get_task(conn, task_id).status == "running"
    assert sr.receipt_summary(conn) == {
        "valid": 0,
        "missing": 0,
        "invalid": 0,
        "failed_without_receipt": 0,
    }


def test_bound_completion_promotes_receipt_body_and_inspect_can_recover_it(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn, key="market", allowed=("web", "kanban"))
    executor = _executor(conn, "executor_market")
    binding = sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="market brief", role_shell_id=shell.id)
    assert kb.claim_task(conn, task_id) is not None
    run = kb.latest_run(conn, task_id)
    receipt = _valid_receipt(run.id, task_id, shell.id, executor.id, binding.id)
    receipt["outputs"] = [
        {
            "type": "market_summary",
            "conclusion": "한국장은 약세이고 나스닥 선물은 상승 중입니다.",
            "levels": {"KOSPI": "-1.44%", "NQU26": "+0.63%"},
            "source_links": ["https://example.test/source"],
        }
    ]

    assert kb.complete_task(
        conn,
        task_id,
        summary="한국장·나스닥 선물 단기 시황 요약",
        expected_run_id=run.id,
        receipt=receipt,
    )
    promoted = kb.get_task(conn, task_id).result
    assert promoted.startswith("한국장은 약세이고")
    assert '"KOSPI": "-1.44%"' in promoted
    assert "https://example.test/source" in promoted
    completed = [event for event in kb.list_events(conn, task_id) if event.kind == "completed"][-1]
    assert completed.payload["result_len"] == len(promoted)
    assert completed.payload["delivery_source"] == "receipt_outputs"

    # Old cards can still have NULL task.result. Inspect must recover their
    # validated receipt output instead of telling the controller work is empty.
    conn.execute("UPDATE tasks SET result=NULL WHERE id=?", (task_id,))
    conn.commit()
    inspected = sr.inspect_task_adapter(conn, task_id)
    assert inspected["delivery"]["source"] == "receipt.outputs"
    assert inspected["delivery"]["result"] == promoted
    assert inspected["delivery"]["receipt_outputs"] == receipt["outputs"]


def test_dispatch_role_task_uses_bound_provenance_even_with_spawn_stub(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn)
    binding = sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="dispatch", role_shell_id=shell.id)
    seen = []

    def spawn(task, workspace, board=None):
        seen.append((task.id, task.role_shell_id, workspace, board))
        return 4242

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    result = kb.dispatch_once(conn, spawn_fn=spawn, board="default")
    assert result.spawned[0][0] == task_id
    assert result.spawned[0][1] == shell.id
    assert seen[0][1] == shell.id
    run = kb.latest_run(conn, task_id)
    assert (run.executor_id, run.binding_id) == (executor.id, binding.id)


def test_supervisor_root_boundary_excludes_bound_executor_children():
    config = {"supervisor": {"enabled": True}}
    assert sr.supervisor_root_enabled(config, environ={}) is True
    assert sr.supervisor_root_enabled(
        config, environ={"HERMES_ROLE_SHELL_ID": "shell_code_v1"}
    ) is False
    assert sr.supervisor_root_enabled(
        {"supervisor": {"enabled": False}}, environ={}
    ) is False


def test_manual_executor_is_never_selected_for_autonomous_dispatch(tmp_path):
    conn = _conn(tmp_path)
    shell = sr.register_shell_version(
        conn,
        shell_key="manual",
        name="Manual",
        contract={"allowed_adapters": ["manual"]},
        required_capabilities=("kanban",),
        allowed_capabilities=("kanban",),
    )
    executor = sr.register_executor(
        conn,
        executor_id="executor_manual",
        name="manual",
        adapter_type="manual",
        capabilities=("kanban",),
        heartbeat_required=False,
    )
    sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    with pytest.raises(sr.NoEligibleExecutor):
        sr.select_binding(conn, shell.id)


@pytest.mark.parametrize(
    "launch, message",
    [
        ({"argv": ["worker"]}, "prompt_file"),
        (
            {"argv": ["worker", "{prompt_file}"], "shell": True},
            "shell mode",
        ),
        (
            {"argv": ["worker", "{prompt_file}"]},
            "capability_enforcement",
        ),
        (
            {
                "argv": ["worker", "{prompt_file}"],
                "capability_enforcement": "argv",
            },
            "capabilities_csv",
        ),
    ],
)
def test_command_executor_registration_fails_closed(launch, message, tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(sr.SupervisorRegistryError, match=message):
        sr.register_executor(
            conn,
            name="unsafe command",
            adapter_type="command",
            launch_config=launch,
            capabilities=("kanban",),
        )


def test_command_executor_health_urls_are_validated_and_normalized(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(sr.SupervisorRegistryError, match="health URL"):
        sr.register_executor(
            conn,
            name="unsafe health",
            adapter_type="command",
            launch_config={
                "argv": ["worker", "{prompt_file}"],
                "capability_enforcement": "env",
                "health_url": "file:///tmp/pretend-healthy",
            },
            capabilities=("kanban",),
        )

    executor = sr.register_executor(
        conn,
        name="probed command",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "health_url": "http://127.0.0.1:8765/health",
            "health_timeout_seconds": 1,
        },
        capabilities=("kanban",),
    )
    assert executor.launch_config["health_urls"] == [
        "http://127.0.0.1:8765/health"
    ]
    assert "health_url" not in executor.launch_config


def test_command_executor_native_mcp_contract_covers_claimed_capabilities(tmp_path):
    conn = _conn(tmp_path)
    servers = [
        "hermes-timeline-code-map",
        "example-browser",
        "example-browser-extra",
        "example-market-data",
        "example-korea-market",
        "example-filings",
        "example-futures",
    ]
    executor = sr.register_executor(
        conn,
        name="universal external agent",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "tool_contract": {
                "transport": "native_mcp",
                "adapter_capabilities": ["kanban"],
                "native_capabilities": ["file", "terminal", *servers],
                "required_mcp_servers": servers,
                "probe": {
                    "argv": ["worker", "mcp", "list"],
                    "required_output": servers,
                },
            },
        },
        capabilities=("file", "terminal", "kanban", *servers),
        heartbeat_required=False,
    )

    contract = executor.launch_config["tool_contract"]
    assert contract["required_mcp_servers"] == sorted(servers)
    with pytest.raises(
        sr.SupervisorRegistryError,
        match="capabilities missing from tool_contract: browser",
    ):
        sr.register_executor(
            conn,
            name="overclaiming agent",
            adapter_type="command",
            launch_config={
                "argv": ["worker", "{prompt_file}"],
                "capability_enforcement": "env",
                "tool_contract": {
                    "transport": "native_mcp",
                    "adapter_capabilities": ["kanban"],
                    "native_capabilities": ["file"],
                    "probe": {"argv": ["worker", "mcp", "list"]},
                },
            },
            capabilities=("file", "kanban", "browser"),
        )


def test_command_executor_tool_probe_is_part_of_health_gate(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    servers = ["hermes-timeline-code-map", "example-market-data"]
    executor = sr.register_executor(
        conn,
        executor_id="executor_tool_probe",
        name="tool-probed agent",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "tool_contract": {
                "transport": "native_mcp",
                "adapter_capabilities": ["kanban"],
                "native_capabilities": servers,
                "required_mcp_servers": servers,
                "probe": {
                    "argv": ["worker", "mcp", "list"],
                    "required_output": servers,
                    "timeout_seconds": 10,
                },
            },
        },
        capabilities=("kanban", *servers),
    )

    def fake_run(argv, **kwargs):
        assert argv == ["worker", "mcp", "list"]
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="hermes-timeline-code-map\nexample-market-data\n",
        )

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    probes = sr.refresh_executor_health_probes(conn)

    assert probes[0]["executor_id"] == executor.id
    assert probes[0]["healthy"] is True
    assert probes[0]["checks"] == [
        {
            "kind": "tool_contract",
            "healthy": True,
            "returncode": 0,
            "required_output": sorted(servers),
            "missing_output": [],
        }
    ]


def test_command_executor_health_confirms_transient_failure_before_poisoning_route(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    server = "hermes-timeline-code-map"
    executor = sr.register_executor(
        conn,
        executor_id="executor_transient_probe",
        name="transient probe",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "tool_contract": {
                "transport": "native_mcp",
                "adapter_capabilities": ["kanban"],
                "native_capabilities": [server],
                "required_mcp_servers": [server],
                "probe": {
                    "argv": ["worker", "probe"],
                    "required_output": [server],
                    "timeout_seconds": 10,
                },
            },
        },
        capabilities=("kanban", server),
    )
    returncodes = iter((1, 0))

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            next(returncodes),
            stdout=f"{server}\n",
        )

    monkeypatch.setattr(sr.subprocess, "run", fake_run)

    probes = sr.refresh_executor_health_probes(
        conn,
        executor_ids=(executor.id,),
    )

    assert probes[0]["attempt_count"] == 2
    assert probes[0]["attempts"][0]["healthy"] is False
    assert probes[0]["attempts"][1]["healthy"] is True
    assert probes[0]["confirmed_failure"] is False
    assert sr.get_executor(conn, executor.id).health_state == "healthy"


def test_command_executor_health_requires_every_declared_endpoint(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    healthy = sr.register_executor(
        conn,
        executor_id="executor_healthy_command",
        name="healthy command",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "health_urls": [
                "http://127.0.0.1:8765/health",
                "http://127.0.0.1:8010/v1/models",
            ],
        },
        capabilities=("kanban",),
    )
    failed = sr.register_executor(
        conn,
        executor_id="executor_failed_command",
        name="failed command",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "health_urls": ["http://127.0.0.1:8007/v1/models"],
        },
        capabilities=("kanban",),
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

    def fake_urlopen(request, timeout):
        assert timeout == 3.0
        if "8007" in request.full_url:
            raise OSError("model endpoint offline")
        return Response()

    monkeypatch.setattr(sr, "urlopen", fake_urlopen)
    probes = sr.refresh_executor_health_probes(conn)
    assert {row["executor_id"]: row["healthy"] for row in probes} == {
        healthy.id: True,
        failed.id: False,
    }
    assert sr.get_executor(conn, healthy.id).health_state == "healthy"
    assert sr.get_executor(conn, failed.id).health_state == "unhealthy"


def test_command_executor_enable_is_rejected_and_audited_when_health_fails(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    executor = sr.register_executor(
        conn,
        executor_id="executor_guarded_command",
        name="guarded command",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "health_urls": ["http://127.0.0.1:8007/v1/models"],
        },
        capabilities=("kanban",),
    )
    assert sr.set_executor_enabled(conn, executor.id, False)
    monkeypatch.setattr(sr, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))

    result = sr.set_executor_operational_state(
        conn,
        executor.id,
        enabled=True,
        reason="try local adapter",
        changed_by="test",
    )

    assert result["requested_enabled"] is True
    assert result["enabled"] is False
    assert result["health_gate_passed"] is False
    assert result["health_state"] == "unhealthy"
    event = sr.list_adapter_events(conn, scope_type="executor", scope_key=executor.id)[0]
    assert event["kind"] == "adapter_executor_enable_rejected"


def test_command_executor_enable_passes_only_after_health_probe(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    executor = sr.register_executor(
        conn,
        executor_id="executor_ready_command",
        name="ready command",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
            "health_urls": ["http://127.0.0.1:8765/health"],
        },
        capabilities=("kanban",),
    )
    assert sr.set_executor_enabled(conn, executor.id, False)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

    monkeypatch.setattr(sr, "urlopen", lambda *_args, **_kwargs: Response())

    result = sr.set_executor_operational_state(conn, executor.id, enabled=True)

    assert result["enabled"] is True
    assert result["health_gate_passed"] is True
    assert result["health_state"] == "healthy"


def test_command_executor_binding_env_stamps_resolved_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = sr.register_executor(
        conn,
        executor_id="executor_command",
        name="command",
        adapter_type="command",
        launch_config={
            "argv": ["worker", "{prompt_file}"],
            "capability_enforcement": "env",
        },
        capabilities=("file", "terminal", "kanban"),
        heartbeat_required=False,
    )
    binding = sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="command env", role_shell_id=shell.id)
    task = kb.claim_task(conn, task_id)
    run = conn.execute(
        "SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)
    ).fetchone()
    selection = sr.Selection(
        shell=shell,
        executor=executor,
        binding=binding,
        effective_capabilities=["file", "kanban", "terminal"],
        active_runs=1,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    env = ea._binding_env(task, run, selection, "default", str(workspace))

    assert env["HERMES_KANBAN_WORKSPACE"] == str(workspace.resolve())
    assert env["HERMES_KANBAN_TASK"] == task_id
    assert env["HERMES_KANBAN_RUN_ID"] == str(task.current_run_id)


def test_role_task_cannot_mix_profile_assignee_or_close_without_claim(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    with pytest.raises(ValueError, match="mutually exclusive"):
        kb.create_task(
            conn,
            title="ambiguous",
            assignee="default",
            role_shell_id=shell.id,
        )
    task_id = kb.create_task(conn, title="unclaimed", role_shell_id=shell.id)
    with pytest.raises(sr.ReceiptValidationError, match="must be claimed"):
        kb.complete_task(conn, task_id, summary="unsafe manual completion")


def test_hermes_profile_executor_receives_role_contract_prompt(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn)
    sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="role prompt", role_shell_id=shell.id)
    task = kb.claim_task(conn, task_id)
    seen = {}

    def fake_spawn(routed_task, workspace, board=None):
        seen["task"] = routed_task
        seen["workspace"] = workspace
        return 9090

    monkeypatch.setattr(kb, "_default_spawn", fake_spawn)
    assert ea.spawn_bound_task(conn, task, str(tmp_path)) == 9090
    routed = seen["task"]
    assert shell.id in routed.worker_prompt
    assert "Timeline evidence contract" in routed.worker_prompt
    assert routed.timeline_goal_id == sr.timeline_goal_id(task_id, task.current_run_id)
    assert routed.timeline_goal_id in routed.worker_prompt
    assert f"run_id={task.current_run_id}" in routed.worker_prompt
    assert routed.effective_capabilities == ["file", "kanban", "terminal"]


def test_recovery_card_prompt_embeds_blocked_source_runs_and_comments(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    shell = _shell(conn, "verification")
    executor = _executor(conn)
    sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    source_id = kb.create_task(
        conn,
        title="source with useful result",
        initial_status="blocked",
    )
    kb.add_comment(
        conn,
        source_id,
        "worker",
        "Recovered market result with primary-source links.",
    )
    recovery_id = kb.create_task(
        conn,
        title="verify recovered result",
        role_shell_id=shell.id,
    )
    sr.register_task_recovery_sources(
        conn,
        recovery_task_id=recovery_id,
        source_task_ids=[source_id],
        created_by="test",
    )
    task = kb.claim_task(conn, recovery_id)
    seen = {}

    def fake_spawn(routed_task, workspace, board=None):
        seen["task"] = routed_task
        return 9091

    monkeypatch.setattr(kb, "_default_spawn", fake_spawn)
    assert ea.spawn_bound_task(conn, task, str(tmp_path)) == 9091
    prompt = seen["task"].worker_prompt
    assert "Non-blocking result-recovery sources" in prompt
    assert source_id in prompt
    assert "Recovered market result with primary-source links." in prompt
    assert "do not create another recovery card" in prompt


def test_supervisor_delegate_records_nonblocking_recovery_lineage(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        source_shell = sr.register_shell_version(
            conn,
            shell_key="market",
            name="Market",
            contract={"allowed_adapters": ["hermes_profile"]},
            required_capabilities=("kanban", "example-market-data"),
            allowed_capabilities=("kanban", "example-market-data"),
            evidence_policy={"timeline_required": True},
        )
        source_id = kb.create_task(
            conn,
            title="blocked output source",
            initial_status="blocked",
            role_shell_id=source_shell.id,
        )
        shell = sr.register_shell_version(
            conn,
            shell_key="verification",
            name="Verification",
            contract={"allowed_adapters": ["hermes_profile"]},
            required_capabilities=("kanban",),
            allowed_capabilities=("kanban", "example-market-data"),
            evidence_policy={"timeline_required": True},
        )
        general = _executor(conn, "executor_general")
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=general.id,
            priority=100,
            responsibility="primary",
        )
        market = sr.register_executor(
            conn,
            executor_id="executor_market_verifier",
            name="Market verifier",
            adapter_type="hermes_profile",
            launch_config={"profile": "market"},
            capabilities=("kanban", "example-market-data"),
        )
        sr.heartbeat_executor(conn, market.id)
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=market.id,
            priority=1,
            responsibility="candidate",
        )

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {
                "shell_key": "verification",
                "title": "recover and verify",
                "source_task_ids": [source_id],
            },
            session_id="telegram-session",
        )
    )
    assert delegated["source_task_ids"] == [source_id]
    assert delegated["lineage_mode"] == "canonical_non_blocking_result_recovery"
    assert delegated["requested_executor_id"] == "executor_market_verifier"
    assert delegated["recovery_required_capabilities"] == [
        "example-market-data",
        "kanban",
    ]
    with kb.connect_closing() as conn:
        recovery = kb.get_task(conn, delegated["task_id"])
        assert recovery.status == "ready"
        assert kb.parent_ids(conn, recovery.id) == []
        assert sr.list_task_recovery_sources(conn, recovery.id)[0][
            "source_task_id"
        ] == source_id
        event_kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id IN (?,?)",
                (source_id, recovery.id),
            ).fetchall()
        ]
    assert "result_recovery_requested" in event_kinds
    assert "result_recovery_source_linked" in event_kinds


def test_supervisor_repair_delegation_is_pinned_to_configured_executor(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "supervisor": {
                    "enabled": True,
                    "repair_executor_id": "executor_hermes_worker_universal",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        shell = _shell(conn, "operations")
        general = _executor(conn, "executor_hermes_worker_general")
        universal = _executor(conn, "executor_hermes_worker_universal")
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=general.id,
            priority=100,
            responsibility="primary",
        )
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=universal.id,
            priority=5,
            responsibility="candidate",
        )

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {
                "shell_key": "operations",
                "title": "gateway heartbeat bug repair",
                "work_kind": "repair",
            },
            session_id="telegram-session",
        )
    )

    assert delegated["work_kind"] == "repair"
    assert delegated["requested_executor_id"] == "executor_hermes_worker_universal"
    assert delegated["executor_selection"] == "pinned_configured_repair"
    assert delegated["routing_policy"] == "configured_repair_executor_required"
    assert delegated["adapter_override"]["executor_id"] == (
        "executor_hermes_worker_universal"
    )
    assert delegated["adapter_override"]["reason"] == (
        "repair policy pins the configured remediation executor"
    )
    assert delegated["adapter_override"]["mode"] == "once"
    assert delegated["adapter_override"]["remaining_uses"] == 1

    rejected = supervisor_tools._handle_delegate(
        {
            "shell_key": "operations",
            "title": "repair with wrong adapter",
            "work_kind": "repair",
            "executor_id": "executor_hermes_worker_general",
        },
        session_id="telegram-session",
    )
    assert "repair work is pinned" in rejected


def test_hermes_self_maintenance_has_a_distinct_pinned_shell_and_executor(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    maintainer_id = "executor_hermes_worker_hermes_maintainer"
    work_kind_schema = supervisor_tools.SUPERVISOR_DELEGATE_SCHEMA[
        "parameters"
    ]["properties"]["work_kind"]
    assert "hermes_repair" in work_kind_schema["enum"]
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "supervisor": {
                    "enabled": True,
                    "repair_executor_id": "executor_hermes_worker_universal",
                    "hermes_repair_executor_id": maintainer_id,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        shell = _shell(conn, "hermes-repair")
        _shell(conn, "code")
        maintainer = _executor(conn, maintainer_id)
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=maintainer.id,
            priority=100,
            responsibility="primary",
        )

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {
                "shell_key": "hermes-repair",
                "title": "repair Hermes adapter routing",
                "work_kind": "hermes_repair",
            },
            session_id="telegram-session",
        )
    )
    assert delegated["shell_key"] == "hermes-repair"
    assert delegated["work_kind"] == "hermes_repair"
    assert delegated["requested_executor_id"] == maintainer_id
    assert delegated["executor_selection"] == "pinned_hermes_maintainer"
    assert delegated["routing_policy"] == "dedicated_hermes_maintainer_required"

    ordinary_on_maintainer = supervisor_tools._handle_delegate(
        {
            "shell_key": "hermes-repair",
            "title": "ordinary project code fix",
            "work_kind": "repair",
        },
        session_id="telegram-session",
    )
    assert "ordinary code repair stays on code or operations" in ordinary_on_maintainer

    hermes_on_code = supervisor_tools._handle_delegate(
        {
            "shell_key": "code",
            "title": "Hermes adapter repair",
            "work_kind": "hermes_repair",
        },
        session_id="telegram-session",
    )
    assert "must use the hermes-repair role shell" in hermes_on_code


def test_nested_recovery_source_is_flattened_to_original_card(tmp_path):
    conn = _conn(tmp_path)
    source_id = kb.create_task(
        conn, title="original failed work", initial_status="blocked"
    )
    first_recovery_id = kb.create_task(conn, title="first recovery")
    sr.register_task_recovery_sources(
        conn,
        recovery_task_id=first_recovery_id,
        source_task_ids=[source_id],
        created_by="test",
    )
    second_recovery_id = kb.create_task(conn, title="nested recovery")

    recorded = sr.register_task_recovery_sources(
        conn,
        recovery_task_id=second_recovery_id,
        source_task_ids=[first_recovery_id],
        created_by="test",
    )

    assert recorded == [source_id]
    assert sr.canonical_task_recovery_sources(
        conn, [second_recovery_id]
    ) == [source_id]
    assert [
        row["source_task_id"]
        for row in sr.list_task_recovery_sources(conn, second_recovery_id)
    ] == [source_id]


def test_completed_recovery_archives_blocked_sources_and_unblocks_children(tmp_path):
    conn = _conn(tmp_path)
    source_id = kb.create_task(
        conn,
        title="blocked original",
        initial_status="blocked",
    )
    child_id = kb.create_task(conn, title="waits on original", parents=[source_id])
    kb.add_comment(conn, source_id, "worker", "original partial result")
    recovery_id = kb.create_task(conn, title="verified recovery")
    sr.register_task_recovery_sources(
        conn,
        recovery_task_id=recovery_id,
        source_task_ids=[source_id],
        created_by="test",
    )

    assert kb.complete_task(conn, recovery_id, summary="verified final result")

    assert kb.get_task(conn, recovery_id).status == "done"
    assert kb.get_task(conn, source_id).status == "archived"
    assert kb.get_task(conn, child_id).status == "ready"
    assert kb.list_comments(conn, source_id)[0].body == "original partial result"
    source_events = [event.kind for event in kb.list_events(conn, source_id)]
    recovery_events = [event.kind for event in kb.list_events(conn, recovery_id)]
    assert "result_recovery_superseded" in source_events
    assert "result_recovery_sources_terminalized" in recovery_events


def test_supervisor_delegate_reopens_existing_canonical_recovery_card(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        source_shell = sr.register_shell_version(
            conn,
            shell_key="market",
            name="Market",
            contract={"allowed_adapters": ["hermes_profile"]},
            required_capabilities=("kanban", "example-market-data"),
            allowed_capabilities=("kanban", "example-market-data"),
            evidence_policy={"timeline_required": True},
        )
        source_id = kb.create_task(
            conn,
            title="original market request",
            initial_status="blocked",
            role_shell_id=source_shell.id,
        )
        verification_shell = sr.register_shell_version(
            conn,
            shell_key="verification",
            name="Verification",
            contract={"allowed_adapters": ["hermes_profile"]},
            required_capabilities=("kanban",),
            allowed_capabilities=("kanban", "example-market-data"),
            evidence_policy={"timeline_required": True},
        )
        verifier = sr.register_executor(
            conn,
            executor_id="executor_market_verifier",
            name="Market verifier",
            adapter_type="hermes_profile",
            launch_config={"profile": "market"},
            capabilities=("kanban", "example-market-data"),
        )
        sr.heartbeat_executor(conn, verifier.id)
        sr.bind_executor(
            conn,
            shell_id=verification_shell.id,
            executor_id=verifier.id,
            priority=100,
            responsibility="primary",
        )
        recovery_id = kb.create_task(
            conn,
            title="recover market result",
            initial_status="blocked",
            session_id="telegram-session",
            role_shell_id=verification_shell.id,
        )
        sr.register_task_recovery_sources(
            conn,
            recovery_task_id=recovery_id,
            source_task_ids=[source_id],
            created_by="test",
        )
        before_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks"
        ).fetchone()["n"]

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {
                "shell_key": "verification",
                "title": "do not create another recovery",
                "source_task_ids": [recovery_id],
            },
            session_id="telegram-session",
        )
    )

    assert delegated["created"] is False
    assert delegated["reused"] is True
    assert delegated["reopened"] is True
    assert delegated["task_id"] == recovery_id
    assert delegated["status"] == "ready"
    assert delegated["source_task_ids"] == [source_id]
    assert delegated["requested_executor_id"] == verifier.id
    with kb.connect_closing() as conn:
        after_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks"
        ).fetchone()["n"]
        assert after_count == before_count
        assert kb.get_task(conn, recovery_id).status == "ready"
        assert [
            row["source_task_id"]
            for row in sr.list_task_recovery_sources(conn, recovery_id)
        ] == [source_id]
        event_kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (recovery_id,),
            ).fetchall()
        ]
    assert "result_recovery_reopened" in event_kinds


def test_shell_upgrade_is_idempotent_and_does_not_break_claimed_old_version(tmp_path):
    conn = _conn(tmp_path)
    first = _shell(conn)
    executor = _executor(conn)
    sr.bind_executor(conn, shell_id=first.id, executor_id=executor.id)
    task_id = kb.create_task(conn, title="old contract", role_shell_id=first.id)

    same = sr.ensure_shell_version(
        conn,
        shell_key="code",
        name="Code",
        contract={
            "allowed_adapters": ["hermes_profile", "command"],
            "instructions": "Perform code work only through this role.",
        },
        required_capabilities=("kanban",),
        allowed_capabilities=("file", "terminal", "kanban"),
        evidence_policy={"timeline_required": True, "code_slice_required": True},
    )
    assert same.id == first.id

    second = sr.ensure_shell_version(
        conn,
        shell_key="code",
        name="Code v2",
        contract={"allowed_adapters": ["hermes_profile"]},
        required_capabilities=("kanban",),
        allowed_capabilities=("file", "kanban"),
        evidence_policy={"timeline_required": True, "code_slice_required": True},
    )
    assert second.version == 2
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    assert kb.latest_run(conn, task_id).role_shell_id == first.id


def test_executor_and_binding_upserts_preserve_many_to_many_ids(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = sr.upsert_executor(
        conn,
        executor_id="executor_stable",
        name="stable",
        adapter_type="hermes_profile",
        launch_config={"profile": "default"},
        capabilities=("file", "kanban"),
        capacity=1,
        heartbeat_required=False,
    )
    updated = sr.upsert_executor(
        conn,
        executor_id=executor.id,
        name="stable updated",
        adapter_type="hermes_profile",
        launch_config={"profile": "default"},
        capabilities=("file", "terminal", "kanban"),
        capacity=3,
        heartbeat_required=False,
    )
    assert updated.id == executor.id
    assert updated.capacity == 3
    binding = sr.upsert_binding(
        conn,
        shell_id=shell.id,
        executor_id=executor.id,
        priority=1,
        binding_id="binding_stable",
    )
    rebound = sr.upsert_binding(
        conn,
        shell_id=shell.id,
        executor_id=executor.id,
        priority=9,
        binding_id="binding_stable",
    )
    assert rebound.id == binding.id
    assert rebound.priority == 9


def test_binding_upsert_rebinds_stable_id_to_new_version_of_same_shell(tmp_path):
    conn = _conn(tmp_path)
    first = _shell(conn)
    executor = _executor(conn)
    binding = sr.upsert_binding(
        conn,
        shell_id=first.id,
        executor_id=executor.id,
        priority=100,
        responsibility="primary",
        binding_id="binding_code_primary",
    )
    second = sr.ensure_shell_version(
        conn,
        shell_key="code",
        name="Code v2",
        contract={"allowed_adapters": ["hermes_profile"]},
        required_capabilities=("kanban",),
        allowed_capabilities=("file", "kanban"),
        evidence_policy={"timeline_required": True},
    )

    rebound = sr.upsert_binding(
        conn,
        shell_id=second.id,
        executor_id=executor.id,
        priority=100,
        responsibility="primary",
        binding_id=binding.id,
    )

    assert rebound.id == binding.id
    assert rebound.shell_id == second.id
    assert rebound.executor_id == executor.id
    assert sr.list_bindings(conn, shell_id=first.id) == []


def test_binding_upsert_rejects_stable_id_cross_role_rebind(tmp_path):
    conn = _conn(tmp_path)
    first = _shell(conn)
    executor = _executor(conn)
    binding = sr.upsert_binding(
        conn,
        shell_id=first.id,
        executor_id=executor.id,
        binding_id="binding_code_primary",
    )
    other = sr.register_shell_version(
        conn,
        shell_key="market",
        name="Market",
        contract={"allowed_adapters": ["hermes_profile"]},
        required_capabilities=("kanban",),
        allowed_capabilities=("kanban",),
    )

    with pytest.raises(sr.SupervisorRegistryError, match="binding role mismatch"):
        sr.upsert_binding(
            conn,
            shell_id=other.id,
            executor_id=executor.id,
            binding_id=binding.id,
        )


def test_build_shell_health_reports_routable_coverage(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    executor = _executor(conn)
    binding = sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
    assert sr.build_shell_health(conn) == [
        {
            "role_shell_id": shell.id,
            "shell_key": "code",
            "version": 1,
            "binding_count": 1,
            "routable_binding_count": 1,
            "routable_binding_ids": [binding.id],
            "coverage_healthy": True,
            "selected_route_healthy": True,
            "selected_binding_id": None,
            "selected_executor_id": None,
            "selection_source": "automatic",
            "route_reason": "routable_candidate_available",
            "healthy": True,
        }
    ]


def test_shell_health_follows_forced_route_not_unused_fallback(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    fallback = _executor(conn, "executor_fallback")
    forced = _executor(conn, "executor_forced")
    sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=fallback.id,
        responsibility="primary",
    )
    forced_binding = sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=forced.id,
        responsibility="candidate",
    )
    sr.create_adapter_override(
        conn,
        target="code",
        executor_value=forced.id,
        mode="permanent",
        created_by="test",
    )
    assert sr.heartbeat_executor(conn, forced.id, health_state="unhealthy")

    row = sr.build_shell_health(conn)[0]

    assert row["coverage_healthy"] is True
    assert row["routable_binding_ids"] != []
    assert forced_binding.id not in row["routable_binding_ids"]
    assert row["selected_executor_id"] == forced.id
    assert row["selected_route_healthy"] is False
    assert row["route_reason"] == "selected_override_ineligible_fallback_disabled"
    assert row["healthy"] is False


def test_unhealthy_unused_candidate_does_not_degrade_active_route_lane(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    selected = _executor(conn, "executor_selected")
    candidate = _executor(conn, "executor_candidate")
    sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=selected.id,
        responsibility="primary",
    )
    sr.bind_executor(
        conn,
        shell_id=shell.id,
        executor_id=candidate.id,
        responsibility="candidate",
    )
    sr.create_adapter_override(
        conn,
        target="code",
        executor_value=selected.id,
        mode="permanent",
        created_by="test",
    )
    assert sr.heartbeat_executor(conn, candidate.id, health_state="unhealthy")

    workers = sr.build_worker_health(conn)
    shells = sr.build_shell_health(conn)

    assert next(row for row in workers if row["executor_id"] == candidate.id)[
        "healthy"
    ] is False
    assert shells[0]["healthy"] is True
    assert supervisor_cli._worker_lane_healthy(workers, shells) is True


def test_bootstrap_config_plan_removes_root_mcp_and_partitions_executor_profiles():
    names = {
        bootstrap.TIMELINE_MCP,
        "example-market-data",
        "example-browser",
    }
    current = {
        "model": {"default": "gpt-test"},
        "mcp_servers": {name: {"command": f"/mcp/{name}"} for name in names},
        "platform_toolsets": {"cli": ["hermes-cli"], "telegram": ["hermes-telegram"]},
        "supervisor": {
            "artifact_health": {
                "enabled": True,
                "checks": [{"name": "report", "path": "outputs/report.json"}],
            }
        },
    }
    plan = bootstrap.build_config_plan(current)

    assert plan["root"]["mcp_servers"] == {}
    assert plan["root"]["toolsets"] == ["supervisor", "kanban", "cronjob"]
    assert all(
        value == bootstrap.ROOT_TOOLSETS
        for value in plan["root"]["platform_toolsets"].values()
    )
    for profile in plan["profiles"].values():
        assert bootstrap.TIMELINE_MCP in profile["mcp_servers"]
        assert profile["supervisor"]["enabled"] is False
        assert "example-market-data" not in profile["mcp_servers"]
        assert "example-browser" not in profile["mcp_servers"]
    assert set(plan["profiles"]["hermes-worker-market"]["mcp_servers"]) == {
        bootstrap.TIMELINE_MCP,
    }
    assert {"web", "browser"}.issubset(
        plan["profiles"]["hermes-worker-market"]["toolsets"]
    )
    assert (
        plan["profiles"]["hermes-worker-market"]["model"]["openai_runtime"]
        == "auto"
    )
    assert set(plan["profiles"]["hermes-worker-multitool"]["mcp_servers"]) == {
        bootstrap.TIMELINE_MCP,
    }
    assert {
        "file", "terminal", "web", "skills", "kanban", bootstrap.TIMELINE_MCP,
    }.issubset(plan["profiles"]["hermes-worker-multitool"]["toolsets"])
    assert "example-market-data" not in plan["profiles"]["hermes-worker-multitool"]["toolsets"]
    assert "skills" in plan["profiles"]["hermes-worker-universal"]["toolsets"]
    assert plan["root"]["supervisor"]["heartbeat_schedule"] == "0 * * * *"
    artifact_health = plan["root"]["supervisor"]["artifact_health"]
    assert artifact_health == {
        "enabled": True,
        "checks": [{"name": "report", "path": "outputs/report.json"}],
    }
    assert plan["root"]["supervisor"]["repair_executor_id"] == (
        bootstrap.DEFAULT_REPAIR_EXECUTOR_ID
    )
    assert plan["root"]["supervisor"]["hermes_repair_executor_id"] == (
        bootstrap.DEFAULT_HERMES_REPAIR_EXECUTOR_ID
    )
    maintainer = plan["profiles"]["hermes-worker-hermes-maintainer"]
    assert maintainer["model"]["provider"] == "openai-codex"
    assert maintainer["model"]["default"] == "gpt-5.6-sol"
    assert maintainer["model"]["openai_runtime"] == "codex_app_server"
    assert maintainer["model"]["api_mode"] == "codex_app_server"
    assert maintainer["agent"]["reasoning_effort"] == "high"
    opencode_free = plan["profiles"]["hermes-worker-opencode-free"]
    assert opencode_free["model"] == {
        "default": bootstrap.OPENCODE_FREE_CONTROLLER_MODELS[0],
        "provider": "opencode-zen",
        "base_url": "https://opencode.ai/zen/v1",
        "api_mode": "chat_completions",
    }
    assert opencode_free["fallback_model"] == [
        {"provider": "opencode-zen", "model": model}
        for model in bootstrap.OPENCODE_FREE_CONTROLLER_MODELS[1:]
    ]
    openrouter_free = plan["profiles"]["hermes-worker-openrouter-free"]
    assert openrouter_free["model"]["provider"] == "openrouter"
    assert openrouter_free["model"]["base_url"] == "https://openrouter.ai/api/v1"
    assert bootstrap.HEARTBEAT_DELIVERY == "local"
    assert plan["root"]["supervisor"]["required_cron"] == [
        "hermes-supervisor-heartbeat"
    ]


def test_supervisor_status_compaction_keeps_full_scheduled_inventory():
    compact = supervisor_tools._compact_status(
        {
            "schema": "heartbeat",
            "healthy": True,
            "lanes": {
                "service": {
                    "healthy": True,
                    "services": [{"name": "api-service", "in_sync": True, "secret": "x"}],
                },
                "worker": {"healthy": True, "receipts": {"missing": 0}},
                "scheduled": {
                    "healthy": True,
                    "counts": {
                        "total": 2,
                        "active": 1,
                        "paused": 1,
                        "failed_active": 0,
                    },
                    "expected_paused": ["flow"],
                    "jobs": [
                        {
                            "id": "cron-1",
                            "name": "daily-report",
                            "state": "scheduled",
                            "enabled": True,
                            "no_agent": True,
                            "next_run_at": "2026-07-20T21:35:00+09:00",
                            "last_run_at": "2026-07-20T20:35:00+09:00",
                            "last_status": "ok",
                            "prompt": "large secret prompt",
                        }
                    ],
                },
                "isolation": {"healthy": True, "enabled_root_mcp": []},
            },
        }
    )
    assert compact["service"]["services"] == [
        {"name": "api-service", "state": None, "in_sync": True}
    ]
    assert compact["scheduled"]["counts"]["active"] == 1
    assert compact["scheduled"]["jobs"] == [
        {
            "id": "cron-1",
            "name": "daily-report",
            "state": "scheduled",
            "enabled": True,
            "no_agent": True,
            "next_run_at": "2026-07-20T21:35:00+09:00",
            "last_run_at": "2026-07-20T20:35:00+09:00",
            "last_status": "ok",
        }
    ]
    assert "prompt" not in compact["scheduled"]["jobs"][0]
    assert compact["isolation"]["enabled_root_mcp"] == []


def _status_snapshot_fixture() -> dict:
    return {
        "schema": supervisor_cli.HEARTBEAT_SNAPSHOT_SCHEMA,
        "healthy": True,
        "lanes": {
            "service": {
                "healthy": True,
                "services": [{"name": "api-service", "in_sync": True}],
            },
            "worker": {
                "healthy": True,
                "role_shells": [{"shell_key": key, "healthy": True} for key in (
                    "browser-research", "code", "market", "operations",
                    "report", "verification", "tool-management",
                )],
            },
            "artifacts": {
                "healthy": True,
                "healthy_count": 2,
                "total": 2,
                "checks": [
                    {
                        "name": "event-stream",
                        "healthy": True,
                        "status": "ok",
                        "group": "trading",
                    },
                    {
                        "name": "daily-report",
                        "healthy": True,
                        "status": "ok",
                        "group": "reports",
                    },
                ],
            },
            "scheduled": {
                "healthy": True,
                "jobs": [
                    {"name": "watchdog", "enabled": True},
                    {"name": "flow", "enabled": False, "state": "paused"},
                ],
                "failed_enabled_cron": [],
            },
            "isolation": {"healthy": True, "enabled_root_mcp": []},
        },
    }


def test_supervisor_status_default_reads_snapshot_without_deep_audit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    supervisor_cli.save_heartbeat_snapshot(_status_snapshot_fixture())
    monkeypatch.setattr(
        supervisor_cli,
        "build_heartbeat_snapshot",
        lambda: pytest.fail("default status must not run the deep heartbeat audit"),
    )

    result = json.loads(supervisor_tools._handle_status({}))

    assert result["snapshot"]["source"] == "heartbeat_snapshot"
    assert result["snapshot"]["stale"] is False
    assert "service" not in result
    assert "worker" not in result
    assert "scheduled" not in result
    assert result["operator_text"] == (
        "Hermes 정상\n"
        "1층 구성 상태 ✅\n"
        "  역할 셸: 7/7\n"
        "  실행기: 0/0\n"
        "  영수증 누락: 0\n"
        "  루트 MCP: 0\n"
        "  Timeline/Code Map/NeuralLink: 미구성 또는 구형 스냅샷\n"
        "  상태 스냅샷: 최신\n"
        "2층 서비스·스케줄 ✅\n"
        "  서비스: 1/1\n"
        "  스케줄: 전체 2 / 활성 1 / 일시정지 1 / 실패 0\n"
        "3층 산출물 ✅\n"
        "  점검: 2/2\n"
        "  ✅ event-stream: OK\n"
        "  ✅ daily-report: OK"
    )


def test_supervisor_status_marks_stale_snapshot_without_refreshing_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = supervisor_cli.save_heartbeat_snapshot(_status_snapshot_fixture())
    old = time.time() - supervisor_tools.STATUS_SNAPSHOT_MAX_AGE_SECONDS - 60
    os.utime(target, (old, old))

    result = json.loads(supervisor_tools._handle_status({}))

    assert result["healthy"] is False
    assert result["snapshot"]["stale"] is True
    assert "상세 점검 필요" in result["operator_text"]


def test_supervisor_status_deep_audit_refreshes_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        supervisor_cli,
        "build_heartbeat_snapshot",
        _status_snapshot_fixture,
    )

    result = json.loads(supervisor_tools._handle_status({"mode": "deep"}))

    assert result["snapshot"]["source"] == "deep_audit"
    assert result["snapshot"]["stale"] is False
    assert supervisor_cli.heartbeat_snapshot_path().exists()


def test_heartbeat_reads_current_and_legacy_cron_status_shapes():
    assert supervisor_cli._cron_last_status({"last_status": "ok"}) == "ok"
    assert supervisor_cli._cron_last_status(
        {"last_status": None, "last_run": {"status": "error"}}
    ) == "error"
    assert supervisor_cli._cron_last_status({"last_run": "invalid"}) is None


def test_heartbeat_cron_inventory_counts_and_failed_active_jobs():
    summary = supervisor_cli._cron_inventory_summary(
        [
            {
                "name": "service-watchdog",
                "state": "scheduled",
                "enabled": True,
                "last_status": "ok",
            },
            {
                "name": "daily-report-retry",
                "state": "scheduled",
                "enabled": True,
                "last_status": "error: source gate",
            },
            {
                "name": "quant-etf-flow-dashboard-refresh",
                "state": "paused",
                "enabled": False,
                "last_status": "error",
            },
            {
                "name": "hermes-supervisor-heartbeat",
                "state": "scheduled",
                "enabled": True,
                "last_status": "error",
            },
        ]
    )

    assert summary == {
        "counts": {
            "total": 4,
            "active": 3,
            "paused": 1,
            "failed_active": 1,
            "observed_failed_active": 1,
            "acknowledged_failed_active": 0,
        },
        "failed_active": ["daily-report-retry"],
        "observed_failed_active": ["daily-report-retry"],
        "acknowledged_failed_active": [],
    }


def test_heartbeat_acknowledges_only_exact_failed_run():
    row = {
        "id": "cron-1",
        "name": "daily-report-retry",
        "state": "scheduled",
        "enabled": True,
        "last_run_at": "2026-07-19T05:00:00+09:00",
        "last_status": "error",
    }
    ack = {
        row["name"]: {
            "last_run_at": row["last_run_at"],
            "last_status": row["last_status"],
        }
    }

    acknowledged = supervisor_cli._cron_inventory_summary([row], ack)
    assert acknowledged["failed_active"] == []
    assert acknowledged["acknowledged_failed_active"] == [row["name"]]
    assert acknowledged["counts"]["failed_active"] == 0

    next_failure = {**row, "last_run_at": "2026-07-20T05:00:00+09:00"}
    actionable = supervisor_cli._cron_inventory_summary([next_failure], ack)
    assert actionable["failed_active"] == [row["name"]]
    assert actionable["acknowledged_failed_active"] == []


def test_supervisor_automation_acknowledges_current_failures(monkeypatch, tmp_path):
    failure_path = tmp_path / "failure_acknowledgements.json"
    jobs = [
        {
            "id": "cron-1",
            "name": "daily-report-publish",
            "state": "scheduled",
            "enabled": True,
            "last_run_at": "2026-07-19T16:00:00+09:00",
            "last_status": "error",
        }
    ]
    monkeypatch.setattr("cron.jobs.list_jobs", lambda include_disabled=True: jobs)

    result = supervisor_cli.acknowledge_cron_failures(
        ["daily-report-publish"],
        path=failure_path,
    )

    assert result["acknowledged"] == ["daily-report-publish"]
    assert result["future_failures_alert_again"] is True
    stored = json.loads(failure_path.read_text(encoding="utf-8"))
    row = stored["acknowledgements"]["daily-report-publish"]
    assert row["last_run_at"] == "2026-07-19T16:00:00+09:00"
    assert row["last_status"] == "error"


def test_supervisor_automation_tool_executes_ack_instead_of_explaining(monkeypatch):
    monkeypatch.setattr(
        supervisor_cli,
        "acknowledge_cron_failures",
        lambda jobs, acknowledged_by: {
            "acknowledged": jobs,
            "scope": "exact_failed_run",
            "future_failures_alert_again": True,
        },
    )
    monkeypatch.setattr(
        supervisor_cli,
        "build_heartbeat_snapshot",
        lambda: {"healthy": True, "lanes": {"scheduled": {"healthy": True}}},
    )

    result = json.loads(
        supervisor_tools._handle_automation(
            {
                "action": "acknowledge_failures",
                "jobs": ["daily-report-retry"],
            }
        )
    )

    assert result["acknowledged"] == ["daily-report-retry"]
    assert result["status"]["healthy"] is True


def test_heartbeat_explicit_failure_status_detection():
    assert supervisor_cli._cron_status_failed("error") is True
    assert supervisor_cli._cron_status_failed("failed: exit 2") is True
    assert supervisor_cli._cron_status_failed("ok") is False
    assert supervisor_cli._cron_status_failed(None) is False


def test_hourly_heartbeat_formats_independent_base_and_artifact_layers():
    line = heartbeat_script._format_summary(
        {
            "healthy": False,
            "lanes": {
                "scheduled": {
                    "counts": {
                        "total": 18,
                        "active": 16,
                        "paused": 2,
                        "failed_active": 2,
                        "observed_failed_active": 3,
                        "acknowledged_failed_active": 1,
                    },
                    "jobs": [],
                    "failed_enabled_cron": [
                        "daily-report-retry",
                        "daily-report-publish",
                    ],
                    "acknowledged_failed_cron": [
                        "daily-report-retry",
                    ],
                    "missing_required_cron": [],
                    "unexpected_paused": [],
                },
                "service": {
                    "services": [
                        {"name": "api-service", "in_sync": True},
                        {"name": "event-service", "in_sync": True},
                    ]
                },
                "artifacts": {
                    "healthy": False,
                    "healthy_count": 1,
                    "total": 2,
                    "checks": [
                        {
                            "name": "event-stream",
                            "healthy": False,
                            "status": "stale",
                            "group": "trading",
                        },
                        {
                            "name": "status-page",
                            "healthy": True,
                            "status": "ok",
                            "group": "trading",
                        },
                        {
                            "name": "daily-report",
                            "healthy": True,
                            "status": "ok",
                            "group": "reports",
                        },
                    ],
                },
                "worker": {
                    "executors": [
                        {"healthy": True, "enabled": True},
                        {"healthy": True, "enabled": True},
                        {"healthy": False, "enabled": False},
                    ],
                    "receipts": {"missing": 0},
                },
            },
        }
    )

    assert line.startswith("⚠️ Hermes heartbeat 전체 주의")
    assert "\n\n2층 · 서비스·스케줄 ⚠️" in line
    assert "\n\n3층 · 산출물 ⚠️" in line
    assert "1층 · 구성 상태 ⚠️" in line
    assert not any(glyph in line for glyph in ("├", "└", "│"))
    assert "daily-report-retry" in line
    assert "서비스: 2/2" in line
    assert "역할 셸: 0/0" in line
    assert "실행기: 2/2" in line
    assert "event-stream: STALE" in line
    assert "status-page: OK" in line


def test_hourly_heartbeat_keeps_artifacts_generic_without_domain_expansion():
    line = heartbeat_script._format_summary(
        {
            "healthy": False,
            "lanes": {
                "scheduled": {
                    "healthy": True,
                    "counts": {
                        "total": 1,
                        "active": 1,
                        "paused": 0,
                        "failed_active": 0,
                        "observed_failed_active": 0,
                        "acknowledged_failed_active": 0,
                    },
                },
                "service": {"healthy": True, "services": []},
                "worker": {"healthy": True, "executors": []},
                "isolation": {"healthy": True},
                "artifacts": {
                    "checks": [
                        {
                            "name": "scheduled-collection",
                            "healthy": False,
                            "lifecycle": "not_due",
                            "group": "reports",
                            "evidence": {
                                "counts": {
                                    "ok": 1,
                                    "not_due": 1,
                                    "recovering": 0,
                                    "failed": 0,
                                },
                                "slots": [
                                    {
                                        "name": "first-run",
                                        "lifecycle": "ok",
                                        "reason": "final_gate_complete",
                                    },
                                    {
                                        "name": "second-run",
                                        "lifecycle": "not_due",
                                        "due_at_kst": "2026-07-21T15:45:00+09:00",
                                    },
                                ],
                            },
                        }
                    ]
                },
            },
        }
    )

    assert "\n  ⚠️ scheduled-collection: FAILED" in line
    assert "first-run" not in line
    assert "second-run" not in line
    assert not any(glyph in line for glyph in ("├", "└", "│"))


def test_disabled_bound_executor_does_not_degrade_worker_lane(tmp_path):
    conn = _conn(tmp_path)
    shell = _shell(conn)
    healthy = _executor(conn, "executor_enabled")
    sr.bind_executor(conn, shell_id=shell.id, executor_id=healthy.id, priority=100)
    disabled = _executor(conn, "executor_disabled")
    sr.bind_executor(conn, shell_id=shell.id, executor_id=disabled.id, priority=1)
    assert sr.set_executor_enabled(conn, disabled.id, False)

    workers = sr.build_worker_health(conn)
    shells = sr.build_shell_health(conn)

    assert supervisor_cli._worker_lane_healthy(workers, shells) is True


def test_heartbeat_cron_disables_standard_delivery_wrapper(monkeypatch, tmp_path):
    existing = {"id": "heartbeat-job", "name": bootstrap.HEARTBEAT_JOB_NAME}
    captured = {}

    monkeypatch.setattr("cron.jobs.list_jobs", lambda include_disabled=True: [existing])

    def update_job(job_id, updates):
        captured.update(updates)
        return {**existing, **updates}

    monkeypatch.setattr("cron.jobs.update_job", update_job)
    script = tmp_path / "hermes_supervisor_heartbeat.py"
    script.write_text("", encoding="utf-8")

    result = bootstrap._install_heartbeat_cron(script)

    assert result["wrap_response"] is False
    assert captured["deliver"] == "local"
    assert captured["schedule"] == "0 * * * *"


def test_supervisor_tool_gate_is_root_only(monkeypatch):
    monkeypatch.setattr(
        supervisor_tools,
        "load_config",
        lambda: {"supervisor": {"enabled": True}},
    )
    monkeypatch.delenv("HERMES_ROLE_SHELL_ID", raising=False)
    assert supervisor_tools._check_supervisor_mode() is True
    monkeypatch.setenv("HERMES_ROLE_SHELL_ID", "shell_code_v1")
    assert supervisor_tools._check_supervisor_mode() is False


@pytest.mark.parametrize(
    ("binding_enabled", "worker_enabled", "health", "current", "expected"),
    [
        (True, True, "healthy", True, "사용 중"),
        (True, True, "healthy", False, "대기"),
        (True, True, "degraded", True, "주의"),
        (True, False, "healthy", False, "꺼짐"),
        (True, False, "unhealthy", False, "사용 불가"),
        (False, True, "healthy", False, "꺼짐"),
    ],
)
def test_adapter_operator_worker_state_is_single_and_unambiguous(
    binding_enabled, worker_enabled, health, current, expected
):
    assert supervisor_tools._worker_state(
        binding_enabled=binding_enabled,
        worker_enabled=worker_enabled,
        health_state=health,
        current=current,
    ) == expected


@pytest.mark.parametrize(
    ("runtime", "worker_name", "expected"),
    [
        (
            {
                "backend": "codex_app_server",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "medium",
            },
            "hermes-worker-browser",
            "Codex 5.6-sol(M)",
        ),
        (
            {
                "backend": "opencode-zen",
                "provider": "opencode-zen",
                "model": "hy3-free",
                "reasoning_effort": "medium",
            },
            None,
            "OpenCode HY3(M)",
        ),
        ({"model": "grok-4.5"}, "grok-build", "Grok"),
        ({"model": "qwen"}, "claude-qwen", "Claude-Qwen"),
    ],
)
def test_mobile_runtime_labels_are_short(runtime, worker_name, expected):
    assert supervisor_tools._mobile_runtime_label(
        runtime, worker_name=worker_name
    ) == expected


def test_conversational_adapter_tool_lists_switches_and_inspects(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        shell = _shell(conn)
        executor = _executor(conn, "executor_codex")
        sr.bind_executor(
            conn,
            shell_id=shell.id,
            executor_id=executor.id,
            responsibility="primary",
        )
        task_id = kb.create_task(
            conn,
            title="chat managed",
            role_shell_id=shell.id,
            session_id="telegram-session",
        )

    listed = json.loads(supervisor_tools._handle_adapter({"action": "list"}))
    assert listed["view"] == "operator_compact"
    assert listed["role_adapter_count"] == 8
    code_adapter = next(
        row for row in listed["role_adapters"] if row["adapter"] == "code"
    )
    assert code_adapter["worker"]["worker_id"] == executor.id
    browser_adapter = next(
        row
        for row in listed["role_adapters"]
        if row["adapter"] == "browser-research"
    )
    assert browser_adapter["worker"]["worker_name"] == "미배정"
    assert browser_adapter["worker"]["state"] == "사용 불가"
    assert listed["controller"]["adapter"] == "hermes"
    assert listed["operator_text"].startswith("Hermes: ")
    assert "├ 브라우저:" in listed["operator_text"]
    assert "├ 검증:" in listed["operator_text"]
    assert "├ 멀티툴:" in listed["operator_text"]
    assert "└ Hermes 수선:" in listed["operator_text"]
    assert "executor_codex" not in listed["operator_text"]
    assert "hermes-worker-" not in listed["operator_text"]
    assert "사용 중" not in listed["operator_text"]
    assert "개 역할" not in listed["operator_text"]
    assert "대기:" not in listed["operator_text"]
    assert "컨트롤러 폴백" in listed["operator_text"]
    assert "변경: Hermes 판단 모델만" in listed["operator_text"]
    assert "유지: 역할 8개 연결" in listed["operator_text"]
    assert "범위: MCP·스킬·툴 등록" in listed["operator_text"]
    assert "범위: 지정 소스 수정·테스트" in listed["operator_text"]
    assert "툴: File·Terminal·Timeline" in listed["operator_text"]
    assert "추가 가능: 정상·겸임 가능" in listed["operator_text"]
    assert "비활성: 운영자가 제외" in listed["operator_text"]
    assert "불가: 헬스 실패로 배정 금지" in listed["operator_text"]
    assert "✅" not in listed["operator_text"]
    assert "❌" not in listed["operator_text"]

    compact = json.loads(
        supervisor_tools._handle_adapter({"action": "list", "view": "compact"})
    )
    assert compact == listed

    full = json.loads(
        supervisor_tools._handle_adapter({"action": "list", "view": "full"})
    )
    assert full["shells"][0]["primary"][0]["executor_id"] == executor.id
    assert full["controller"]["slot_key"] == "hermes"

    recent = json.loads(
        supervisor_tools._handle_adapter(
            {"action": "recent"}, session_id="telegram-session"
        )
    )
    assert recent["recent_tasks"][0]["task_id"] == task_id

    switched = json.loads(
        supervisor_tools._handle_adapter(
            {
                "action": "switch",
                "target": task_id,
                "scope_type": "task",
                "executor_id": executor.id,
                "mode": "once",
                "reason": "Telegram request",
            }
        )
    )
    assert switched["switched"] is True

    inspected = json.loads(
        supervisor_tools._handle_adapter(
            {"action": "inspect"}, session_id="telegram-session"
        )
    )
    assert inspected["reference_resolution"]["resolved_from"] == "latest"
    assert inspected["effective_override"]["override_id"] == switched["override_id"]
    assert inspected["available_executors"][0]["executor_id"] == executor.id

    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, task_id) is not None
        run = kb.latest_run(conn, task_id)
        assert kb.complete_task(
            conn,
            task_id,
            summary="Telegram task done",
            expected_run_id=run.id,
            receipt=_valid_receipt(
                run.id, task_id, shell.id, executor.id, run.binding_id
            ),
        )
    rerun = json.loads(
        supervisor_tools._handle_adapter(
            {"action": "rerun", "reason": "do the last task again"},
            session_id="telegram-session",
            notification_route={
                "platform": "telegram",
                "chat_id": "rerun-chat",
                "chat_type": "dm",
                "thread_id": "rerun-topic",
                "user_id": "rerun-user",
                "notifier_profile": "default",
            },
        )
    )
    assert rerun["original_task_id"] == task_id
    assert rerun["executor_id"] == executor.id
    assert rerun["executor_source"] == "previous_run"
    assert rerun["reference_resolution"]["resolved_from"] == "latest_completed"
    assert rerun["notification"] == {
        "subscribed": True,
        "platform": "telegram",
        "threaded": True,
    }
    with kb.connect_closing() as conn:
        rerun_subscriptions = kb.list_notify_subs(conn, rerun["revision_task_id"])
    assert len(rerun_subscriptions) == 1
    assert rerun_subscriptions[0]["chat_id"] == "rerun-chat"
    assert rerun_subscriptions[0]["thread_id"] == "rerun-topic"

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {"shell_key": "code", "title": "follow-up from Telegram"},
            session_id="telegram-session",
            notification_route={
                "platform": "telegram",
                "chat_id": "trusted-chat",
                "chat_type": "dm",
                "thread_id": "trusted-topic",
                "user_id": "trusted-user",
                "notifier_profile": "default",
            },
        )
    )
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, delegated["task_id"]).session_id == "telegram-session"
        subscriptions = kb.list_notify_subs(conn, delegated["task_id"])
    assert delegated["notification"] == {
        "subscribed": True,
        "platform": "telegram",
        "threaded": True,
    }
    assert len(subscriptions) == 1
    assert subscriptions[0]["platform"] == "telegram"
    assert subscriptions[0]["chat_id"] == "trusted-chat"
    assert subscriptions[0]["chat_type"] == "dm"


def test_conversational_adapter_tool_switches_hermes_controller(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "gpt-5.6-sol",
                    "provider": "openai-codex",
                    "openai_runtime": "codex_app_server",
                },
                "supervisor": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        codex = sr.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_codex",
            name="Codex controller",
            provider="openai-codex",
            model="gpt-5.6-sol",
            verified_healthy=True,
        )
        gemma = sr.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_openrouter_gemma4",
            name="OpenRouter Gemma 4 controller",
            provider="openrouter",
            model="google/gemma-4-26b-a4b-it:free",
            fallback_adapter_id=codex.id,
            verified_healthy=True,
        )

    switched = json.loads(
        supervisor_tools._handle_adapter(
            {
                "action": "switch",
                "target": "hermes",
                "controller_id": gemma.id,
                "mode": "once",
                "reason": "Telegram request",
            },
            session_id="telegram-session",
        )
    )

    assert switched["switched"] is True
    assert switched["target"] == "hermes"
    assert switched["applies_from"] == "next_turn"
    listed = json.loads(
        supervisor_tools._handle_adapter(
            {"action": "list", "view": "compact"},
            session_id="telegram-session",
        )
    )
    assert listed["controller"]["controller_adapter_id"] == gemma.id
    assert listed["controller"]["override"]["mode"] == "once"


def test_conversational_adapter_tool_health_gates_executor_activation(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        executor = sr.register_executor(
            conn,
            executor_id="executor_local_candidate",
            name="local candidate",
            adapter_type="command",
            launch_config={
                "argv": ["worker", "{prompt_file}"],
                "capability_enforcement": "env",
                "health_urls": ["http://127.0.0.1:8007/v1/models"],
            },
            capabilities=("kanban",),
        )
        assert sr.set_executor_enabled(conn, executor.id, False)
    monkeypatch.setattr(
        sr,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    result = json.loads(
        supervisor_tools._handle_adapter(
            {
                "action": "executor_state",
                "executor_id": executor.id,
                "enabled": True,
                "reason": "Telegram local-adapter request",
            }
        )
    )

    assert result["requested_enabled"] is True
    assert result["enabled"] is False
    assert result["health_gate_passed"] is False


def test_supervisor_delegate_reports_missing_return_route(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        shell = _shell(conn)
        executor = _executor(conn, "executor_codex")
        sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {"shell_key": "code", "title": "unrouted follow-up"},
            session_id="telegram-session",
        )
    )

    assert delegated["notification"] == {
        "subscribed": False,
        "reason": "no_routable_session",
    }
    with kb.connect_closing() as conn:
        assert kb.list_notify_subs(conn, delegated["task_id"]) == []


def test_supervisor_delegate_uses_gateway_context_without_codex_bridge(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        shell = _shell(conn)
        executor = _executor(conn, "executor_codex")
        sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)

    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="telegram",
        chat_id="hy3-chat",
        chat_type="dm",
        user_id="hy3-user",
        session_key="agent:main:telegram:dm:hy3-chat",
        session_id="hy3-session",
        async_delivery=True,
    )
    try:
        delegated = json.loads(
            supervisor_tools._handle_delegate(
                {"shell_key": "code", "title": "HY3-routed follow-up"},
                session_id="hy3-session",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert delegated["notification"] == {
        "subscribed": True,
        "platform": "telegram",
        "threaded": False,
    }
    with kb.connect_closing() as conn:
        subscriptions = kb.list_notify_subs(conn, delegated["task_id"])
    assert len(subscriptions) == 1
    assert subscriptions[0]["chat_id"] == "hy3-chat"
    assert subscriptions[0]["chat_type"] == "dm"


def test_supervisor_delegate_recovers_route_from_persisted_session(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        shell = _shell(conn)
        executor = _executor(conn, "executor_codex")
        sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)

    from hermes_state import SessionDB

    session_db = SessionDB(db_path=home / "state.db")
    try:
        session_db.create_session(
            "persisted-telegram-session",
            "telegram",
            user_id="persisted-user",
            session_key="agent:main:telegram:dm:persisted-chat",
            chat_id="persisted-chat",
            chat_type="dm",
            thread_id="persisted-topic",
        )
    finally:
        session_db.close()

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {"shell_key": "code", "title": "persisted route follow-up"},
            session_id="persisted-telegram-session",
        )
    )

    assert delegated["notification"] == {
        "subscribed": True,
        "platform": "telegram",
        "threaded": True,
    }
    with kb.connect_closing() as conn:
        subscriptions = kb.list_notify_subs(conn, delegated["task_id"])
    assert len(subscriptions) == 1
    assert subscriptions[0]["chat_id"] == "persisted-chat"
    assert subscriptions[0]["thread_id"] == "persisted-topic"


def test_supervisor_delegate_stamps_controller_provenance(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect_closing() as conn:
        shell = _shell(conn)
        executor = _executor(conn, "executor_codex")
        sr.bind_executor(conn, shell_id=shell.id, executor_id=executor.id)
        codex = sr.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_codex",
            name="Codex controller",
            provider="openai-codex",
            model="gpt-5.6-sol",
            api_mode="codex_app_server",
            verified_healthy=True,
        )
        gemma = sr.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_openrouter_gemma4",
            name="OpenRouter Gemma controller",
            provider="openrouter",
            model="google/gemma-4-26b-a4b-it:free",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            fallback_adapter_id=codex.id,
            verified_healthy=True,
        )
        sr.create_controller_override(
            conn,
            controller_adapter_value=gemma.id,
            mode="temporary",
            duration_seconds=3600,
            session_id="telegram-session",
            created_by="test",
        )

    delegated = json.loads(
        supervisor_tools._handle_delegate(
            {"shell_key": "code", "title": "controller provenance"},
            session_id="telegram-session",
        )
    )

    assert delegated["controller_provenance"]["controller_adapter_id"] == (
        "controller_openrouter_gemma4"
    )
    assert delegated["controller_provenance"]["runtime"]["provider"] == "openrouter"
    with kb.connect_closing() as conn:
        event = next(
            item
            for item in kb.list_events(conn, delegated["task_id"])
            if item.kind == "supervisor_provenance"
        )
    assert event.payload["runtime"]["model"] == "google/gemma-4-26b-a4b-it:free"


def test_supervisor_install_is_idempotent_in_isolated_home(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    mcp_names = {
        bootstrap.TIMELINE_MCP,
        "example-market-data",
        "example-browser",
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"default": "gpt-test"},
                "mcp_servers": {
                    name: {"command": "/bin/false"} for name in mcp_names
                },
                "platform_toolsets": {"cli": ["hermes-cli"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    command = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "supervisor",
        "install",
        "--repo-root",
        str(repo_root),
    ]
    first = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert first.returncode == 0, first.stderr or first.stdout
    second = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert second.returncode == 0, second.stderr or second.stdout

    root = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert root["mcp_servers"] == {}
    assert root["supervisor"]["enabled"] is True
    for name in bootstrap.PROFILE_SPECS:
        profile = yaml.safe_load(
            (home / "profiles" / name / "config.yaml").read_text(encoding="utf-8")
        )
        assert bootstrap.TIMELINE_MCP in profile["mcp_servers"]
        assert profile["supervisor"]["enabled"] is False
    jobs = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs") or []
    assert len([j for j in jobs if j.get("name") == bootstrap.HEARTBEAT_JOB_NAME]) == 1

    conn = sqlite3.connect(home / "kanban.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM role_shells").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM executors").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM role_bindings").fetchone()[0] == 33
        assert conn.execute(
            "SELECT COUNT(*) FROM controller_adapters"
        ).fetchone()[0] == 6
        assert conn.execute(
            "SELECT health_state FROM controller_adapters WHERE id='controller_codex'"
        ).fetchone()[0] == "unknown"
        assert conn.execute(
            "SELECT name,model FROM controller_adapters "
            "WHERE id='controller_openrouter_gemma4'"
        ).fetchone() == (
            "OpenRouter Gemma 4 31B controller",
            "google/gemma-4-31b-it",
        )
        assert conn.execute(
            "SELECT enabled FROM controller_adapters "
            "WHERE id='controller_vllm_gemma4'"
        ).fetchone()[0] == 0
        opencode_free = conn.execute(
            "SELECT name,provider,model,key_env,fallback_adapter_id,metadata_json "
            "FROM controller_adapters WHERE id='controller_opencode_free'"
        ).fetchone()
        assert opencode_free is not None
        assert tuple(opencode_free[:5]) == (
            "OpenCode free controller",
            "opencode-zen",
            bootstrap.OPENCODE_FREE_CONTROLLER_MODELS[0],
            None,
            None,
        )
        assert json.loads(opencode_free[5])["anonymous_api"] is True
        assert json.loads(opencode_free[5])["dynamic_free_model_fallback"] is True
        assert json.loads(opencode_free[5])["model_fallback_candidates"] == [
            *bootstrap.OPENCODE_FREE_CONTROLLER_MODELS,
        ]
        assert json.loads(opencode_free[5])["free_model_ids"] == ["big-pickle"]
        assert json.loads(opencode_free[5])["tool_smoke_choice"] == "auto"
        openrouter_free = conn.execute(
            "SELECT provider,model,key_env,metadata_json FROM controller_adapters "
            "WHERE id='controller_openrouter_free'"
        ).fetchone()
        assert openrouter_free is not None
        assert tuple(openrouter_free[:3]) == (
            "openrouter",
            bootstrap.OPENROUTER_FREE_MODEL_PRIORITY[0],
            "OPENROUTER_API_KEY",
        )
        openrouter_free_metadata = json.loads(openrouter_free[3])
        assert openrouter_free_metadata["openrouter_free_router"] is True
        assert openrouter_free_metadata["server_side_model_fallback"] is True
        assert conn.execute(
            "SELECT key_env FROM controller_adapters WHERE id='controller_grok'"
        ).fetchone()[0] == "XAI_API_KEY"
        assert conn.execute(
            "SELECT COUNT(*) FROM role_bindings WHERE responsibility='primary'"
        ).fetchone()[0] == 8
        free_worker_rows = conn.execute(
            "SELECT id,adapter_type,launch_config FROM executors "
            "WHERE id IN (?,?) ORDER BY id",
            (
                "executor_hermes_worker_opencode_free",
                "executor_hermes_worker_openrouter_free",
            ),
        ).fetchall()
        assert len(free_worker_rows) == 2
        assert {row[1] for row in free_worker_rows} == {"command"}
        for row in free_worker_rows:
            launch = json.loads(row[2])
            assert "hermes_cli.free_worker_router" in json.dumps(launch)
        hermes_repair_shell_id = conn.execute(
            "SELECT shell_id FROM role_shell_heads WHERE shell_key='hermes-repair'"
        ).fetchone()[0]
        assert conn.execute(
            "SELECT COUNT(*) FROM role_bindings WHERE shell_id=? "
            "AND executor_id IN (?,?)",
            (
                hermes_repair_shell_id,
                "executor_hermes_worker_opencode_free",
                "executor_hermes_worker_openrouter_free",
            ),
        ).fetchone()[0] == 0
    finally:
        conn.close()
