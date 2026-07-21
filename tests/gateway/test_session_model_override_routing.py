"""Regression tests for session-scoped model/provider overrides in gateway agents.

These cover the bug where `/model ...` stored a session override, but fresh
agent constructions still resolved model/provider from global config/runtime.
That let helper agents (and cache-miss main agents) route GPT-5.4 to the wrong
provider, e.g. Nous instead of OpenAI Codex.
"""

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from hermes_cli import supervisor_registry as sr


class _CapturingAgent:
    """Fake agent that records init kwargs for assertions."""

    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.session_store = None
    runner.config = None
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    return runner


def _codex_override():
    return {
        "model": "gpt-5.4",
        "provider": "openai-codex",
        "api_key": "***",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }


def _explode_runtime_resolution():
    raise AssertionError(
        "global runtime resolution should not run when a complete session override exists"
    )


def test_run_agent_prefers_session_override_over_global_runtime(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _explode_runtime_resolution)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner()

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )
    session_key = "agent:main:local:dm"
    runner._session_model_overrides[session_key] = _codex_override()
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == "gpt-5.4"
    assert _CapturingAgent.last_init["provider"] == "openai-codex"
    assert _CapturingAgent.last_init["api_mode"] == "codex_responses"
    assert _CapturingAgent.last_init["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert _CapturingAgent.last_init["api_key"] == "***"
    assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "high"}


@pytest.mark.asyncio
async def test_background_task_prefers_session_override_over_global_runtime(monkeypatch):
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _explode_runtime_resolution)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner()

    adapter = AsyncMock()
    adapter.send = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], "ok"))
    adapter.extract_images = MagicMock(return_value=([], "ok"))
    runner.adapters[Platform.TELEGRAM] = adapter

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="12345",
        chat_id="67890",
        user_name="testuser",
    )
    session_key = runner._session_key_for_source(source)
    runner._session_model_overrides[session_key] = _codex_override()
    runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}

    await runner._run_background_task("say hello", source, "bg_test")

    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == "gpt-5.4"
    assert _CapturingAgent.last_init["provider"] == "openai-codex"
    assert _CapturingAgent.last_init["api_mode"] == "codex_responses"
    assert _CapturingAgent.last_init["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert _CapturingAgent.last_init["api_key"] == "***"
    assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "high"}

def test_gateway_auth_fallback_uses_fallback_model_from_config(tmp_path, monkeypatch):
    """Regression: fallback provider must not inherit the primary model.

    If primary openai-codex auth fails and fallback_providers selects
    OpenRouter/minimax, the gateway must instantiate AIAgent with the fallback
    model, not the primary config model (e.g. gpt-5.5). Otherwise OpenRouter
    receives an unintended GPT request.
    """
    config = tmp_path / "config.yaml"
    config.write_text(
        """
model:
  default: gpt-5.5
  provider: openai-codex
fallback_providers:
  - provider: openrouter
    model: minimax/minimax-m2.7
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    def fake_resolve_runtime_provider(
        *, requested=None, target_model=None, explicit_base_url=None,
        explicit_api_key=None
    ):
        if requested in {None, "", "openai-codex"}:
            from hermes_cli.auth import AuthError
            raise AuthError("No Codex credentials stored. Run `hermes auth` to authenticate.")
        assert requested == "openrouter"
        return {
            "api_key": "sk-openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }

    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fake_resolve_runtime_provider)

    runner = _make_runner()
    model, runtime_kwargs = runner._resolve_session_agent_runtime(
        session_key="agent:main:telegram:group:-1003715515980:63",
        user_config={
            "model": {"default": "gpt-5.5", "provider": "openai-codex"},
            "fallback_providers": [{"provider": "openrouter", "model": "minimax/minimax-m2.7"}],
        },
    )

    assert model == "minimax/minimax-m2.7"
    assert runtime_kwargs["provider"] == "openrouter"
    assert runtime_kwargs["api_key"] == "sk-openrouter"


def test_gateway_auth_fallback_resolves_key_env_for_custom_provider(tmp_path, monkeypatch):
    """Auth-failure fallback should honor key_env/api_key_env custom-endpoint hints."""
    config = tmp_path / "config.yaml"
    config.write_text(
        """
fallback_providers:
  - provider: custom
    model: fallback-model
    base_url: https://fallback.example/v1
    key_env: MY_FALLBACK_KEY
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("MY_FALLBACK_KEY", "env-secret")

    def fake_resolve_runtime_provider(
        *, requested=None, target_model=None, explicit_base_url=None,
        explicit_api_key=None
    ):
        assert requested == "custom"
        assert explicit_base_url == "https://fallback.example/v1"
        assert explicit_api_key == "env-secret"
        return {
            "api_key": explicit_api_key,
            "base_url": explicit_base_url,
            "provider": "custom",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }

    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fake_resolve_runtime_provider)

    runtime_kwargs = gateway_run._try_resolve_fallback_provider()

    assert runtime_kwargs is not None
    assert runtime_kwargs["provider"] == "custom"
    assert runtime_kwargs["api_key"] == "env-secret"
    assert runtime_kwargs["base_url"] == "https://fallback.example/v1"
    assert runtime_kwargs["model"] == "fallback-model"


