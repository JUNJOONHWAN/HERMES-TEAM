from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.supervisor_contract import (
    SUPERVISOR_CONTROL_TOOL_SEQUENCE,
    activate_codex_control_failback,
    normalize_supervisor_repair_delegation,
    normalize_supervisor_tool_management_delegation,
    supervisor_control_plane_active,
    supervisor_control_tool_required,
    supervisor_failure_acknowledgement_requested,
    supervisor_automation_mutation_authorized,
    supervisor_operator_screen_allowed,
    supervisor_operator_text_from_tool_result,
    supervisor_recovery_tool_name,
    supervisor_required_tool_name,
    supervisor_repair_delegation_required,
    supervisor_hermes_repair_delegation_required,
    supervisor_tool_choice_payload,
    supervisor_tool_management_delegation_required,
)
from run_agent import AIAgent


def _tool_def(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _response(content: str = "", *, tool_calls=None, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        model="hy3-free",
        usage=None,
    )


def test_substantive_supervisor_turn_requires_control_tool():
    assert supervisor_control_tool_required("현재 어댑터 현황 보여줘") is True
    assert supervisor_control_tool_required("안녕하세요") is False
    assert supervisor_control_tool_required("[kanban] t_1 done") is False
    assert supervisor_control_tool_required("Cronjob Response: heartbeat") is False


def test_project_and_card_management_routes_to_native_controller():
    messages = (
        "새 프로젝트를 시작하고 첫 카드 만들어줘",
        "t_abcd 카드에 후속 작업을 붙여줘",
        "이 프로젝트 카드들을 병렬로 쪼개줘",
        "완료 카드에 검증 카드를 이어줘",
        "실패한 카드 복구 카드를 만들어줘",
    )
    for message in messages:
        assert supervisor_required_tool_name(message) == "supervisor_project"
        assert supervisor_recovery_tool_name(message) == "supervisor_project"

    # Ordinary adapter inventory still belongs to adapter governance.
    assert supervisor_required_tool_name("현재 어댑터 카드 보여줘") == "supervisor_adapter"


def test_repair_turn_requires_and_normalizes_codex_delegation():
    message = "하트비트 버그 원인을 찾아서 고쳐줘"
    assert supervisor_repair_delegation_required(message) is True

    arguments, eligible = normalize_supervisor_repair_delegation(
        message,
        "supervisor_delegate",
        {
            "shell_key": "operations",
            "title": "heartbeat incident",
            "branch_name": "codex/invalid-for-scratch",
        },
    )
    assert eligible is True
    assert arguments["work_kind"] == "repair"
    assert "branch_name" not in arguments

    arguments, eligible = normalize_supervisor_repair_delegation(
        message,
        "supervisor_delegate",
        {
            "shell_key": "code",
            "title": "worktree repair",
            "workspace_kind": "worktree",
            "workspace_path": "/tmp/repo",
            "branch_name": "codex/valid-worktree",
        },
    )
    assert eligible is True
    assert arguments["branch_name"] == "codex/valid-worktree"

    _, eligible = normalize_supervisor_repair_delegation(
        message,
        "supervisor_status",
        {},
    )
    assert eligible is False


def test_hermes_self_maintenance_is_distinct_from_ordinary_code_repair():
    hermes_message = "헤르메스 어댑터 라우팅 오류를 고쳐줘"
    assert supervisor_hermes_repair_delegation_required(hermes_message) is True
    arguments, eligible = normalize_supervisor_repair_delegation(
        hermes_message,
        "supervisor_delegate",
        {"shell_key": "code", "title": "wrong initial shell"},
    )
    assert eligible is True
    assert arguments["shell_key"] == "hermes-repair"
    assert arguments["work_kind"] == "hermes_repair"

    ordinary_message = "내 프로젝트의 결제 코드 버그를 고쳐줘"
    assert supervisor_hermes_repair_delegation_required(ordinary_message) is False
    arguments, eligible = normalize_supervisor_repair_delegation(
        ordinary_message,
        "supervisor_delegate",
        {"shell_key": "code", "title": "ordinary code repair"},
    )
    assert eligible is True
    assert arguments["shell_key"] == "code"
    assert arguments["work_kind"] == "repair"

    assert supervisor_hermes_repair_delegation_required(
        "헤르메스가 내 프로젝트 코드를 고쳐줘"
    ) is False


def test_tool_mutation_is_pinned_to_multitool_delegation():
    message = "시장 어댑터에 새 MCP를 설치하고 검증해줘"
    assert supervisor_tool_management_delegation_required(message) is True
    assert supervisor_required_tool_name(message) == "supervisor_delegate"

    arguments, eligible = normalize_supervisor_tool_management_delegation(
        message,
        "supervisor_delegate",
        {
            "shell_key": "operations",
            "title": "MCP install",
            "branch_name": "codex/not-for-scratch",
        },
    )

    assert eligible is True
    assert arguments["shell_key"] == "tool-management"
    assert arguments["work_kind"] == "tooling"
    assert "branch_name" not in arguments


def test_tool_inventory_read_does_not_force_a_mutation_card():
    message = "현재 MCP와 스킬 현황 보여줘"
    assert supervisor_tool_management_delegation_required(message) is False


def test_codex_control_failback_jumps_directly_over_intermediate_chain():
    agent = SimpleNamespace(
        provider="opencode-zen",
        _fallback_index=0,
        _fallback_chain=[
            {"provider": "openrouter", "model": "gemma"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        ],
    )
    activations = []

    def activate(*, emit_status=True):
        activations.append((agent._fallback_index, emit_status))
        selected = agent._fallback_chain[agent._fallback_index]
        agent.provider = selected["provider"]
        agent._fallback_index += 1
        return True

    agent._try_activate_fallback = activate

    assert activate_codex_control_failback(agent) is True
    assert agent.provider == "openai-codex"
    assert activations == [(1, False)]
    assert SUPERVISOR_CONTROL_TOOL_SEQUENCE[0] == "supervisor_status"


def test_supervisor_recovery_tool_is_specific_only_after_violation():
    assert supervisor_recovery_tool_name("현재 어댑터 정보") == "supervisor_adapter"
    assert supervisor_recovery_tool_name("그래서 어갭커 현황") == "supervisor_adapter"
    assert supervisor_required_tool_name("그래서 어갭커 현황") == "supervisor_adapter"
    assert supervisor_recovery_tool_name("하트비트 자동화를 고쳐") == "supervisor_delegate"
    assert supervisor_recovery_tool_name("하트비트가 정상인가? 왜 경고야?") == "supervisor_status"
    assert supervisor_required_tool_name("하트비트가 정상인가? 왜 경고야?") == "supervisor_status"
    assert supervisor_required_tool_name("이 실패 확인 처리해서 반복 알리지 마") == "supervisor_automation"
    assert supervisor_recovery_tool_name("역할 셸 현황") == "supervisor_roles"
    assert supervisor_recovery_tool_name("전체 상태 알려줘") == "supervisor_status"


def test_failure_acknowledgement_requires_explicit_operator_command():
    diagnostic = "보고서는 발행됐는데 왜 실패로 보여? 하트비트가 정상인가?"
    explicit = "일일 보고서 실패는 확인 처리하고 반복 알리지 마"

    assert supervisor_failure_acknowledgement_requested(diagnostic) is False
    assert supervisor_automation_mutation_authorized(
        diagnostic, "acknowledge_failures"
    ) is False
    assert supervisor_failure_acknowledgement_requested(explicit) is True
    assert supervisor_automation_mutation_authorized(
        explicit, "acknowledge_failures"
    ) is True


def test_interpretive_heartbeat_question_keeps_model_answer():
    question = "하트비트가 정상인가? 보고서는 나갔는데 뭐가 문제야?"
    assert supervisor_operator_screen_allowed(
        question,
        "supervisor_status",
        {"mode": "deep"},
    ) is False
    assert supervisor_operator_screen_allowed(
        "전체 상태 알려줘",
        "supervisor_status",
        {"mode": "snapshot"},
    ) is True
    assert supervisor_operator_screen_allowed(
        "일일 보고서 실패 확인 처리해",
        "supervisor_automation",
        {"action": "acknowledge_failures"},
    ) is False


def test_wrong_supervisor_tool_does_not_satisfy_clear_adapter_intent():
    tool_defs = [_tool_def(name) for name in SUPERVISOR_CONTROL_TOOL_SEQUENCE]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch(
            "hermes_cli.config.load_config",
            return_value={"supervisor": {"enabled": True}},
        ),
    ):
        agent = AIAgent(
            model="hy3-free",
            api_key="test-key-1234567890",
            base_url="https://opencode.test/v1",
            provider="opencode-zen",
            enabled_toolsets=["supervisor"],
            quiet_mode=True,
            max_iterations=7,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are Hermes."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tool_delay = 0
    wrong_status_call = SimpleNamespace(
        id="call_status",
        type="function",
        function=SimpleNamespace(name="supervisor_status", arguments="{}"),
    )
    adapter_call = SimpleNamespace(
        id="call_adapter",
        type="function",
        function=SimpleNamespace(
            name="supervisor_adapter",
            arguments='{"action":"list","view":"compact"}',
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[wrong_status_call], finish_reason="tool_calls"),
        _response("서비스와 크론도 모두 정상입니다."),
        _response(tool_calls=[adapter_call], finish_reason="tool_calls"),
        _response("Hermes: OpenCode HY3(M)"),
    ]

    with (
        patch("run_agent.handle_function_call", return_value='{"ok":true}'),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("그래서 어갭커 현황")

    calls = agent.client.chat.completions.create.call_args_list
    assert result["final_response"] == "Hermes: OpenCode HY3(M)"
    assert any(
        call.kwargs.get("tool_choice")
        == {
            "type": "function",
            "function": {"name": "supervisor_adapter"},
        }
        for call in calls
    )


def test_adapter_operator_text_wins_when_provider_emits_extra_status_tool():
    tool_defs = [_tool_def(name) for name in SUPERVISOR_CONTROL_TOOL_SEQUENCE]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch(
            "hermes_cli.config.load_config",
            return_value={"supervisor": {"enabled": True}},
        ),
    ):
        agent = AIAgent(
            model="nemotron-3-ultra-free",
            api_key="test-key-1234567890",
            base_url="https://opencode.test/v1",
            provider="opencode-zen",
            enabled_toolsets=["supervisor"],
            quiet_mode=True,
            max_iterations=5,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are Hermes."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tool_delay = 0
    adapter_call = SimpleNamespace(
        id="call_adapter",
        type="function",
        function=SimpleNamespace(
            name="supervisor_adapter",
            arguments='{"action":"list","view":"compact"}',
        ),
    )
    status_call = SimpleNamespace(
        id="call_status",
        type="function",
        function=SimpleNamespace(name="supervisor_status", arguments="{}"),
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[adapter_call, status_call],
            finish_reason="tool_calls",
        ),
        _response("서비스 7/7"),
    ]

    with (
        patch(
            "run_agent.handle_function_call",
            side_effect=[
                '{"operator_text":"어댑터 역할표"}',
                '{"operator_text":"Hermes 상태표"}',
            ],
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("어댑터 현황좀")

    assert result["final_response"] == "어댑터 역할표"
    assert agent.client.chat.completions.create.call_args_list[0].kwargs[
        "tool_choice"
    ] == {
        "type": "function",
        "function": {"name": "supervisor_adapter"},
    }


def test_supervisor_control_plane_requires_complete_native_controller():
    controller = SimpleNamespace(
        _supervisor_mode=True,
        valid_tool_names=set(SUPERVISOR_CONTROL_TOOL_SEQUENCE),
    )
    compression_helper = SimpleNamespace(
        _supervisor_mode=True,
        valid_tool_names={"terminal"},
    )
    assert supervisor_control_plane_active(controller) is True
    assert supervisor_control_plane_active(compression_helper) is False
    assert "supervisor_project" in SUPERVISOR_CONTROL_TOOL_SEQUENCE


def test_supervisor_tool_choice_payloads_match_provider_api_modes():
    assert supervisor_tool_choice_payload("chat_completions") == "required"
    assert supervisor_tool_choice_payload("anthropic_messages") == {"type": "any"}
    assert supervisor_tool_choice_payload(
        "chat_completions", "supervisor_adapter"
    ) == {
        "type": "function",
        "function": {"name": "supervisor_adapter"},
    }
    assert supervisor_tool_choice_payload(
        "codex_responses", "supervisor_delegate"
    ) == {"type": "function", "name": "supervisor_delegate"}


def test_supervisor_operator_text_unwraps_codex_input_text_result():
    wrapped = json.dumps(
        [
            {
                "type": "inputText",
                "text": json.dumps(
                    {
                        "healthy": True,
                        "operator_text": "Hermes 정상\n서비스 7/7",
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        ensure_ascii=False,
    )

    assert supervisor_operator_text_from_tool_result(wrapped) == (
        "Hermes 정상\n서비스 7/7"
    )


def test_supervisor_retries_same_model_with_specific_tool_after_plain_prose():
    tool_defs = [_tool_def(name) for name in SUPERVISOR_CONTROL_TOOL_SEQUENCE]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch(
            "hermes_cli.config.load_config",
            return_value={"supervisor": {"enabled": True}},
        ),
    ):
        agent = AIAgent(
            model="hy3-free",
            api_key="test-key-1234567890",
            base_url="https://opencode.test/v1",
            provider="opencode-zen",
            enabled_toolsets=["supervisor"],
            quiet_mode=True,
            max_iterations=5,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are Hermes."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tool_delay = 0
    adapter_call = SimpleNamespace(
        id="call_adapter",
        type="function",
        function=SimpleNamespace(
            name="supervisor_adapter",
            arguments='{"action":"list"}',
        ),
    )
    agent.client.chat.completions.create.side_effect = [
        _response("제가 직접 확인하겠습니다."),
        _response(tool_calls=[adapter_call], finish_reason="tool_calls"),
        _response("현재 어댑터 상태를 확인했습니다."),
    ]

    with (
        patch("run_agent.handle_function_call", return_value='{"ok":true}'),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("현재 어댑터 정보")

    calls = agent.client.chat.completions.create.call_args_list
    assert result["completed"] is True
    assert result["final_response"] == "현재 어댑터 상태를 확인했습니다."
    assert agent.provider == "opencode-zen"
    assert len(calls) == 3
    assert calls[0].kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "supervisor_adapter"},
    }
    assert calls[1].kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "supervisor_adapter"},
    }
    assert "tool_choice" not in calls[2].kwargs


def test_supervisor_agent_init_fails_closed_when_control_tools_are_missing():
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=[_tool_def("kanban_list")],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch(
            "hermes_cli.config.load_config",
            return_value={"supervisor": {"enabled": True}},
        ),
        pytest.raises(RuntimeError, match="missing tools: supervisor_adapter"),
    ):
        AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.test/v1",
            provider="test",
            enabled_toolsets=["supervisor"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
