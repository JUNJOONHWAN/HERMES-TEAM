"""OpenCode Zen free models must use the provider's anonymous HTTP path."""

from unittest.mock import MagicMock, patch

import httpx

from run_agent import AIAgent


@patch("run_agent.OpenAI")
def test_anonymous_opencode_zen_free_strips_sdk_authorization(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="no-key-required",
        base_url="https://opencode.ai/zen/v1",
        provider="opencode-zen",
        api_mode="chat_completions",
        model="hy3-free",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    matching = [
        call
        for call in mock_openai.call_args_list
        if call.kwargs.get("base_url") == "https://opencode.ai/zen/v1"
    ]
    assert matching
    forwarded = matching[-1].kwargs
    assert forwarded["default_headers"]["User-Agent"].startswith("HermesAgent/")
    http_client = forwarded["http_client"]
    request = httpx.Request(
        "POST",
        "https://opencode.ai/zen/v1/chat/completions",
        headers={"Authorization": "Bearer no-key-required"},
    )
    for hook in http_client.event_hooks["request"]:
        hook(request)
    assert "Authorization" not in request.headers
    http_client.close()


@patch("run_agent.OpenAI")
def test_paid_opencode_zen_keeps_normal_sdk_auth(mock_openai):
    mock_openai.return_value = MagicMock()
    AIAgent(
        api_key="real-zen-key",
        base_url="https://opencode.ai/zen/v1",
        provider="opencode-zen",
        api_mode="chat_completions",
        model="deepseek-v4-flash",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    forwarded = mock_openai.call_args.kwargs
    hooks = forwarded["http_client"].event_hooks["request"]
    assert _strip_hook_names(hooks) == []
    forwarded["http_client"].close()


@patch("run_agent.OpenAI")
def test_declared_anonymous_model_without_free_suffix_strips_auth(mock_openai):
    mock_openai.return_value = MagicMock()
    AIAgent(
        api_key="no-key-required",
        base_url="https://opencode.ai/zen/v1",
        provider="opencode-zen",
        api_mode="chat_completions",
        model="big-pickle",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    forwarded = mock_openai.call_args.kwargs
    request = httpx.Request(
        "POST",
        "https://opencode.ai/zen/v1/chat/completions",
        headers={"Authorization": "Bearer no-key-required"},
    )
    for hook in forwarded["http_client"].event_hooks["request"]:
        hook(request)
    assert "Authorization" not in request.headers
    forwarded["http_client"].close()


def _strip_hook_names(hooks):
    return [
        getattr(hook, "__name__", "")
        for hook in hooks
        if "anonymous_opencode_zen" in getattr(hook, "__name__", "")
    ]
