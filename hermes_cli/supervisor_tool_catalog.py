"""Deterministic role-scoped tool inventory for the Hermes supervisor.

The catalog is deliberately read-only.  Installation and removal remain
Kanban work owned by the ``tool-management`` role so every mutation has a
worker, receipt, validation evidence, and rollback path.  This module exposes
the current assignment truth without leaking MCP definitions or credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home
from toolsets import resolve_toolset


_PROFILE_ROLE_LABELS = {
    "hermes-worker-browser": "브라우저",
    "hermes-worker-general": "코드·운영·검증",
    "hermes-worker-market": "시장·리포트",
    "hermes-worker-universal": "범용 후보",
    "hermes-worker-multitool": "멀티툴",
}


def _load_profile_config(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.is_file():
        return {}, "config.yaml 없음"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {}, f"config.yaml 읽기 실패: {exc}"
    if not isinstance(value, dict):
        return {}, "config.yaml 최상위가 객체가 아님"
    return value, None


def _skill_names(skills_dir: Path) -> list[str]:
    if not skills_dir.is_dir():
        return []
    names: set[str] = set()
    for manifest in skills_dir.rglob("SKILL.md"):
        if manifest.is_file():
            names.add(manifest.parent.name)
    return sorted(names)


def _installed_plugins(home: Path) -> list[str]:
    plugins = home / "plugins"
    if not plugins.is_dir():
        return []
    return sorted(
        path.name
        for path in plugins.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def _inverse_assignment(
    profiles: list[dict[str, Any]], field: str
) -> dict[str, list[str]]:
    assignment: dict[str, list[str]] = {}
    for profile in profiles:
        for item in profile[field]:
            assignment.setdefault(item, []).append(profile["profile"])
    return {key: sorted(value) for key, value in sorted(assignment.items())}


def _resolved_builtin_tools(toolsets: list[str]) -> list[str]:
    """Expand static Hermes toolsets without starting MCP servers."""
    names: set[str] = set()
    for toolset in toolsets:
        names.update(resolve_toolset(toolset, include_registry=False))
    return sorted(names)


def _declared_mcp_tools(mcp_servers: dict[str, Any]) -> list[str]:
    """Return explicit MCP include lists when a profile declares them.

    An MCP server with no include list is intentionally represented by its
    server name only. Discovering its live schema belongs to a Multitool probe,
    not the fast read-only status path.
    """
    names: set[str] = set()
    for config in mcp_servers.values():
        if not isinstance(config, dict):
            continue
        tools = config.get("tools") or {}
        if not isinstance(tools, dict):
            continue
        include = tools.get("include") or []
        if isinstance(include, list):
            names.update(str(item) for item in include if str(item))
    return sorted(names)


def build_tool_catalog(
    home: Path | None = None, *, conn: Any | None = None
) -> dict[str, Any]:
    """Return names-only tool assignments for every configured worker profile."""
    home = Path(home) if home is not None else get_hermes_home()
    profiles_dir = home / "profiles"
    profile_dirs = (
        sorted(path for path in profiles_dir.iterdir() if path.is_dir())
        if profiles_dir.is_dir()
        else []
    )
    profiles: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for profile_dir in profile_dirs:
        config, error = _load_profile_config(profile_dir / "config.yaml")
        toolsets = config.get("toolsets") or []
        mcp_servers = config.get("mcp_servers") or {}
        if not isinstance(toolsets, list):
            error = error or "toolsets가 배열이 아님"
            toolsets = []
        if not isinstance(mcp_servers, dict):
            error = error or "mcp_servers가 객체가 아님"
            mcp_servers = {}
        normalized_toolsets = sorted(
            {str(item) for item in toolsets if str(item)}
        )
        builtin_tools = _resolved_builtin_tools(normalized_toolsets)
        declared_mcp_tools = _declared_mcp_tools(mcp_servers)
        row = {
            "profile": profile_dir.name,
            "role": _PROFILE_ROLE_LABELS.get(profile_dir.name, "기타 프로필"),
            "toolsets": normalized_toolsets,
            "mcp_servers": sorted(str(name) for name in mcp_servers),
            "skills": _skill_names(profile_dir / "skills"),
            "builtin_tools": builtin_tools,
            "declared_mcp_tools": declared_mcp_tools,
            "callable_tools": sorted(set(builtin_tools) | set(declared_mcp_tools)),
            "healthy": error is None,
        }
        profiles.append(row)
        if error:
            errors.append({"profile": profile_dir.name, "error": error})

    assignments = {
        "toolsets": _inverse_assignment(profiles, "toolsets"),
        "mcp_servers": _inverse_assignment(profiles, "mcp_servers"),
        "skills": _inverse_assignment(profiles, "skills"),
        "callable_tools": _inverse_assignment(profiles, "callable_tools"),
    }
    plugins = _installed_plugins(home)
    executors: list[dict[str, Any]] = []
    executor_capabilities: dict[str, list[str]] = {}
    if conn is not None:
        from hermes_cli import supervisor_registry

        for executor in supervisor_registry.list_executors(conn):
            capabilities = sorted(str(item) for item in executor.capabilities)
            executors.append(
                {
                    "executor_id": executor.id,
                    "name": executor.name,
                    "adapter_type": executor.adapter_type,
                    "enabled": bool(executor.enabled),
                    "health_state": executor.health_state,
                    "capabilities": capabilities,
                }
            )
            for capability in capabilities:
                executor_capabilities.setdefault(capability, []).append(
                    executor.id
                )
        executor_capabilities = {
            key: sorted(value)
            for key, value in sorted(executor_capabilities.items())
        }
    return {
        "schema": "hermes.supervisor.tool_catalog.v1",
        "healthy": not errors,
        "policy": "role_scoped_not_install_everywhere",
        "profile_count": len(profiles),
        "counts": {
            "toolsets": len(assignments["toolsets"]),
            "mcp_servers": len(assignments["mcp_servers"]),
            "skills": len(assignments["skills"]),
            "callable_tools": len(assignments["callable_tools"]),
            "plugins": len(plugins),
            "executor_capabilities": len(executor_capabilities),
        },
        "profiles": profiles,
        "assignments": assignments,
        "plugins": plugins,
        "executors": executors,
        "executor_capabilities": executor_capabilities,
        "errors": errors,
    }


def search_tool_catalog(
    catalog: dict[str, Any], query: str, *, limit: int = 12
) -> list[dict[str, Any]]:
    """Return names-only catalog candidates for a semantic missing capability."""
    text = str(query or "").strip().casefold()
    if not text:
        return []
    aliases = {
        "브라우": ("browser", "web", "search"),
        "고급": ("advanced", "browser", "web"),
        "시장": ("market", "finance", "quote", "web"),
        "시황": ("market", "finance", "quote", "web"),
        "노하우": ("knowledge", "memory", "file"),
        "코드": ("file", "terminal", "code"),
    }
    terms = {part for part in text.replace("/", " ").split() if len(part) >= 2}
    for marker, expansions in aliases.items():
        if marker in text:
            terms.update(expansions)
    candidates: list[dict[str, Any]] = []

    def add(kind: str, name: str, owners: list[str]) -> None:
        haystack = f"{kind} {name} {' '.join(owners)}".casefold()
        score = sum(1 for term in terms if term in haystack)
        if score:
            candidates.append(
                {"kind": kind, "name": name, "owners": owners, "score": score}
            )

    for kind in ("mcp_servers", "skills", "toolsets", "callable_tools"):
        for name, owners in (catalog.get("assignments", {}).get(kind) or {}).items():
            add(kind, str(name), [str(owner) for owner in owners])
    for name in catalog.get("plugins") or []:
        add("plugins", str(name), [])
    for name, owners in (catalog.get("executor_capabilities") or {}).items():
        add("executor_capabilities", str(name), [str(owner) for owner in owners])
    kind_order = {
        "mcp_servers": 0,
        "skills": 1,
        "toolsets": 2,
        "plugins": 3,
        "executor_capabilities": 4,
        "callable_tools": 5,
    }
    return sorted(
        candidates,
        key=lambda row: (
            -int(row["score"]),
            kind_order.get(str(row["kind"]), 99),
            row["name"],
        ),
    )[: max(1, int(limit))]


def compact_tool_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Add an iPhone-width deterministic operator screen to a catalog."""
    counts = catalog.get("counts") or {}
    lines = [
        "툴 관리 정상" if catalog.get("healthy") else "툴 관리 주의",
        (
            f"프로필 {catalog.get('profile_count', 0)} · "
            f"MCP {counts.get('mcp_servers', 0)} · "
            f"스킬 {counts.get('skills', 0)} · "
            f"툴셋 {counts.get('toolsets', 0)}"
        ),
    ]
    profiles = catalog.get("profiles") or []
    for index, row in enumerate(profiles):
        branch = "└" if index == len(profiles) - 1 else "├"
        lines.append(
            f"{branch} {row['role']}: MCP {len(row['mcp_servers'])} · "
            f"스킬 {len(row['skills'])} · 툴셋 {len(row['toolsets'])}"
        )
    lines.extend(
        [
            "",
            "원칙: 역할별 최소 장착",
            "설치·교체: 멀티툴 카드로 기록",
            "세션 적용: 새 실행부터",
        ]
    )
    return {
        "schema": catalog.get("schema"),
        "view": "operator_compact",
        "operator_text": "\n".join(lines),
        "healthy": bool(catalog.get("healthy")),
        "policy": catalog.get("policy"),
        "profile_count": catalog.get("profile_count"),
        "executor_count": len(catalog.get("executors") or []),
        "counts": counts,
        "profiles": profiles,
        "errors": catalog.get("errors") or [],
    }
