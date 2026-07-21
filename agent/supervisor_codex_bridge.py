"""Hermes supervisor identity and local Codex dynamic-tool bridge.

The Codex app-server owns its own model loop, so Hermes' normal system prompt
and in-process tool registry do not reach it automatically.  This module
provides the intentionally small control-plane surface that the central
supervisor needs without exposing an MCP server or domain-work tools.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agent.supervisor_contract import (
    SUPERVISOR_CONTROL_TOOL_SEQUENCE,
    SUPERVISOR_DEVELOPER_INSTRUCTIONS,
)
from hermes_constants import get_hermes_home
from hermes_cli.config import load_config_readonly
from hermes_cli.supervisor_registry import supervisor_root_enabled


_SUPERVISOR_TOOL_NAMES = SUPERVISOR_CONTROL_TOOL_SEQUENCE


@dataclass(frozen=True)
class SupervisorCodexContext:
    developer_instructions: str
    dynamic_tools: list[dict[str, Any]]
    dynamic_tool_handler: Callable[[str, dict[str, Any]], tuple[bool, str]]
    cwd: str
    codex_home: Optional[str]


def _platform_toolset_names(config: dict[str, Any], platform: str) -> set[str]:
    raw = (config.get("platform_toolsets") or {}).get(platform, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def supervisor_codex_enabled(agent: Any) -> bool:
    """Return whether *agent* is the configured central supervisor root."""
    config = load_config_readonly()
    if not supervisor_root_enabled(config):
        return False
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    configured = _platform_toolset_names(config, platform)
    enabled = {
        str(item).strip()
        for item in (getattr(agent, "enabled_toolsets", None) or [])
        if str(item).strip()
    }
    return "supervisor" in configured or "supervisor" in enabled


def supervisor_no_mcp_enabled(agent: Any) -> bool:
    """Return the raw per-platform no_mcp intent (before toolset expansion)."""
    config = load_config_readonly()
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    return "no_mcp" in _platform_toolset_names(config, platform)


def supervisor_dynamic_tools() -> list[dict[str, Any]]:
    """Return Codex app-server dynamic-tool specs for the control plane only."""
    # Importing the module registers the fixed supervisor tools in the central
    # in-process registry.  Keep the import local so non-supervisor runtimes do
    # not pay for the Kanban/supervisor dependency graph.
    import tools.supervisor_tools  # noqa: F401
    from tools.registry import registry as tool_registry

    specs: list[dict[str, Any]] = []
    for name in _SUPERVISOR_TOOL_NAMES:
        schema = tool_registry.get_schema(name)
        if not isinstance(schema, dict):
            raise RuntimeError(f"Hermes supervisor tool is not registered: {name}")
        specs.append(
            {
                "type": "function",
                "name": name,
                "description": str(schema.get("description") or ""),
                "inputSchema": schema.get("parameters")
                or {"type": "object", "properties": {}},
                "deferLoading": False,
            }
        )
    return specs


def _trusted_notification_route() -> Optional[dict[str, str]]:
    """Capture the gateway-owned async return route for this conversation.

    The model never supplies these values. They come only from the task-local
    gateway ContextVars, preventing one conversation from subscribing a card
    to another chat while still allowing the notifier to wake the originating
    Telegram/Discord/Slack session after the controller turn has ended.
    """
    try:
        from gateway.session_context import async_delivery_supported, get_session_env

        if not async_delivery_supported():
            return None
        platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
        if not platform or not chat_id:
            return None
        return {
            "platform": platform,
            "chat_id": chat_id,
            "chat_type": get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip(),
            "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "").strip(),
            "user_id": get_session_env("HERMES_SESSION_USER_ID", "").strip(),
            "notifier_profile": get_session_env(
                "HERMES_SESSION_PROFILE", ""
            ).strip(),
        }
    except Exception:
        return None


def make_supervisor_dynamic_tool_handler(
    agent: Any,
) -> Callable[[str, dict[str, Any]], tuple[bool, str]]:
    """Bind the fixed supervisor registry surface to one Hermes session."""
    import tools.supervisor_tools  # noqa: F401
    from tools.registry import registry as tool_registry

    allowed = set(_SUPERVISOR_TOOL_NAMES)
    session_id = str(getattr(agent, "session_id", "") or "").strip() or None
    notification_route = _trusted_notification_route()

    def _handle(name: str, args: dict[str, Any]) -> tuple[bool, str]:
        if name not in allowed:
            return False, json.dumps(
                {"error": f"Unsupported Hermes supervisor tool: {name}"},
                ensure_ascii=False,
            )
        dispatch_args = dict(args)
        if (
            name == "supervisor_adapter"
            and str(dispatch_args.get("action") or "list").strip().lower() == "list"
        ):
            # The full registry repeats identical runtime blobs in shells,
            # slots, and every binding. The compact view retains every
            # operator-facing fact and joins runtime details through the
            # executor roster, so the controller must not expand it back to
            # the 40k+ raw payload.
            dispatch_args["view"] = "compact"
        dispatch_context: dict[str, Any] = {"session_id": session_id}
        if notification_route is not None:
            dispatch_context["notification_route"] = dict(notification_route)
        result = tool_registry.dispatch(name, dispatch_args, **dispatch_context)
        text = result if isinstance(result, str) else json.dumps(
            result, ensure_ascii=False, default=str
        )
        success = True
        try:
            parsed = json.loads(text)
            success = not (isinstance(parsed, dict) and parsed.get("error"))
        except (TypeError, json.JSONDecodeError):
            pass
        return success, text

    return _handle


def _supervisor_codex_config(agent: Any) -> str:
    """Render the minimal controller-only Codex configuration."""
    model = str(getattr(agent, "model", "") or "gpt-5.6-sol").strip()
    reasoning = getattr(agent, "reasoning_config", None) or {}
    effort = str(reasoning.get("effort") or "medium").strip().lower()
    if effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        effort = "medium"
    return "\n".join(
        [
            f"model = {json.dumps(model)}",
            f"model_reasoning_effort = {json.dumps(effort)}",
            'approval_policy = "never"',
            'sandbox_mode = "read-only"',
            "notify = []",
            "",
            "[features]",
            "apps = false",
            "plugins = false",
            "remote_plugin = false",
            "plugin_sharing = false",
            "hooks = false",
            "browser_use = false",
            "browser_use_external = false",
            "browser_use_full_cdp_access = false",
            "computer_use = false",
            "in_app_browser = false",
            "image_generation = false",
            "shell_tool = false",
            "unified_exec = false",
            "multi_agent = false",
            "code_mode_host = false",
            "workspace_dependencies = false",
            "skill_mcp_dependency_install = false",
            "tool_suggest = false",
            "goals = false",
            "",
        ]
    )


def prepare_supervisor_codex_runtime(agent: Any) -> tuple[str, str]:
    """Create the isolated controller workspace and MCP-free CODEX_HOME.

    The isolated home shares only the Codex authentication file. It does not
    inherit the operator's config.toml, plugins, MCP definitions, skills,
    memories, or repo-level Codex session state.
    """
    root = get_hermes_home() / "supervisor"
    codex_home = root / "codex_home"
    workspace = root / "workspace"
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(codex_home, 0o700)
    os.chmod(workspace, 0o700)

    source_root_value = os.environ.get("CODEX_HOME")
    source_root = (
        Path(source_root_value).expanduser()
        if source_root_value
        else Path.home() / ".codex"
    )
    source_auth = (source_root / "auth.json").resolve()
    if not source_auth.is_file():
        raise RuntimeError(
            f"Cannot prepare Hermes supervisor Codex runtime: missing {source_auth}"
        )
    target_auth = codex_home / "auth.json"
    if target_auth.is_symlink():
        if target_auth.resolve() != source_auth:
            target_auth.unlink()
            target_auth.symlink_to(source_auth)
    elif target_auth.exists():
        if not target_auth.samefile(source_auth):
            raise RuntimeError(
                f"Refusing to replace unmanaged supervisor auth file: {target_auth}"
            )
    else:
        target_auth.symlink_to(source_auth)

    config_path = codex_home / "config.toml"
    config_text = _supervisor_codex_config(agent)
    if not config_path.exists() or config_path.read_text(encoding="utf-8") != config_text:
        tmp_path = codex_home / f".config.toml.tmp-{os.getpid()}"
        tmp_path.write_text(config_text, encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
    os.chmod(config_path, 0o600)
    return str(workspace), str(codex_home)


def build_supervisor_codex_context(
    agent: Any,
) -> Optional[SupervisorCodexContext]:
    """Return the fixed controller context, or None for normal workers."""
    if not supervisor_codex_enabled(agent):
        return None
    cwd = str(get_hermes_home() / "supervisor" / "workspace")
    codex_home: Optional[str] = None
    if supervisor_no_mcp_enabled(agent):
        cwd, codex_home = prepare_supervisor_codex_runtime(agent)
    else:
        Path(cwd).mkdir(parents=True, exist_ok=True, mode=0o700)
    return SupervisorCodexContext(
        developer_instructions=SUPERVISOR_DEVELOPER_INSTRUCTIONS,
        dynamic_tools=supervisor_dynamic_tools(),
        dynamic_tool_handler=make_supervisor_dynamic_tool_handler(agent),
        cwd=cwd,
        codex_home=codex_home,
    )
