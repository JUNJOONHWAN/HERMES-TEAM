from __future__ import annotations

import json

import pytest
import yaml

from scripts import build_supervisor_mcp_bundle as exporter


def _source(tmp_path, *, names=None, omit=None):
    servers = {}
    for name in names or (*exporter.DEFAULT_SERVERS, "example-market-data"):
        if name == omit:
            continue
        servers[name] = {
            "command": "/opt/mcp/runner",
            "args": [f"{name}.py"],
            "env": {"SHARED_PATH": "/srv/shared"} if name.startswith("hermes") else {},
            "timeout": 180,
        }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"mcp_servers": servers}), encoding="utf-8")
    return path


def test_export_default_timeline_bundle_for_claude_and_opencode(tmp_path):
    claude = tmp_path / "claude.json"
    opencode = tmp_path / "opencode.json"
    manifest = tmp_path / "manifest.json"

    result = exporter.export_bundle(
        source=_source(tmp_path),
        claude_output=claude,
        opencode_output=opencode,
        manifest_output=manifest,
    )

    claude_payload = json.loads(claude.read_text(encoding="utf-8"))
    opencode_payload = json.loads(opencode.read_text(encoding="utf-8"))
    assert set(claude_payload["mcpServers"]) == set(exporter.DEFAULT_SERVERS)
    assert set(opencode_payload["mcp"]) == set(exporter.DEFAULT_SERVERS)
    assert opencode_payload["mcp"]["hermes-timeline-code-map"] == {
        "type": "local",
        "command": ["/opt/mcp/runner", "hermes-timeline-code-map.py"],
        "enabled": True,
        "environment": {"SHARED_PATH": "/srv/shared"},
    }
    assert claude_payload["mcpServers"]["hermes-timeline-code-map"]["env"] == {
        "SHARED_PATH": "/srv/shared"
    }
    assert result["root_mcp_unchanged"] is True
    assert json.loads(manifest.read_text(encoding="utf-8"))["servers"] == list(
        exporter.DEFAULT_SERVERS
    )


def test_export_role_specific_bundle_and_refuse_missing_alias(tmp_path):
    selected = ("hermes-timeline-code-map", "example-market-data")
    result = exporter.export_bundle(
        source=_source(tmp_path),
        claude_output=tmp_path / "claude.json",
        opencode_output=tmp_path / "opencode.json",
        manifest_output=tmp_path / "manifest.json",
        servers=selected,
    )
    assert result["servers"] == list(selected)

    with pytest.raises(ValueError, match="missing-server"):
        exporter.export_bundle(
            source=_source(tmp_path),
            claude_output=tmp_path / "claude.json",
            opencode_output=tmp_path / "opencode.json",
            manifest_output=tmp_path / "manifest.json",
            servers=("hermes-timeline-code-map", "missing-server"),
        )