def test_supervisor_controller_override_routes_openrouter_with_codex_fallback(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_ROLE_SHELL_ID", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-test")
    kb.init_db()
    with kb.connect_closing() as conn:
        codex = sr.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_codex",
            name="Codex controller",
            provider="openai-codex",
            model="gpt-5.6-sol",
            base_url="https://chatgpt.com/backend-api/codex",
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
            session_id="gateway-session-key",
            created_by="test",
        )

    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openai-codex",
            "api_key": "codex-test",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_app_server",
        },
    )

    import hermes_cli.runtime_provider as runtime_provider

    def fake_resolve_runtime_provider(
        *, requested=None, target_model=None, explicit_base_url=None,
        explicit_api_key=None
    ):
        assert requested == "openrouter"
        assert target_model == "google/gemma-4-26b-a4b-it:free"
        assert explicit_base_url == "https://openrouter.ai/api/v1"
        assert explicit_api_key == "sk-openrouter-test"
        return {
            "api_key": explicit_api_key,
            "base_url": explicit_base_url,
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }

    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider", fake_resolve_runtime_provider
    )
    runner = _make_runner()
    model, runtime = runner._resolve_session_agent_runtime(
        session_key="gateway-session-key",
        user_config={
            "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
            "supervisor": {"enabled": True},
        },
    )

    assert model == "google/gemma-4-26b-a4b-it:free"
    assert runtime["provider"] == "openrouter"
    assert runtime["api_key"] == "sk-openrouter-test"
    route = runner._resolve_turn_agent_config("hello", model, runtime)
    assert route["controller_override"]["override_id"] == override.id
    assert route["fallback_model"] == [
        {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }
    ]

    fake_agent = types.SimpleNamespace(
        provider="openrouter",
        model="google/gemma-4-26b-a4b-it:free",
        _fallback_activated=False,
    )
    runner._record_supervisor_controller_turn(route, agent=fake_agent, failed=False)
    with kb.connect_closing() as conn:
        consumed = sr.get_controller_override(conn, override.id)
        assert consumed is not None and consumed.enabled is False
        assert consumed.remaining_uses == 0


def test_opencode_controller_runtime_has_model_then_codex_fallbacks(monkeypatch):
    adapter = types.SimpleNamespace(
        provider="opencode-zen",
        model="hy3-free",
        base_url="https://opencode.ai/zen/v1",
        api_mode="chat_completions",
        key_env=None,
        metadata={
            "anonymous_api": True,
            "free_model_suffix": "-free",
            "free_model_ids": ["big-pickle"],
            "model_fallback_candidates": [
                "hy3-free",
                "big-pickle",
                "nemotron-3-ultra-free",
            ],
        },
    )

    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **kwargs: {
            "api_key": None,
            "base_url": kwargs["explicit_base_url"],
            "provider": kwargs["requested"],
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        },
    )

    runtime = gateway_run.GatewayRunner._resolve_controller_candidate_runtime(
        adapter
    )
    fallbacks = gateway_run.GatewayRunner._controller_model_fallback_entries(
        adapter
    )
    assert runtime["api_key"] == "no-key-required"
    assert fallbacks == [
        {
            "provider": "opencode-zen",
            "model": "big-pickle",
            "base_url": "https://opencode.ai/zen/v1",
            "api_key": "no-key-required",
        },
        {
            "provider": "opencode-zen",
            "model": "nemotron-3-ultra-free",
            "base_url": "https://opencode.ai/zen/v1",
            "api_key": "no-key-required",
        }
    ]
