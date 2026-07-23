"""Idempotent live bootstrap for the lightweight Hermes supervisor.

This module is intentionally deterministic: it creates no model sessions and
performs no domain work. It partitions the root MCP catalog into isolated
executor profiles, installs immutable role-shell contracts and many-to-many
bindings, restricts the root to control-plane tools, and installs a no-agent
heartbeat cron while preserving all existing jobs.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from hermes_cli import kanban_db as kb
from hermes_cli import supervisor_registry as registry
from hermes_cli.opencode_free_router import FREE_MODEL_PRIORITY
from hermes_cli.openrouter_free_router import OPENROUTER_FREE_MODEL_PRIORITY
from utils import atomic_json_write, atomic_replace, atomic_yaml_write


TIMELINE_MCP = "hermes-timeline-code-map"
HEARTBEAT_JOB_NAME = "hermes-supervisor-heartbeat"
HEARTBEAT_SCHEDULE = "0 * * * *"
HEARTBEAT_DELIVERY = "local"
DEFAULT_REPAIR_EXECUTOR_ID = "executor_hermes_worker_universal"
DEFAULT_HERMES_REPAIR_EXECUTOR_ID = "executor_hermes_worker_hermes_maintainer"
ROOT_TOOLSETS = ["supervisor", "kanban", "cronjob", "no_mcp"]
OPENROUTER_GEMMA_MODEL = "google/gemma-4-31b-it"
LOCAL_VLLM_GEMMA_MODEL = "google/gemma-4-26b-a4b-it"
GROK_MODEL = "grok-4"
OPENCODE_FREE_CONTROLLER_MODELS = tuple(
    model.removeprefix("opencode/") for model in FREE_MODEL_PRIORITY
)
ORDINARY_ROLE_KEYS = (
    "code",
    "market",
    "browser-research",
    "operations",
    "report",
    "verification",
    "tool-management",
)
DEFAULT_ARTIFACT_HEALTH: dict[str, Any] = {
    "enabled": False,
    "checks": [],
}


PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "hermes-worker-general": {
        "description": "General code, operations, reporting, and verification executor.",
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "kanban", "cronjob", TIMELINE_MCP,
        ],
        "capacity": 3,
    },
    "hermes-worker-market": {
        "description": (
            "Public-source market and finance research executor isolated from "
            "the Hermes root. Optional private data tools are added per role."
        ),
        "openai_runtime": "auto",
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "web", "browser", "kanban", TIMELINE_MCP,
        ],
        "capacity": 2,
    },
    "hermes-worker-browser": {
        "description": "Browser/research executor isolated from the Hermes root.",
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "web", "browser", "kanban", TIMELINE_MCP,
        ],
        "capacity": 2,
    },
    "hermes-worker-universal": {
        "description": (
            "Provider-neutral universal fallback profile. Product-specific command "
            "adapters such as OpenCode, Codex, or Grok inherit the same Role Shell."
        ),
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "web", "browser", "skills", "kanban", "cronjob",
            TIMELINE_MCP,
        ],
        "capacity": 4,
    },
    "hermes-worker-multitool": {
        "description": (
            "Tool specialist for role-scoped MCP, skill, plugin, and toolset "
            "inventory, compatibility checks, installation, and validation."
        ),
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "web", "skills", "kanban", TIMELINE_MCP,
        ],
        "capacity": 1,
    },
    "hermes-worker-hermes-maintainer": {
        "description": (
            "Hermes self-maintenance executor for controller, adapter, role-shell, "
            "routing, supervisor configuration, and Hermes runtime contract repair."
        ),
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "openai_runtime": "codex_app_server",
        "api_mode": "codex_app_server",
        "reasoning_effort": "high",
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "kanban", TIMELINE_MCP,
        ],
        "capacity": 1,
    },
    "hermes-worker-opencode-free": {
        "description": (
            "Dynamic strict-free OpenCode worker. It discovers the live catalog "
            "before every task and routes through the ordinary Role Shell."
        ),
        "provider": "opencode-zen",
        "model": OPENCODE_FREE_CONTROLLER_MODELS[0],
        "base_url": "https://opencode.ai/zen/v1",
        "api_mode": "chat_completions",
        "reasoning_effort": "medium",
        "fallback_model": [
            {"provider": "opencode-zen", "model": model}
            for model in OPENCODE_FREE_CONTROLLER_MODELS[1:]
        ],
        "worker_router": "opencode",
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "web", "browser", "skills", "kanban", "cronjob",
            TIMELINE_MCP,
        ],
        "capacity": 2,
    },
    "hermes-worker-openrouter-free": {
        "description": (
            "Dynamic strict-free OpenRouter worker. It ranks the live zero-price "
            "tool-capable catalog and sends an ordered same-request fallback list."
        ),
        "provider": "openrouter",
        "model": OPENROUTER_FREE_MODEL_PRIORITY[0],
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
        "reasoning_effort": "medium",
        "worker_router": "openrouter",
        "mcp_servers": [TIMELINE_MCP],
        "capabilities": [
            "file", "terminal", "web", "browser", "skills", "kanban", "cronjob",
            TIMELINE_MCP,
        ],
        "capacity": 2,
    },
}


ROLE_SPECS: dict[str, dict[str, Any]] = {
    "code": {
        "name": "Code Change",
        "description": "Scoped source changes and tests; no deployment authority unless stated.",
        "required": ["file", "terminal", "kanban", TIMELINE_MCP],
        "allowed": ["file", "terminal", "kanban", TIMELINE_MCP],
        "code_slice_required": True,
        "instructions": (
            "Implement only the card's repository scope. Read repo instructions and "
            "the target files first, use Timeline context and a stored code slice, "
            "run proportional tests, and return exact changed files and test evidence."
        ),
    },
    "market": {
        "name": "Market Research",
        "description": "Public-source finance research; no trade or account writes.",
        "required": ["kanban", TIMELINE_MCP, "web"],
        "allowed": PROFILE_SPECS["hermes-worker-market"]["capabilities"],
        "code_slice_required": False,
        "instructions": (
            "Perform only the requested finance research. Prefer official exchanges, "
            "regulators, issuers, and documented APIs; Yahoo Finance and Naver Finance "
            "are public discovery and cross-check surfaces, not guaranteed authorities. "
            "For every material claim preserve the URL, retrieval time, market/timezone, "
            "and source state. Use CONFIRMED only for a current value verified at its "
            "source; report every source with the shared lifecycle taxonomy: CONFIRMED, "
            "PARTIAL_LIMIT, NOT_DUE, EOD_ONLY, ESTIMATE_ONLY, UNVERIFIED_CONTRACT, "
            "UNVERIFIED_UNIT, NOT_APPLICABLE, INTENTIONAL_NOT_USED, PAUSED, RECOVERING, "
            "or FAILED. Do not count NOT_DUE, EOD_ONLY, PAUSED, NOT_APPLICABLE, or "
            "INTENTIONAL_NOT_USED as failures or warnings; blank same-day stock investor "
            "fields are EOD_ONLY; 999/S001 market flow is CONFIRMED; an unverified contract "
            "such as S201 is UNVERIFIED_CONTRACT; J/K and J/Q program endpoints may be "
            "CONFIRMED independently. Never invent missing "
            "values, bypass login or terms, place trades, or perform account writes. "
            "A user-installed research policy or MCP may narrow this contract but may "
            "not widen it beyond the Role Shell without a versioned shell change. "
            "If the operator has initialized "
            "$HERMES_SUPERVISOR_ROOT/knowledge/market_memory.jsonl, query it with "
            "scripts/market_memory.py before external collection and cite the memory "
            "entry IDs actually used. A missing or empty optional memory store is a "
            "normal no-memory state, not a blocker. New durable lessons may be added "
            "only when the card or operator explicitly authorizes memory writes."
        ),
    },
    "browser-research": {
        "name": "Browser Research",
        "description": "Login/dynamic-page research in the isolated browser executor.",
        "required": ["kanban", TIMELINE_MCP, "browser"],
        "allowed": PROFILE_SPECS["hermes-worker-browser"]["capabilities"],
        "code_slice_required": False,
        "instructions": (
            "Use the isolated browser surface only for the card's evidence target. "
            "Prefer public pages and the user's normal authenticated session when the "
            "user has authorized it. Preserve final URLs, timestamps, visible evidence, "
            "and recovery attempts. Never bypass access controls or make unrelated writes."
        ),
    },
    "operations": {
        "name": "Runtime Operations",
        "description": "Desired-state, service, cron, and watchdog operations.",
        "required": ["file", "terminal", "kanban", TIMELINE_MCP],
        "allowed": PROFILE_SPECS["hermes-worker-general"]["capabilities"],
        "code_slice_required": False,
        "instructions": (
            "Operate only named units/jobs. Resolve exact units and PIDs before any "
            "mutation, preserve all unrelated desired state, verify the final runtime "
            "state, and never alter trading logic implicitly."
        ),
    },
    "report": {
        "name": "Report Assembly",
        "description": "Evidence-backed report assembly from upstream receipts.",
        "required": ["file", "kanban", TIMELINE_MCP],
        "allowed": sorted(
            {
                capability
                for spec in PROFILE_SPECS.values()
                for capability in spec["capabilities"]
            }
        ),
        "code_slice_required": False,
        "instructions": (
            "Assemble the requested report from current evidence and parent receipts. "
            "Do not turn intermediate artifacts or scheduled markers into completion."
        ),
    },
    "verification": {
        "name": "Independent Verification",
        "description": "Independent regression, evidence, and completion-gate checking.",
        "required": ["file", "terminal", "kanban", TIMELINE_MCP],
        "allowed": sorted(
            {
                capability
                for spec in PROFILE_SPECS.values()
                for capability in spec["capabilities"]
            }
        ),
        "code_slice_required": False,
        "instructions": (
            "Verify the card independently against its real completion gate. Record "
            "regressions, baseline-existing failures, runtime drift, and exact evidence."
        ),
    },
    "tool-management": {
        "name": "Multitool Management",
        "description": (
            "Role-scoped MCP, skill, plugin, and toolset lifecycle management."
        ),
        "required": ["file", "terminal", "skills", "kanban", TIMELINE_MCP],
        "allowed": PROFILE_SPECS["hermes-worker-multitool"]["capabilities"],
        "code_slice_required": False,
        "instructions": (
            "Manage tool inventory, provenance, compatibility, installation, "
            "assignment, health probes, and rollback only within the card scope. "
            "Use HERMES_SUPERVISOR_ROOT as the central Hermes root; named worker "
            "profiles live below its profiles directory. "
            "Never install every MCP or skill into every profile: assign the "
            "minimum required set to the owning role and preserve profile "
            "isolation. For an approved target profile, cross-profile writes are "
            "allowed only to the exact backed-up tool/skill/MCP config named by "
            "the card. Inspect local registries and existing skills first; use "
            "public web discovery only when the local catalog is insufficient. "
            "Delegate login or dynamic-page discovery to browser-research. "
            "Delegate source-code changes, service restarts, secret expansion, "
            "and high-risk repair to the code or operations repair path. Toolset "
            "changes take effect only in a new worker session; never hot-mutate a "
            "running model context. Before mutation, back up the exact profile "
            "config. Return before/after assignments, provenance, probe results, "
            "rollback evidence, and any capability_missing handoff in the receipt."
        ),
    },
    "hermes-repair": {
        "name": "Hermes Maintainer",
        "description": (
            "Hermes self-maintenance only; ordinary project coding and repair stay "
            "on the code or operations shells."
        ),
        "required": ["file", "terminal", "kanban", TIMELINE_MCP],
        "allowed": PROFILE_SPECS["hermes-worker-hermes-maintainer"]["capabilities"],
        "code_slice_required": True,
        "instructions": (
            "Maintain Hermes itself only: its controller, adapters, role shells, "
            "routing, supervisor configuration, and Hermes runtime contracts. "
            "Never take ordinary product code creation or generic project bug repair; "
            "those remain code/operations work and may use lower-cost executors. Read "
            "the exact source_task_ids, runs, comments, receipts, and Timeline evidence "
            "from other adapters, distinguish claims from reproduced facts, and record "
            "a before-change impact report. Modify only the named Hermes repository or "
            "runtime configuration, run risk-proportional tests, and record the actual "
            "after-change impact. Commit and push only the task's non-default branch; "
            "never push main, master, or another default branch and never merge or "
            "promote a release without a separate explicit operator action."
        ),
        "runtime_requirements": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "api_mode": "codex_app_server",
            "reasoning_effort": "high",
        },
        "replacement_gate": {
            "baseline_executor_id": DEFAULT_HERMES_REPAIR_EXECUTOR_ID,
            "baseline_runtime": {
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "api_mode": "codex_app_server",
                "reasoning_effort": "high",
            },
            "minimum_cases": 20,
            "minimum_overall_pass_rate": 0.95,
            "critical_pass_rate": 1.0,
            "benchmark_suite": "hermes-repair-v1",
            "minimum_baseline_score_ratio": 0.95,
            "maximum_median_latency_ratio": 1.5,
            "require_benchmark_artifact": True,
            "maximum_default_branch_writes": 0,
            "require_operator_approval": True,
            "require_conflicting_adapter_result_case": True,
            "require_config_and_source_cases": True,
            "require_branch_push_and_rollback": True,
            "require_timeline_invalid_count": 0,
        },
    },
}


BINDING_SPECS = [
    ("code", "hermes-worker-general", 100),
    ("operations", "hermes-worker-general", 100),
    ("report", "hermes-worker-general", 40),
    ("verification", "hermes-worker-general", 60),
    ("market", "hermes-worker-market", 100),
    ("report", "hermes-worker-market", 50),
    ("verification", "hermes-worker-market", 40),
    ("browser-research", "hermes-worker-browser", 100),
    ("report", "hermes-worker-browser", 30),
    ("verification", "hermes-worker-browser", 30),
    ("code", "hermes-worker-universal", 5),
    ("market", "hermes-worker-universal", 5),
    ("browser-research", "hermes-worker-universal", 5),
    ("operations", "hermes-worker-universal", 5),
    ("report", "hermes-worker-universal", 5),
    ("verification", "hermes-worker-universal", 5),
    ("tool-management", "hermes-worker-multitool", 100),
    ("tool-management", "hermes-worker-universal", 5),
    ("hermes-repair", "hermes-worker-hermes-maintainer", 100),
    *[
        (shell_key, "hermes-worker-openrouter-free", 4)
        for shell_key in ORDINARY_ROLE_KEYS
    ],
    *[
        (shell_key, "hermes-worker-opencode-free", 3)
        for shell_key in ORDINARY_ROLE_KEYS
    ],
]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"expected mapping in {path}")
    return data


def _root_config(current: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(current)
    current_supervisor = config.get("supervisor") or {}
    current_artifact_health = current_supervisor.get("artifact_health") or {}
    artifact_health = copy.deepcopy(DEFAULT_ARTIFACT_HEALTH)
    for key, value in current_artifact_health.items():
        artifact_health[key] = copy.deepcopy(value)
    platforms = sorted(
        set((config.get("platform_toolsets") or {}).keys())
        | {"cli", "telegram", "discord", "slack"}
    )
    config["mcp_servers"] = {}
    config["toolsets"] = ["supervisor", "kanban", "cronjob"]
    config["platform_toolsets"] = {
        platform: list(ROOT_TOOLSETS) for platform in platforms
    }
    kanban = dict(config.get("kanban") or {})
    kanban.update(
        {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 15,
            "auto_decompose": False,
        }
    )
    config["kanban"] = kanban
    config["supervisor"] = {
        "enabled": True,
        "schema_version": 1,
        "repair_executor_id": DEFAULT_REPAIR_EXECUTOR_ID,
        "hermes_repair_executor_id": DEFAULT_HERMES_REPAIR_EXECUTOR_ID,
        "platforms": platforms,
        "expected_paused_cron": [],
        "expected_services": [],
        "required_cron": [HEARTBEAT_JOB_NAME],
        "heartbeat_schedule": HEARTBEAT_SCHEDULE,
        "timezone": "Asia/Seoul",
        "artifact_health": artifact_health,
    }
    timeline_db = str(current_supervisor.get("timeline_db") or "").strip()
    if timeline_db:
        config["supervisor"]["timeline_db"] = timeline_db
    return config


def _profile_config(
    current: dict[str, Any],
    *,
    spec: dict[str, Any],
    mcp_catalog: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    config = copy.deepcopy(current)
    source_mcp = mcp_catalog if mcp_catalog is not None else current.get("mcp_servers") or {}
    missing = [name for name in spec["mcp_servers"] if name not in source_mcp]
    if missing:
        raise RuntimeError(
            "executor profile MCP source missing from root config: "
            + ", ".join(sorted(missing))
        )
    config["mcp_servers"] = {
        name: copy.deepcopy(source_mcp[name]) for name in spec["mcp_servers"]
    }
    # Bound one-shot workers must see their MCP aliases on the first and only
    # turn. Give local/proxy discovery a bounded but practical startup window.
    config["mcp_discovery_timeout"] = 15.0
    if any(
        spec.get(name)
        for name in (
            "provider", "model", "base_url", "openai_runtime", "api_mode",
        )
    ):
        model_config = dict(config.get("model") or {})
        if spec.get("provider"):
            model_config["provider"] = str(spec["provider"])
        if spec.get("model"):
            model_config["default"] = str(spec["model"])
        if spec.get("base_url"):
            model_config["base_url"] = str(spec["base_url"])
        if spec.get("openai_runtime"):
            model_config["openai_runtime"] = str(spec["openai_runtime"])
        if spec.get("api_mode"):
            model_config["api_mode"] = str(spec["api_mode"])
        config["model"] = model_config
    if spec.get("fallback_model"):
        config["fallback_model"] = copy.deepcopy(spec["fallback_model"])
    if spec.get("reasoning_effort"):
        agent_config = dict(config.get("agent") or {})
        agent_config["reasoning_effort"] = str(spec["reasoning_effort"])
        config["agent"] = agent_config
    config["toolsets"] = list(spec["capabilities"])
    config["platform_toolsets"] = {"cli": list(spec["capabilities"])}
    kanban = dict(config.get("kanban") or {})
    kanban.update({"dispatch_in_gateway": False, "auto_decompose": False})
    config["kanban"] = kanban
    config["supervisor"] = {
        "enabled": False,
        "executor_profile": True,
        "requires_timeline_receipt": True,
    }
    return config


def build_config_plan(
    current: dict[str, Any],
    *,
    mcp_catalog: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a serializable zero-MCP root and isolated-profile plan."""
    return {
        "root": _root_config(current),
        "profiles": {
            name: _profile_config(current, spec=spec, mcp_catalog=mcp_catalog)
            for name, spec in PROFILE_SPECS.items()
        },
    }


