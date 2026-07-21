"""Installer primitives for the portable Hermes governance distribution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from hermes_cli import kanban_db as kb
from hermes_cli import supervisor_registry as registry
from hermes_cli.supervisor_bootstrap import TIMELINE_MCP, bootstrap_supervisor
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from utils import atomic_yaml_write


OPENCODE_PACKAGE = "opencode-ai"
OPENCODE_INSTALL_DOC = "https://opencode.ai/docs"
PUBLIC_SCHEMA = "hermes.public-edition.setup.v1"
OPENCODE_EXECUTOR_ID = "executor_opencode_free"
OPENCODE_PRIMARY_SHELLS = ("code", "operations", "report", "verification")


def _run(argv: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "output": (completed.stdout or "")[-4000:],
    }


def ensure_opencode(*, install: bool, dry_run: bool = False) -> dict[str, Any]:
    """Resolve OpenCode or install the official npm package without a shell."""
    existing = shutil.which("opencode")
    if existing:
        return {"status": "present", "path": existing, "installed": False}
    npm = shutil.which("npm")
    if not install:
        return {
            "status": "missing",
            "path": None,
            "installed": False,
            "remediation": f"npm install -g {OPENCODE_PACKAGE}",
        }
    if not npm:
        raise RuntimeError(
            "OpenCode is missing and npm is unavailable; install from "
            + OPENCODE_INSTALL_DOC
        )
    command = [npm, "install", "-g", OPENCODE_PACKAGE]
    if dry_run:
        return {"status": "planned", "argv": command, "installed": False}
    result = _run(command)
    if result["returncode"]:
        raise RuntimeError("OpenCode npm installation failed: " + result["output"])
    installed = shutil.which("opencode")
    if not installed:
        raise RuntimeError("OpenCode installed but is not visible on PATH")
    return {
        "status": "installed",
        "path": installed,
        "installed": True,
        "install_result": result,
    }


def install_timeline_extension(
    *, repo_root: Path, install: bool, dry_run: bool = False
) -> dict[str, Any]:
    extension = repo_root / "extensions" / "hermes-timeline-code-map"
    if not extension.joinpath("pyproject.toml").is_file():
        raise RuntimeError(f"bundled Timeline extension is missing: {extension}")
    command = [sys.executable, "-m", "pip", "install", "-e", f"{extension}[mcp]"]
    if not install:
        return {"status": "skipped", "path": str(extension), "argv": command}
    if dry_run:
        return {"status": "planned", "path": str(extension), "argv": command}
    result = _run(command, cwd=repo_root)
    if result["returncode"]:
        raise RuntimeError("Timeline extension installation failed: " + result["output"])
    return {"status": "installed", "path": str(extension), "result": result}


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


def configure_timeline_catalog(*, home: Path, dry_run: bool = False) -> dict[str, Any]:
    config_path = home / "config.yaml"
    if not config_path.is_file():
        raise RuntimeError(
            f"Hermes config not found: {config_path}. Run `hermes setup` first."
        )
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"Hermes config must be a mapping: {config_path}")
    timeline_db = home / "timeline_code_map" / "graph.db"
    server = {
        "command": sys.executable,
        "args": ["-m", "hermes_timeline_code_map.mcp_server"],
        "env": {"TIMELINE_CODE_MAP_DB_PATH": str(timeline_db)},
    }
    catalog = dict(payload.get("mcp_servers") or {})
    catalog[TIMELINE_MCP] = server
    payload["mcp_servers"] = catalog
    supervisor = dict(payload.get("supervisor") or {})
    supervisor["timeline_db"] = str(timeline_db)
    payload["supervisor"] = supervisor
    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            TIMELINE_MCP: {
                "type": "local",
                "command": [server["command"], *server["args"]],
                "environment": server["env"],
                "enabled": True,
            }
        },
    }
    opencode_path = home / "supervisor" / "mcp" / "opencode.json"
    if not dry_run:
        timeline_db.parent.mkdir(parents=True, exist_ok=True)
        atomic_yaml_write(config_path, payload, sort_keys=False)
        _atomic_json(opencode_path, opencode_config)
    return {
        "timeline_db": str(timeline_db),
        "root_catalog_staged": [TIMELINE_MCP],
        "opencode_config": str(opencode_path),
        "server": server,
    }


def install_neural_link_plugin(
    *, home: Path, repo_root: Path, dry_run: bool = False
) -> dict[str, Any]:
    source = (
        repo_root
        / "extensions"
        / "hermes-timeline-code-map"
        / "deploy"
        / "hermes_plugin"
        / "timeline-neural-link"
    )
    target = home / "plugins" / "timeline-neural-link"
    if not source.joinpath("plugin.yaml").is_file():
        raise RuntimeError(f"NeuralLink plugin is missing: {source}")
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
    return {
        "status": "planned" if dry_run else "installed",
        "source": str(source),
        "target": str(target),
        "hook": "pre_llm_call",
        "empty_recall_behavior": "no_context_injected",
    }


def _opencode_launch_config(
    *, home: Path, repo_root: Path, opencode_path: str
) -> dict[str, Any]:
    opencode_config = home / "supervisor" / "mcp" / "opencode.json"
    state_file = home / "supervisor" / "opencode_free_router_state.json"
    timeline_cli = repo_root / "scripts" / "hermes_timeline_cli.py"
    engine_argv = [
        sys.executable,
        "-m",
        "hermes_cli.opencode_free_router",
        "--prompt-file",
        "{prompt_file}",
        "--workspace",
        "{workspace}",
        "--config",
        str(opencode_config),
        "--state-file",
        str(state_file),
        "--opencode",
        opencode_path,
    ]
    bridge_argv = [
        sys.executable,
        "-m",
        "hermes_cli.external_cli_adapter",
        "--prompt-file",
        "{prompt_file}",
        "--engine-name",
        "opencode-free",
        "--engine-argv-json",
        json.dumps(engine_argv),
        "--timeline-client",
        str(timeline_cli),
        "--timeline-python",
        sys.executable,
        "--timeline-db",
        str(home / "timeline_code_map" / "graph.db"),
    ]
    health_argv = [
        sys.executable,
        "-m",
        "hermes_cli.opencode_free_router",
        "--health-check",
        "--workspace",
        str(repo_root),
        "--config",
        str(opencode_config),
        "--state-file",
        str(state_file),
        "--opencode",
        opencode_path,
    ]
    capabilities = ["file", "terminal", "kanban", TIMELINE_MCP]
    return {
        "argv": bridge_argv,
        "capability_enforcement": "env",
        "health_failure_confirmation_attempts": 2,
        "tool_contract": {
            "schema_version": 1,
            "transport": "adapter_brokered",
            "adapter_capabilities": capabilities,
            "native_capabilities": [],
            "required_mcp_servers": [],
            "probe": {
                "argv": health_argv,
                "required_output": [],
                "timeout_seconds": 120,
            },
        },
    }


def install_opencode_adapter(
    *,
    home: Path,
    repo_root: Path,
    opencode_path: str,
    live_health: bool,
) -> dict[str, Any]:
    db_path = home / "kanban.db"
    conn = kb.connect(db_path)
    try:
        capabilities = ("file", "terminal", "kanban", TIMELINE_MCP)
        executor = registry.upsert_executor(
            conn,
            executor_id=OPENCODE_EXECUTOR_ID,
            name="OpenCode free adapter",
            adapter_type="command",
            launch_config=_opencode_launch_config(
                home=home, repo_root=repo_root, opencode_path=opencode_path
            ),
            capabilities=capabilities,
            capacity=2,
            heartbeat_required=False,
            description=(
                "Default public-edition worker adapter. Live catalog discovery "
                "accepts only explicitly free OpenCode models."
            ),
        )
        registry.set_executor_enabled(conn, executor.id, False)
        binding_ids: list[str] = []
        for shell_key in OPENCODE_PRIMARY_SHELLS:
            shell = registry.resolve_shell(conn, shell_key)
            if shell is None:
                raise RuntimeError(f"active Role Shell is missing: {shell_key}")
            binding = registry.upsert_binding(
                conn,
                shell_id=shell.id,
                executor_id=executor.id,
                priority=200,
                weight=1.0,
                constraints={"auto_spawn": True},
                responsibility="candidate",
                assignment_note="OpenCode public-edition default candidate",
                assigned_by="public-edition-setup",
                binding_id=f"binding_{shell_key.replace('-', '_')}_opencode_free",
            )
            binding_ids.append(binding.id)
        health = {
            "requested_enabled": False,
            "enabled": False,
            "health_gate_passed": False,
            "reason": "live health check skipped",
        }
        if live_health:
            health = registry.set_executor_operational_state(
                conn,
                executor.id,
                enabled=True,
                reason="HERMES-TEAM OpenCode default health gate",
                changed_by="public-edition-setup",
            )
        if health.get("enabled"):
            for shell_key, binding_id in zip(OPENCODE_PRIMARY_SHELLS, binding_ids):
                shell = registry.resolve_shell(conn, shell_key)
                assert shell is not None
                with registry.write_txn(conn):
                    conn.execute(
                        "UPDATE role_bindings SET responsibility='candidate' "
                        "WHERE shell_id=? AND id<>? AND responsibility='primary'",
                        (shell.id, binding_id),
                    )
                current = registry.get_binding(conn, binding_id)
                assert current is not None
                registry.upsert_binding(
                    conn,
                    shell_id=current.shell_id,
                    executor_id=current.executor_id,
                    priority=current.priority,
                    weight=current.weight,
                    capability_cap=current.capability_cap,
                    constraints=current.constraints,
                    responsibility="primary",
                    assignment_note=current.assignment_note,
                    assigned_by=current.assigned_by,
                    binding_id=current.id,
                )
        return {
            "executor_id": executor.id,
            "bindings": binding_ids,
            "health": health,
        }
    finally:
        conn.close()


def enable_opencode_controller(*, home: Path, live_health: bool) -> dict[str, Any]:
    conn = kb.connect(home / "kanban.db")
    try:
        if not live_health:
            return {"enabled": False, "reason": "live health check skipped"}
        state = registry.set_controller_adapter_operational_state(
            conn,
            "controller_opencode_free",
            enabled=True,
            reason="HERMES-TEAM default controller",
            changed_by="public-edition-setup",
            timeout_seconds=20,
        )
        if state.get("enabled"):
            override = registry.create_controller_override(
                conn,
                controller_adapter_value="controller_opencode_free",
                mode="permanent",
                scope_type="all",
                reason="HERMES-TEAM OpenCode default",
                created_by="public-edition-setup",
            )
            state["override_id"] = override.id
        return state
    finally:
        conn.close()


def register_external_adapter(
    *,
    home: Path,
    repo_root: Path,
    spec: dict[str, Any],
    live_health: bool = True,
) -> dict[str, Any]:
    """Register a provider-neutral command adapter from an explicit JSON spec."""
    adapter_id = str(spec.get("id") or "").strip()
    name = str(spec.get("name") or adapter_id).strip()
    executable_value = str(spec.get("executable") or "").strip()
    if not adapter_id or not executable_value:
        raise ValueError("adapter spec requires id and executable")
    executable = (
        shutil.which(executable_value)
        if not Path(executable_value).is_absolute()
        else executable_value
    )
    if not executable or not Path(executable).is_file():
        raise RuntimeError(f"adapter executable is unavailable: {executable_value}")
    engine_template = spec.get("engine_argv") or []
    if not isinstance(engine_template, list) or not engine_template:
        raise ValueError("adapter engine_argv must be a non-empty list")
    engine_argv = [str(value).replace("{binary}", executable) for value in engine_template]
    if not any(
        "{prompt_file}" in value or "{prompt_text}" in value
        for value in engine_argv
    ):
        raise ValueError("adapter engine_argv must consume {prompt_file} or {prompt_text}")
    capabilities = sorted(
        set(str(value).strip() for value in spec.get("capabilities") or ())
        | {"kanban", TIMELINE_MCP}
    )
    shell_keys = [str(value).strip() for value in spec.get("shell_keys") or ()]
    if not shell_keys:
        raise ValueError("adapter shell_keys must not be empty")
    probe_template = spec.get("probe_argv") or ["{binary}", "--version"]
    if not isinstance(probe_template, list) or not probe_template:
        raise ValueError("adapter probe_argv must be a non-empty list")
    probe_argv = [str(value).replace("{binary}", executable) for value in probe_template]
    timeline_cli = repo_root / "scripts" / "hermes_timeline_cli.py"
    bridge_argv = [
        sys.executable,
        "-m",
        "hermes_cli.external_cli_adapter",
        "--prompt-file",
        "{prompt_file}",
        "--engine-name",
        name,
        "--engine-argv-json",
        json.dumps(engine_argv),
        "--timeline-client",
        str(timeline_cli),
        "--timeline-python",
        sys.executable,
        "--timeline-db",
        str(home / "timeline_code_map" / "graph.db"),
    ]
    research_policy = str(spec.get("research_policy") or "").strip()
    if research_policy:
        bridge_argv.extend(["--research-policy", research_policy])
    launch = {
        "argv": bridge_argv,
        "capability_enforcement": "env",
        "health_failure_confirmation_attempts": 2,
        "tool_contract": {
            "schema_version": 1,
            "transport": "adapter_brokered",
            "adapter_capabilities": capabilities,
            "native_capabilities": [],
            "required_mcp_servers": [],
            "probe": {
                "argv": probe_argv,
                "required_output": [
                    str(value) for value in spec.get("probe_required_output") or ()
                ],
                "timeout_seconds": float(spec.get("probe_timeout_seconds") or 30),
            },
        },
    }
    conn = kb.connect(home / "kanban.db")
    try:
        executor = registry.upsert_executor(
            conn,
            executor_id=adapter_id,
            name=name,
            adapter_type="command",
            launch_config=launch,
            capabilities=capabilities,
            capacity=int(spec.get("capacity") or 1),
            heartbeat_required=False,
            description=str(spec.get("description") or "") or None,
        )
        registry.set_executor_enabled(conn, executor.id, False)
        binding_ids: list[str] = []
        priority = int(spec.get("priority") or 50)
        for shell_key in shell_keys:
            shell = registry.resolve_shell(conn, shell_key)
            if shell is None:
                raise RuntimeError(f"unknown Role Shell: {shell_key}")
            if not set(shell.required_capabilities).issubset(capabilities):
                missing = sorted(set(shell.required_capabilities) - set(capabilities))
                raise RuntimeError(
                    f"adapter {adapter_id} cannot satisfy {shell_key}: "
                    + ", ".join(missing)
                )
            binding = registry.upsert_binding(
                conn,
                shell_id=shell.id,
                executor_id=executor.id,
                priority=priority,
                constraints={"auto_spawn": True},
                responsibility="candidate",
                assignment_note="operator-installed external adapter",
                assigned_by="register-external-adapter",
                binding_id=(
                    f"binding_{shell_key.replace('-', '_')}_"
                    f"{adapter_id.replace('-', '_')}"
                ),
            )
            binding_ids.append(binding.id)
        health = {
            "enabled": False,
            "health_gate_passed": False,
            "reason": "live health check skipped",
        }
        if live_health:
            health = registry.set_executor_operational_state(
                conn,
                executor.id,
                enabled=True,
                reason="external adapter registration health gate",
                changed_by="register-external-adapter",
            )
        return {
            "executor_id": executor.id,
            "bindings": binding_ids,
            "health": health,
            "primary_changed": False,
        }
    finally:
        conn.close()


def setup_public_edition(
    *,
    home: Path,
    repo_root: Path,
    install_opencode_binary: bool = True,
    install_timeline: bool = True,
    live_health: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    home = home.expanduser().resolve()
    repo_root = repo_root.expanduser().resolve()
    home_token = set_hermes_home_override(home)
    try:
        opencode = ensure_opencode(install=install_opencode_binary, dry_run=dry_run)
        timeline_install = install_timeline_extension(
            repo_root=repo_root, install=install_timeline, dry_run=dry_run
        )
        catalog = configure_timeline_catalog(home=home, dry_run=dry_run)
        neural_plugin = install_neural_link_plugin(
            home=home, repo_root=repo_root, dry_run=dry_run
        )
        result: dict[str, Any] = {
            "schema": PUBLIC_SCHEMA,
            "dry_run": dry_run,
            "home": str(home),
            "repo_root": str(repo_root),
            "opencode": opencode,
            "timeline_extension": timeline_install,
            "timeline_catalog": catalog,
            "neural_link_plugin": neural_plugin,
        }
        if dry_run:
            result["bootstrap"] = bootstrap_supervisor(
                home=home,
                repo_root=repo_root,
                dry_run=True,
                mcp_catalog_overrides={TIMELINE_MCP: catalog["server"]},
            )
            return result
        opencode_path = str(opencode.get("path") or "")
        if not opencode_path:
            raise RuntimeError("OpenCode is required for the public-edition default")
        result["bootstrap"] = bootstrap_supervisor(home=home, repo_root=repo_root)
        result["opencode_adapter"] = install_opencode_adapter(
            home=home,
            repo_root=repo_root,
            opencode_path=opencode_path,
            live_health=live_health,
        )
        result["opencode_controller"] = enable_opencode_controller(
            home=home, live_health=live_health
        )
        return result
    finally:
        reset_hermes_home_override(home_token)
