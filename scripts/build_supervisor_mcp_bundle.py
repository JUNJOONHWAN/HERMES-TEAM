#!/usr/bin/env python3
"""Export the canonical Hermes child MCP bundle for external agent CLIs."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SERVERS = ("hermes-timeline-code-map",)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _load_source(path: Path, servers: tuple[str, ...]) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected mapping in {path}")
    catalog = payload.get("mcp_servers") or {}
    if not isinstance(catalog, dict):
        raise ValueError("source mcp_servers must be a mapping")
    missing = sorted(set(servers) - set(catalog))
    if missing:
        raise ValueError("requested MCP bundle is incomplete: " + ", ".join(missing))
    return catalog


def build_claude_config(
    catalog: dict[str, Any], servers: tuple[str, ...] = DEFAULT_SERVERS
) -> dict[str, Any]:
    output_servers: dict[str, Any] = {}
    for name in servers:
        source = catalog[name]
        server = {
            "command": str(source["command"]),
            "args": [str(item) for item in source.get("args") or []],
        }
        if isinstance(source.get("env"), dict) and source["env"]:
            server["env"] = {str(k): str(v) for k, v in source["env"].items()}
        output_servers[name] = server
    return {"mcpServers": output_servers}


def build_opencode_config(
    catalog: dict[str, Any], servers: tuple[str, ...] = DEFAULT_SERVERS
) -> dict[str, Any]:
    output_servers: dict[str, Any] = {}
    for name in servers:
        source = catalog[name]
        command = [str(source["command"]), *[str(x) for x in source.get("args") or []]]
        server: dict[str, Any] = {
            "type": "local",
            "command": command,
            "enabled": True,
        }
        if isinstance(source.get("env"), dict) and source["env"]:
            server["environment"] = {
                str(k): str(v) for k, v in source["env"].items()
            }
        output_servers[name] = server
    return {"$schema": "https://opencode.ai/config.json", "mcp": output_servers}


def export_bundle(
    *,
    source: Path,
    claude_output: Path,
    opencode_output: Path,
    manifest_output: Path,
    servers: tuple[str, ...] = DEFAULT_SERVERS,
) -> dict[str, Any]:
    selected = tuple(dict.fromkeys(str(name).strip() for name in servers if str(name).strip()))
    if not selected:
        raise ValueError("at least one MCP server must be selected")
    catalog = _load_source(source, selected)
    _atomic_json(claude_output, build_claude_config(catalog, selected))
    _atomic_json(opencode_output, build_opencode_config(catalog, selected))
    manifest = {
        "schema": "hermes.supervisor.external-mcp-bundle.v1",
        "source": str(source.resolve()),
        "servers": list(selected),
        "claude_config": str(claude_output.resolve()),
        "opencode_config": str(opencode_output.resolve()),
        "root_mcp_unchanged": True,
    }
    _atomic_json(manifest_output, manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--claude-output", type=Path, required=True)
    parser.add_argument("--opencode-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument(
        "--server",
        action="append",
        dest="servers",
        help=(
            "MCP alias to export; repeat for role-specific additions. "
            "Defaults to hermes-timeline-code-map only."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = export_bundle(
        source=args.source.expanduser().resolve(),
        claude_output=args.claude_output.expanduser().resolve(),
        opencode_output=args.opencode_output.expanduser().resolve(),
        manifest_output=args.manifest_output.expanduser().resolve(),
        servers=tuple(args.servers or DEFAULT_SERVERS),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
