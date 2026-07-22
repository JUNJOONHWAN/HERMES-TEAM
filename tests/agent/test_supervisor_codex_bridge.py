from __future__ import annotations

import json
from types import SimpleNamespace

import agent.supervisor_codex_bridge as bridge


def _agent(**overrides):
    values = {
        "platform": "telegram",
        "enabled_toolsets": ["supervisor", "kanban"],
        "session_id": "telegram-session-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(*, enabled=True, toolsets=None):
    return {
        "supervisor": {"enabled": enabled},
        "platform_toolsets": {
            "telegram": toolsets
            if toolsets is not None
            else ["supervisor", "kanban", "no_mcp"],
        },
    }


def test_context_injects_fixed_identity_control_tools_and_no_mcp(monkeypatch):
    monkeypatch.setattr(bridge, "load_config_readonly", lambda: _config())
    monkeypatch.setattr(
        bridge,
        "prepare_supervisor_codex_runtime",
        lambda agent: ("/isolated/workspace", "/isolated/codex_home"),
    )

    context = bridge.build_supervisor_codex_context(_agent())

    assert "fixed lightweight central control tower" in context.developer_instructions
    assert "Never perform domain work yourself" in context.developer_instructions
    assert 'work_kind="repair"' in context.developer_instructions
    assert "configured repair executor" in context.developer_instructions
    assert "Never inspect files, logs" in context.developer_instructions
    assert "required_cron is only a protected" in context.developer_instructions
    assert "scheduled.jobs" in context.developer_instructions
    assert "action=acknowledge_failures" in context.developer_instructions
    assert "not authorization to acknowledge anything" in context.developer_instructions
    assert "never a project" in context.developer_instructions
    assert 'workspace_kind="scratch"' in context.developer_instructions
    assert "queued for automatic dispatch" in context.developer_instructions
    assert "completion notification is subscribed" in context.developer_instructions
    assert "source_task_ids" in context.developer_instructions
    assert [tool["name"] for tool in context.dynamic_tools] == [
        "supervisor_status",
        "supervisor_automation",
        "supervisor_roles",
        "supervisor_delegate",
        "supervisor_adapter",
    ]
    assert all(tool["type"] == "function" for tool in context.dynamic_tools)
    assert callable(context.dynamic_tool_handler)
    assert context.cwd == "/isolated/workspace"
    assert context.codex_home == "/isolated/codex_home"


def test_context_is_not_injected_into_non_supervisor_platform(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "load_config_readonly",
        lambda: _config(toolsets=["kanban", "no_mcp"]),
    )
    assert bridge.build_supervisor_codex_context(
        _agent(enabled_toolsets=["kanban"])
    ) is None


def test_prepare_runtime_isolates_config_and_shares_only_auth(monkeypatch, tmp_path):
    source_home = tmp_path / "operator-codex"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_auth.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setattr(bridge, "get_hermes_home", lambda: hermes_home)

    cwd, codex_home = bridge.prepare_supervisor_codex_runtime(_agent())

    controller_home = hermes_home / "supervisor" / "codex_home"
    config_text = controller_home.joinpath("config.toml").read_text(encoding="utf-8")
    assert codex_home == str(controller_home)
    assert cwd == str(hermes_home / "supervisor" / "workspace")
    assert controller_home.joinpath("auth.json").is_symlink()
    assert controller_home.joinpath("auth.json").resolve() == source_auth.resolve()
    assert 'model = "gpt-5.6-sol"' in config_text
    assert 'model_reasoning_effort = "medium"' in config_text
    assert 'sandbox_mode = "read-only"' in config_text
    assert "apps = false" in config_text
    assert "plugins = false" in config_text
    assert "shell_tool = false" in config_text
    assert "unified_exec = false" in config_text
    assert "multi_agent = false" in config_text
    assert "mcp_servers" not in config_text


def test_dynamic_handler_allowlists_supervisor_tools(monkeypatch):
    import tools.supervisor_tools  # noqa: F401
    from tools.registry import registry as tool_registry

    calls = []

    def dispatch(name, args, **kwargs):
        calls.append((name, args, kwargs))
        return '{"ok": true}'

    monkeypatch.setattr(tool_registry, "dispatch", dispatch)
    monkeypatch.setattr(bridge, "_trusted_notification_route", lambda: None)
    handler = bridge.make_supervisor_dynamic_tool_handler(_agent())

    assert handler("supervisor_adapter", {"action": "list"}) == (
        True,
        '{"ok": true}',
    )
    assert calls == [
        (
            "supervisor_adapter",
            {"action": "list", "view": "compact"},
            {"session_id": "telegram-session-1"},
        )
    ]

    success, text = handler("exec_command", {"cmd": "pwd"})
    assert success is False
    assert "Unsupported Hermes supervisor tool" in text
    assert len(calls) == 1

    handler("supervisor_adapter", {"action": "list", "view": "full"})
    assert calls[-1][1]["view"] == "compact"


def test_dynamic_handler_rejects_unrequested_failure_acknowledgement(monkeypatch):
    import tools.supervisor_tools  # noqa: F401
    from tools.registry import registry as tool_registry

    calls = []

    def dispatch(name, args, **kwargs):
        calls.append((name, args, kwargs))
        return '{"acknowledged":["job"]}'

    monkeypatch.setattr(tool_registry, "dispatch", dispatch)
    monkeypatch.setattr(bridge, "_trusted_notification_route", lambda: None)
    agent = _agent(
        _supervisor_current_user_message=(
            "보고서는 발행됐는데 왜 실패로 보여? 하트비트가 정상인가?"
        )
    )
    handler = bridge.make_supervisor_dynamic_tool_handler(agent)

    success, payload = handler(
        "supervisor_automation",
        {"action": "acknowledge_failures", "jobs": ["daily-report"]},
    )
    assert success is False
    assert json.loads(payload)["error"] == "explicit_operator_authorization_required"
    assert calls == []

    agent._supervisor_current_user_message = (
        "일일 보고서 실패를 확인 처리하고 반복 알리지 마"
    )
    success, _ = handler(
        "supervisor_automation",
        {"action": "acknowledge_failures", "jobs": ["daily-report"]},
    )
    assert success is True
    assert len(calls) == 1


def test_dynamic_handler_passes_trusted_notification_route(monkeypatch):
    import tools.supervisor_tools  # noqa: F401
    from tools.registry import registry as tool_registry

    route = {
        "platform": "telegram",
        "chat_id": "trusted-chat",
        "chat_type": "dm",
        "thread_id": "trusted-topic",
        "user_id": "trusted-user",
        "notifier_profile": "default",
    }
    calls = []

    def dispatch(name, args, **kwargs):
        calls.append((name, args, kwargs))
        return '{"created": true}'

    monkeypatch.setattr(tool_registry, "dispatch", dispatch)
    monkeypatch.setattr(
        bridge, "_trusted_notification_route", lambda: dict(route)
    )

    handler = bridge.make_supervisor_dynamic_tool_handler(_agent())
    assert handler(
        "supervisor_delegate", {"shell_key": "market", "title": "market check"}
    )[0] is True
    assert calls == [
        (
            "supervisor_delegate",
            {"shell_key": "market", "title": "market check"},
            {
                "session_id": "telegram-session-1",
                "notification_route": route,
            },
        )
    ]


def test_trusted_notification_route_comes_from_session_context():
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="telegram",
        chat_id="trusted-chat",
        chat_type="dm",
        thread_id="trusted-topic",
        user_id="trusted-user",
        profile="default",
        async_delivery=True,
    )
    try:
        assert bridge._trusted_notification_route() == {
            "platform": "telegram",
            "chat_id": "trusted-chat",
            "chat_type": "dm",
            "thread_id": "trusted-topic",
            "user_id": "trusted-user",
            "notifier_profile": "default",
        }
    finally:
        clear_session_vars(tokens)


def test_trusted_notification_route_rejects_stateless_session():
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="api",
        chat_id="request-response-only",
        async_delivery=False,
    )
    try:
        assert bridge._trusted_notification_route() is None
    finally:
        clear_session_vars(tokens)
