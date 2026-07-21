import json

import yaml

from hermes_cli.supervisor_tool_catalog import (
    build_tool_catalog,
    compact_tool_catalog,
    search_tool_catalog,
)
from tools import supervisor_tools


def _profile(home, name, *, toolsets=(), mcp_servers=(), skills=()):
    root = home / "profiles" / name
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "toolsets": list(toolsets),
                "mcp_servers": {server: {"command": "safe"} for server in mcp_servers},
            }
        ),
        encoding="utf-8",
    )
    for skill in skills:
        skill_dir = root / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")


def test_tool_catalog_reports_role_scoped_assignments_without_mcp_secrets(tmp_path):
    _profile(
        tmp_path,
        "hermes-worker-market",
        toolsets=("file", "example-market-data"),
        mcp_servers=("hermes-timeline-code-map", "example-market-data"),
        skills=("market-gate",),
    )
    _profile(
        tmp_path,
        "hermes-worker-multitool",
        toolsets=("file", "terminal", "skills"),
        mcp_servers=("hermes-timeline-code-map",),
        skills=("skill-installer",),
    )
    (tmp_path / "plugins" / "example-plugin").mkdir(parents=True)

    catalog = build_tool_catalog(tmp_path)

    assert catalog["healthy"] is True
    assert catalog["policy"] == "role_scoped_not_install_everywhere"
    assert catalog["profile_count"] == 2
    assert catalog["assignments"]["mcp_servers"] == {
        "hermes-timeline-code-map": [
            "hermes-worker-market", "hermes-worker-multitool"
        ],
        "example-market-data": ["hermes-worker-market"],
    }
    assert catalog["plugins"] == ["example-plugin"]
    assert "read_file" in catalog["assignments"]["callable_tools"]
    assert "terminal" in catalog["assignments"]["callable_tools"]
    assert "command" not in json.dumps(catalog)
    assert "safe" not in json.dumps(catalog)


def test_compact_tool_catalog_is_mobile_width_and_explains_policy(tmp_path):
    _profile(
        tmp_path,
        "hermes-worker-multitool",
        toolsets=("file", "terminal", "skills"),
        mcp_servers=("hermes-timeline-code-map",),
    )

    compact = compact_tool_catalog(build_tool_catalog(tmp_path))

    assert compact["operator_text"].startswith("툴 관리 정상\n")
    assert "└ 멀티툴: MCP 1 · 스킬 0 · 툴셋 3" in compact["operator_text"]
    assert "원칙: 역할별 최소 장착" in compact["operator_text"]
    assert all(len(line) <= 40 for line in compact["operator_text"].splitlines())


def test_supervisor_adapter_tools_action_uses_profile_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(
        tmp_path,
        "hermes-worker-multitool",
        toolsets=("file", "terminal", "skills"),
        mcp_servers=("hermes-timeline-code-map",),
    )

    result = json.loads(
        supervisor_tools._handle_adapter({"action": "tools", "view": "compact"})
    )

    assert result["schema"] == "hermes.supervisor.tool_catalog.v1"
    assert result["profile_count"] == 1
    assert result["view"] == "operator_compact"


def test_supervisor_adapter_tools_action_searches_the_central_catalog(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(
        tmp_path,
        "hermes-worker-browser",
        toolsets=("web", "browser"),
        mcp_servers=("example-browser",),
    )

    result = json.loads(
        supervisor_tools._handle_adapter(
            {"action": "tools", "query": "고급 브라우징", "view": "compact"}
        )
    )

    assert result["query"] == "고급 브라우징"
    assert any(
        row["name"] == "example-browser" for row in result["matches"]
    )
    assert any(
        row["name"] == "browser_navigate" for row in result["matches"]
    )


def test_tool_catalog_semantic_search_maps_portal_research_to_browser_candidates(tmp_path):
    _profile(
        tmp_path,
        "hermes-worker-browser",
        toolsets=("web", "browser"),
        mcp_servers=("example-browser",),
    )

    candidates = search_tool_catalog(
        build_tool_catalog(tmp_path), "로그인형 포털 고급 브라우징"
    )

    assert any(row["name"] == "example-browser" for row in candidates)
    assert any(row["name"] == "browser" for row in candidates)
    assert any(row["name"] == "browser_navigate" for row in candidates)


def test_tool_catalog_semantic_search_treats_browser_query_as_discovery(
    tmp_path,
):
    _profile(
        tmp_path,
        "hermes-worker-browser",
        toolsets=("web", "browser"),
        mcp_servers=("example-browser",),
    )

    candidates = search_tool_catalog(build_tool_catalog(tmp_path), "브라우저 검색")

    assert any(row["name"] == "example-browser" for row in candidates)
    assert any(row["name"] == "web_search" for row in candidates)