def _collect_mcp_catalog(home: Path, current: dict[str, Any]) -> dict[str, Any]:
    """Recover the executor catalog without putting MCP back on the root."""
    catalog = copy.deepcopy(current.get("mcp_servers") or {})
    for name in PROFILE_SPECS:
        profile_config = _load_yaml(home / "profiles" / name / "config.yaml")
        for server_name, server_config in (
            profile_config.get("mcp_servers") or {}
        ).items():
            catalog.setdefault(server_name, copy.deepcopy(server_config))
    return catalog


def _ensure_profile_dir(home: Path, name: str) -> Path:
    profile = home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    for relative in ("cron", "logs", "memory", "sessions", "skills"):
        (profile / relative).mkdir(parents=True, exist_ok=True)
    env_path = profile / ".env"
    if not env_path.exists():
        env_path.write_text(
            "# Lightweight Hermes executor profile.\n"
            "# Provider auth may fall back to the shared Hermes auth store.\n",
            encoding="utf-8",
        )
        os.chmod(env_path, 0o600)
    marker = profile / ".no-bundled-skills"
    if not marker.exists():
        marker.write_text(
            "Executor profiles are capability-scoped by the supervisor.\n",
            encoding="utf-8",
        )
    soul = profile / "SOUL.md"
    if not soul.exists():
        soul.write_text(
            "# Hermes Executor\n\n"
            "Follow the immutable role-shell contract and return a validated receipt.\n",
            encoding="utf-8",
        )
    return profile


def _backup_runtime(home: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%Z")
    base = home / "backups" / f"supervisor-install-{stamp}"
    target = base
    suffix = 1
    while target.exists():
        target = Path(f"{base}-{suffix}")
        suffix += 1
    target.mkdir(parents=True, exist_ok=False)
    for relative in ("config.yaml", "kanban.db", "cron/jobs.json"):
        source = home / relative
        if source.exists():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return target


def _install_heartbeat_script(home: Path, repo_root: Path) -> Path:
    source = repo_root / "scripts" / "hermes_supervisor_heartbeat.py"
    if not source.is_file():
        raise RuntimeError(f"heartbeat source missing: {source}")
    target = home / "scripts" / "hermes_supervisor_heartbeat.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".supervisor-heartbeat-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    os.chmod(target, 0o700)
    return target


def _rebind_existing_bindings_to_active_shells(
    conn,
    shells: dict[str, registry.RoleShell],
) -> list[str]:
    """Move every adapter binding to the active version of its role shell.

    Role-shell contracts are adapter-independent.  Bootstrap-managed profile
    bindings are refreshed from ``BINDING_SPECS`` below, but operator-added
    command adapters (Grok, OpenCode, Codex, local models, and future adapters)
    must inherit the same active contract without needing adapter-specific
    bootstrap code.  Preserve the stable binding id and all operator-owned
    routing metadata while moving only the shell version.
    """
    binding_ids = [
        str(row["id"])
        for row in conn.execute(
            "SELECT b.id FROM role_bindings b "
            "JOIN role_shells s ON s.id=b.shell_id "
            "ORDER BY s.shell_key,b.created_at,b.id"
        ).fetchall()
    ]
    rebound: list[str] = []
    for binding_id in binding_ids:
        binding = registry.get_binding(conn, binding_id)
        if binding is None:
            continue
        old_shell = registry.get_shell(conn, shell_id=binding.shell_id)
        if old_shell is None or old_shell.shell_key not in shells:
            continue
        active_shell = shells[old_shell.shell_key]
        if binding.shell_id == active_shell.id:
            continue
        conflict = conn.execute(
            "SELECT id FROM role_bindings WHERE shell_id=? AND executor_id=?",
            (active_shell.id, binding.executor_id),
        ).fetchone()
        if conflict is not None and str(conflict["id"]) != binding.id:
            raise RuntimeError(
                "cannot preserve stable adapter binding while upgrading role shell: "
                f"{binding.id} conflicts with {conflict['id']} for "
                f"{active_shell.id}/{binding.executor_id}"
            )
        updated = registry.upsert_binding(
            conn,
            shell_id=active_shell.id,
            executor_id=binding.executor_id,
            priority=binding.priority,
            weight=binding.weight,
            capability_cap=binding.capability_cap,
            constraints=binding.constraints,
            responsibility=binding.responsibility,
            assignment_note=binding.assignment_note,
            assigned_by=binding.assigned_by,
            binding_id=binding.id,
        )
        if not binding.enabled:
            registry.set_binding_enabled(conn, updated.id, False)
        rebound.append(updated.id)
    return rebound


def _free_worker_launch_config(
    *,
    home: Path,
    repo_root: Path,
    profile_name: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Build a trusted bridge around a dynamic strict-free Hermes worker."""
    router = str(spec["worker_router"])
    profile_home = home / "profiles" / profile_name
    engine_argv = [
        sys.executable,
        "-m",
        "hermes_cli.free_worker_router",
        "--provider",
        router,
        "--prompt-file",
        "{prompt_file}",
        "--workspace",
        "{workspace}",
        "--profile-home",
        str(profile_home),
    ]
    health_argv = [
        sys.executable,
        "-m",
        "hermes_cli.free_worker_router",
        "--provider",
        router,
        "--health-check",
        "--workspace",
        str(repo_root),
        "--profile-home",
        str(profile_home),
    ]
    if router == "opencode":
        opencode_config = home / "supervisor" / "mcp" / "opencode.json"
        state_file = home / "supervisor" / "opencode_free_router_state.json"
        opencode_path = shutil.which("opencode") or "opencode"
        for argv in (engine_argv, health_argv):
            argv.extend(
                [
                    "--opencode-config",
                    str(opencode_config),
                    "--state-file",
                    str(state_file),
                    "--opencode",
                    opencode_path,
                ]
            )
    bridge_argv = [
        sys.executable,
        "-m",
        "hermes_cli.external_cli_adapter",
        "--prompt-file",
        "{prompt_file}",
        "--engine-name",
        f"{router}-free-hermes-worker",
        "--engine-argv-json",
        json.dumps(engine_argv),
        "--timeline-client",
        str(repo_root / "scripts" / "hermes_timeline_cli.py"),
        "--timeline-python",
        sys.executable,
        "--timeline-db",
        str(home / "timeline_code_map" / "graph.db"),
    ]
    return {
        "argv": bridge_argv,
        "profile": profile_name,
        "provider": spec["provider"],
        "model": spec["model"],
        "api_mode": spec["api_mode"],
        "reasoning_effort": spec["reasoning_effort"],
        "capability_enforcement": "env",
        "health_failure_confirmation_attempts": 2,
        "tool_contract": {
            "schema_version": 1,
            "transport": "adapter_brokered",
            "adapter_capabilities": list(spec["capabilities"]),
            "native_capabilities": list(spec["capabilities"]),
            "required_mcp_servers": [],
            "probe": {
                "argv": health_argv,
                "required_output": ["READY"],
                "timeout_seconds": 120,
            },
        },
    }


def _install_registry(
    *, home: Path, repo_root: Path, controller_config: dict[str, Any]
) -> dict[str, Any]:
    shells: dict[str, registry.RoleShell] = {}
    executors: dict[str, registry.Executor] = {}
    bindings: list[registry.Binding] = []
    with kb.connect_closing(home / "kanban.db") as conn:
        registry.ensure_schema(conn)
        model_config = controller_config.get("model") or {}
        if not isinstance(model_config, dict):
            model_config = {}
        agent_config = controller_config.get("agent") or {}
        if not isinstance(agent_config, dict):
            agent_config = {}
        existing_opencode = registry.get_controller_adapter(
            conn, "controller_opencode_free"
        )
        opencode_model = (
            existing_opencode.model
            if existing_opencode is not None
            and existing_opencode.provider == "opencode-zen"
            and existing_opencode.model in OPENCODE_FREE_CONTROLLER_MODELS
            else OPENCODE_FREE_CONTROLLER_MODELS[0]
        )
        opencode_free = registry.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_opencode_free",
            name="OpenCode free controller",
            provider="opencode-zen",
            model=opencode_model,
            base_url="https://opencode.ai/zen/v1",
            api_mode="chat_completions",
            reasoning_effort="medium",
            health_url="https://opencode.ai/zen/v1/models",
            description=(
                "Default public-edition controller candidate. It is enabled only "
                "after live free-catalog and tool-use health checks pass."
            ),
            metadata={
                "source": "opencode_zen",
                "default_candidate": True,
                "delegation_only": True,
                "require_model_in_catalog": True,
                "require_tool_smoke": True,
                "tool_smoke_choice": "auto",
                "tool_smoke_max_tokens": 128,
                "anonymous_api": True,
                "dynamic_free_model_fallback": True,
                "free_model_suffix": "-free",
                "free_model_ids": ["big-pickle"],
                "model_fallback_candidates": list(OPENCODE_FREE_CONTROLLER_MODELS),
                "health_ttl_seconds": 3600,
            },
            initial_enabled=False,
        )
        codex = registry.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_codex",
            name="Codex controller",
            provider="openai-codex",
            model=str(model_config.get("default") or "gpt-5.6-sol"),
            base_url=str(
                model_config.get("base_url")
                or "https://chatgpt.com/backend-api/codex"
            ),
            api_mode=str(
                model_config.get("api_mode")
                or (
                    "codex_app_server"
                    if model_config.get("openai_runtime") == "codex_app_server"
                    else "codex_responses"
                )
            ),
            reasoning_effort=str(
                agent_config.get("reasoning_effort") or "medium"
            ),
            fallback_adapter_id=opencode_free.id,
            description="Optional Codex controller; enabled only after authentication and probe.",
            metadata={"source": "controller_config", "delegation_only": True},
            initial_enabled=False,
        )
        openrouter = registry.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_openrouter_gemma4",
            name="OpenRouter Gemma 4 31B controller",
            provider="openrouter",
            model=OPENROUTER_GEMMA_MODEL,
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            reasoning_effort="medium",
            key_env="OPENROUTER_API_KEY",
            health_url="https://openrouter.ai/api/v1/models",
            fallback_adapter_id=opencode_free.id,
            description=(
                "Direct paid OpenRouter controller candidate; independent from "
                "OpenCode."
            ),
            metadata={
                "source": "direct_openrouter",
                "delegation_only": True,
                "require_model_in_catalog": True,
                "require_tool_smoke": True,
            },
            initial_enabled=False,
        )
        existing_openrouter_free = registry.get_controller_adapter(
            conn, "controller_openrouter_free"
        )
        openrouter_free_model = (
            existing_openrouter_free.model
            if existing_openrouter_free is not None
            and existing_openrouter_free.provider == "openrouter"
            and existing_openrouter_free.model in OPENROUTER_FREE_MODEL_PRIORITY
            else OPENROUTER_FREE_MODEL_PRIORITY[0]
        )
        openrouter_free = registry.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_openrouter_free",
            name="OpenRouter strongest-free controller",
            provider="openrouter",
            model=openrouter_free_model,
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            reasoning_effort="medium",
            key_env="OPENROUTER_API_KEY",
            health_url="https://openrouter.ai/api/v1/models",
            fallback_adapter_id=opencode_free.id,
            description=(
                "Strict zero-price, tool-capable OpenRouter route. Hermes orders "
                "strong models first and OpenRouter performs same-request fallback."
            ),
            metadata={
                "source": "direct_openrouter_free",
                "delegation_only": True,
                "require_model_in_catalog": True,
                "require_tool_smoke": True,
                "tool_smoke_choice": "auto",
                "tool_smoke_max_tokens": 128,
                "dynamic_free_model_fallback": True,
                "openrouter_free_router": True,
                "server_side_model_fallback": True,
                "free_model_suffix": ":free",
                "model_fallback_candidates": list(
                    OPENROUTER_FREE_MODEL_PRIORITY
                ),
                "health_ttl_seconds": 21_600,
            },
            initial_enabled=False,
        )
        grok = registry.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_grok",
            name="Grok controller",
            provider="xai",
            model=GROK_MODEL,
            base_url="https://api.x.ai/v1",
            api_mode="chat_completions",
            reasoning_effort="medium",
            key_env="XAI_API_KEY",
            health_url="https://api.x.ai/v1/models",
            fallback_adapter_id=opencode_free.id,
            description="Optional Grok controller; no credentials are bundled.",
            metadata={
                "source": "xai",
                "delegation_only": True,
                "require_model_in_catalog": True,
                "require_tool_smoke": True,
            },
            initial_enabled=False,
        )
        local_vllm = registry.upsert_controller_adapter(
            conn,
            controller_adapter_id="controller_vllm_gemma4",
            name="Local vLLM Gemma 4 controller",
            provider="vllm",
            model=LOCAL_VLLM_GEMMA_MODEL,
            base_url="http://127.0.0.1:8000/v1",
            api_mode="chat_completions",
            reasoning_effort="medium",
            health_url="http://127.0.0.1:8000/v1/models",
            fallback_adapter_id=opencode_free.id,
            description="Future local Gemma 4 controller; disabled until vLLM is loaded.",
            metadata={
                "source": "local_vllm",
                "delegation_only": True,
                "require_model_in_catalog": True,
                "require_tool_smoke": True,
                "pending_runtime": True,
            },
            initial_enabled=False,
        )
        for key, spec in ROLE_SPECS.items():
            shells[key] = registry.ensure_shell_version(
                conn,
                shell_key=key,
                name=spec["name"],
                description=spec["description"],
                contract={
                    "allowed_adapters": ["hermes_profile", "command"],
                    "instructions": spec["instructions"],
                    "root_may_execute": False,
                    "executor_identity_does_not_widen_capabilities": True,
                    **(
                        {"runtime_requirements": spec["runtime_requirements"]}
                        if spec.get("runtime_requirements")
                        else {}
                    ),
                    **(
                        {"replacement_gate": spec["replacement_gate"]}
                        if spec.get("replacement_gate")
                        else {}
                    ),
                },
                required_capabilities=spec["required"],
                allowed_capabilities=spec["allowed"],
                evidence_policy={
                    "timeline_required": True,
                    "neural_recall_required": True,
                    "code_slice_required": bool(spec["code_slice_required"]),
                    "verify_all_invalid_count": 0,
                    "outputs_required": True,
                },
            )
        for profile_name, spec in PROFILE_SPECS.items():
            executor_id = f"executor_{profile_name.replace('-', '_')}"
            adapter_type = "hermes_profile"
            if spec.get("worker_router"):
                adapter_type = "command"
                launch_config = _free_worker_launch_config(
                    home=home,
                    repo_root=repo_root,
                    profile_name=profile_name,
                    spec=spec,
                )
            else:
                launch_config = {"profile": profile_name}
                for runtime_key in (
                    "provider", "model", "api_mode", "reasoning_effort"
                ):
                    if spec.get(runtime_key):
                        launch_config[runtime_key] = spec[runtime_key]
            executors[profile_name] = registry.upsert_executor(
                conn,
                executor_id=executor_id,
                name=profile_name,
                description=spec["description"],
                adapter_type=adapter_type,
                launch_config=launch_config,
                capabilities=spec["capabilities"],
                capacity=int(spec["capacity"]),
                heartbeat_required=False,
            )
        default_primary_priority = {
            shell_key: max(
                priority
                for candidate_shell, _profile, priority in BINDING_SPECS
                if candidate_shell == shell_key
            )
            for shell_key in ROLE_SPECS
        }
        for shell_key, profile_name, priority in BINDING_SPECS:
            shell = shells[shell_key]
            executor = executors[profile_name]
            existing_row = conn.execute(
                "SELECT * FROM role_bindings WHERE shell_id=? AND executor_id=?",
                (shell.id, executor.id),
            ).fetchone()
            existing = (
                registry.Binding.from_row(existing_row) if existing_row else None
            )
            bindings.append(
                registry.upsert_binding(
                    conn,
                    shell_id=shell.id,
                    executor_id=executor.id,
                    priority=(existing.priority if existing else priority),
                    weight=(existing.weight if existing else 1.0),
                    capability_cap=(existing.capability_cap if existing else ()),
                    constraints=(
                        existing.constraints if existing else {"auto_spawn": True}
                    ),
                    responsibility=(
                        existing.responsibility
                        if existing
                        else (
                            "primary"
                            if priority == default_primary_priority[shell_key]
                            else "candidate"
                        )
                    ),
                    assignment_note=(existing.assignment_note if existing else None),
                    assigned_by=(existing.assigned_by if existing else "bootstrap"),
                    binding_id=(
                        f"binding_{shell_key.replace('-', '_')}_"
                        f"{profile_name.replace('-', '_')}"
                    ),
                )
            )
        # Rebind every adapter, including operator-added command executors that
        # are not enumerated in BINDING_SPECS, to the active version of its role.
        # This is the adapter-independent role-contract inheritance gate.
        rebound_bindings = _rebind_existing_bindings_to_active_shells(conn, shells)

        # Existing databases gain the responsibility column as 'candidate'.
        # Promote exactly one deterministic default only when no operator-owned
        # primary already exists; never overwrite a prior adapter assignment.
        for shell_key, shell in shells.items():
            primary = conn.execute(
                "SELECT 1 FROM role_bindings WHERE shell_id=? "
                "AND responsibility='primary' LIMIT 1",
                (shell.id,),
            ).fetchone()
            if primary is None:
                row = conn.execute(
                    "SELECT id FROM role_bindings WHERE shell_id=? AND enabled=1 "
                    "ORDER BY priority DESC,weight DESC,created_at,id LIMIT 1",
                    (shell.id,),
                ).fetchone()
                if row is not None:
                    with registry.write_txn(conn):
                        conn.execute(
                            "UPDATE role_bindings SET responsibility='primary',"
                            "assigned_by=COALESCE(assigned_by,'bootstrap'),updated_at=? "
                            "WHERE id=?",
                            (registry._now(), row["id"]),
                        )
    return {
        "shells": {key: shell.id for key, shell in shells.items()},
        "executors": {
            key: executor.id for key, executor in executors.items()
        },
        "controller_adapters": [
            opencode_free.id,
            codex.id,
            openrouter.id,
            openrouter_free.id,
            grok.id,
            local_vllm.id,
        ],
        "bindings": [binding.id for binding in bindings],
        "rebound_bindings": rebound_bindings,
    }


def _install_heartbeat_cron(script: Path) -> dict[str, Any]:
    from cron.jobs import create_job, list_jobs, update_job

    matches = [
        job for job in list_jobs(include_disabled=True)
        if job.get("name") == HEARTBEAT_JOB_NAME
    ]
    if len(matches) > 1:
        raise RuntimeError(
            f"multiple cron jobs named {HEARTBEAT_JOB_NAME!r}; refusing to guess"
        )
    common_updates = {
        "schedule": HEARTBEAT_SCHEDULE,
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "prompt": "",
        "script": script.name,
        "no_agent": True,
        "deliver": HEARTBEAT_DELIVERY,
        "wrap_response": False,
        "workdir": str(script.parent.parent),
        "timezone_contract": {
            "intended_timezone": "Asia/Seoul",
            "intended_local_time": "hourly at minute 00 KST",
            "stored_cron": HEARTBEAT_SCHEDULE,
            "scheduler_execution_timezone": "Asia/Seoul",
            "reporting_timezone": "KST",
            "out_of_window_action": "scheduler_timezone_mismatch",
        },
    }
    if matches:
        job = update_job(matches[0]["id"], common_updates)
        if job is None:
            raise RuntimeError("failed to update supervisor heartbeat cron")
        return job
    job = create_job(
        prompt="",
        schedule=HEARTBEAT_SCHEDULE,
        name=HEARTBEAT_JOB_NAME,
        script=script.name,
        no_agent=True,
        deliver=HEARTBEAT_DELIVERY,
        workdir=str(script.parent.parent),
    )
    updated = update_job(job["id"], common_updates)
    if updated is None:
        raise RuntimeError("failed to stamp supervisor heartbeat cron contract")
    return updated


def bootstrap_supervisor(
    *,
    home: Path,
    repo_root: Path,
    dry_run: bool = False,
    mcp_catalog_overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    home = home.expanduser().resolve()
    repo_root = repo_root.expanduser().resolve()
    config_path = home / "config.yaml"
    current = _load_yaml(config_path)
    mcp_catalog = _collect_mcp_catalog(home, current)
    if mcp_catalog_overrides:
        # Dry-run installers may plan an MCP entry without persisting it.  Feed
        # that planned catalog into the same profile builder so preview and
        # apply validate identical Role Shell inputs.
        mcp_catalog.update(copy.deepcopy(mcp_catalog_overrides))
    plan = build_config_plan(current, mcp_catalog=mcp_catalog)
    summary: dict[str, Any] = {
        "schema": "hermes.supervisor.bootstrap.v1",
        "dry_run": dry_run,
        "home": str(home),
        "repo_root": str(repo_root),
        "root_mcp_before": sorted((current.get("mcp_servers") or {}).keys()),
        "executor_mcp_catalog": sorted(mcp_catalog),
        "root_mcp_after": [],
        "profiles": sorted(PROFILE_SPECS),
        "role_shells": sorted(ROLE_SPECS),
        "binding_count": len(BINDING_SPECS),
        "heartbeat_schedule": HEARTBEAT_SCHEDULE,
    }
    if dry_run:
        return summary
    if not config_path.exists():
        raise RuntimeError(f"Hermes root config not found: {config_path}")
    if str(os.environ.get("HERMES_ROLE_SHELL_ID") or "").strip():
        raise RuntimeError("a bound executor child cannot bootstrap the supervisor root")

    backup_dir = _backup_runtime(home)
    summary["backup_dir"] = str(backup_dir)
    for name, profile_config in plan["profiles"].items():
        profile_dir = _ensure_profile_dir(home, name)
        atomic_yaml_write(profile_dir / "config.yaml", profile_config, sort_keys=False)
    opencode_config = home / "supervisor" / "mcp" / "opencode.json"
    if not opencode_config.exists():
        atomic_json_write(
            opencode_config,
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    TIMELINE_MCP: {
                        "type": "local",
                        "command": [
                            sys.executable,
                            "-m",
                            "hermes_timeline_code_map.mcp_server",
                        ],
                        "environment": {
                            "TIMELINE_CODE_MAP_DB_PATH": str(
                                home / "timeline_code_map" / "graph.db"
                            )
                        },
                        "enabled": True,
                    }
                },
            },
        )
    heartbeat_script = _install_heartbeat_script(home, repo_root)
    atomic_yaml_write(config_path, plan["root"], sort_keys=False)
    registry_state = _install_registry(
        home=home, repo_root=repo_root, controller_config=plan["root"]
    )
    heartbeat_job = _install_heartbeat_cron(heartbeat_script)
    summary.update(
        {
            "registry": registry_state,
            "heartbeat_script": str(heartbeat_script),
            "heartbeat_job_id": heartbeat_job.get("id"),
            "heartbeat_job_state": heartbeat_job.get("state"),
        }
    )
    return summary


def format_bootstrap_summary(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2)
