"""CLI and deterministic heartbeat for the lightweight Hermes supervisor."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb
from hermes_cli import supervisor_registry as registry


SELF_HEARTBEAT_CRON_NAME = "hermes-supervisor-heartbeat"
HEARTBEAT_FAILURE_ACK_SCHEMA = "hermes.supervisor.failure-acknowledgements.v1"
HEARTBEAT_SNAPSHOT_SCHEMA = "hermes.supervisor.heartbeat.v2"


def _json_arg(value: str) -> Any:
    path = Path(value).expanduser()
    text = path.read_text(encoding="utf-8") if path.is_file() else value
    return json.loads(text)


def _shell_dict(shell: registry.RoleShell) -> dict[str, Any]:
    return {
        "id": shell.id,
        "shell_key": shell.shell_key,
        "version": shell.version,
        "supersedes_shell_id": shell.supersedes_shell_id,
        "name": shell.name,
        "description": shell.description,
        "contract_hash": shell.contract_hash,
        "contract": shell.contract,
        "required_capabilities": shell.required_capabilities,
        "allowed_capabilities": shell.allowed_capabilities,
        "evidence_policy": shell.evidence_policy,
        "created_at": shell.created_at,
    }


def _executor_dict(executor: registry.Executor) -> dict[str, Any]:
    return {
        "id": executor.id,
        "name": executor.name,
        "adapter_type": executor.adapter_type,
        "description": executor.description,
        "launch_config": executor.launch_config,
        "capabilities": executor.capabilities,
        "capacity": executor.capacity,
        "heartbeat_required": executor.heartbeat_required,
        "heartbeat_ttl_seconds": executor.heartbeat_ttl_seconds,
        "last_heartbeat_at": executor.last_heartbeat_at,
        "health_state": executor.health_state,
        "enabled": executor.enabled,
    }


def _binding_dict(binding: registry.Binding) -> dict[str, Any]:
    return {
        "id": binding.id,
        "shell_id": binding.shell_id,
        "executor_id": binding.executor_id,
        "priority": binding.priority,
        "weight": binding.weight,
        "capability_cap": binding.capability_cap,
        "constraints": binding.constraints,
        "responsibility": binding.responsibility,
        "assignment_note": binding.assignment_note,
        "assigned_by": binding.assigned_by,
        "enabled": binding.enabled,
        "last_selected_at": binding.last_selected_at,
    }


def _cron_last_status(job: dict[str, Any]) -> Any:
    """Read both the current flat cron status and the legacy nested shape."""
    current = job.get("last_status")
    if current is not None:
        return current
    legacy = job.get("last_run") or {}
    return legacy.get("status") if isinstance(legacy, dict) else None


def _cron_status_failed(status: Any) -> bool:
    """Return whether a persisted cron result is an explicit failure."""
    value = str(status or "").strip().lower()
    if not value:
        return False
    failure_states = {
        "error",
        "failed",
        "failure",
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
    }
    return value in failure_states or any(
        value.startswith(f"{prefix}:") for prefix in failure_states
    )


def _heartbeat_failure_ack_path() -> Path:
    return get_hermes_home() / "supervisor" / "failure_acknowledgements.json"


def heartbeat_snapshot_path() -> Path:
    """Return the hourly heartbeat's single persisted status snapshot."""
    return get_hermes_home() / "supervisor" / "heartbeat.json"


def save_heartbeat_snapshot(
    snapshot: dict[str, Any], path: Path | None = None
) -> Path:
    """Atomically persist a heartbeat snapshot for lightweight status reads."""
    target = path or heartbeat_snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".heartbeat-", suffix=".json", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return target


def load_heartbeat_snapshot(
    path: Path | None = None,
) -> tuple[dict[str, Any], float]:
    """Load the persisted heartbeat and return it with its filesystem mtime."""
    target = path or heartbeat_snapshot_path()
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != HEARTBEAT_SNAPSHOT_SCHEMA:
        raise RuntimeError(f"invalid heartbeat snapshot: {target}")
    if not isinstance(payload.get("lanes"), dict):
        raise RuntimeError(f"heartbeat snapshot has no lanes: {target}")
    return payload, target.stat().st_mtime


def _cron_failure_fingerprint(row: dict[str, Any]) -> dict[str, str]:
    """Identify one persisted failed run without masking a future failure."""
    return {
        "last_run_at": str(row.get("last_run_at") or ""),
        "last_status": str(_cron_last_status(row) or ""),
    }


def _load_failure_acknowledgements(
    path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    target = path or _heartbeat_failure_ack_path()
    if not target.exists():
        return {}
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != HEARTBEAT_FAILURE_ACK_SCHEMA:
        raise RuntimeError(f"invalid heartbeat failure acknowledgement file: {target}")
    rows = payload.get("acknowledgements") or {}
    if not isinstance(rows, dict):
        raise RuntimeError(f"invalid heartbeat failure acknowledgements: {target}")
    return {
        str(name): value
        for name, value in rows.items()
        if str(name) and isinstance(value, dict)
    }


def _save_failure_acknowledgements(
    rows: dict[str, dict[str, Any]],
    path: Path | None = None,
) -> Path:
    target = path or _heartbeat_failure_ack_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": HEARTBEAT_FAILURE_ACK_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "acknowledgements": rows,
    }
    fd, tmp_name = tempfile.mkstemp(
        prefix=".failure-acknowledgements-", suffix=".json", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return target


def _active_failed_cron_rows(cron_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in cron_rows
        if row.get("enabled", True)
        and row.get("state") != "paused"
        and str(row.get("name") or row.get("id")) != SELF_HEARTBEAT_CRON_NAME
        and (
            _cron_status_failed(_cron_last_status(row))
            or _cron_status_failed(row.get("state"))
        )
    ]


def _ack_matches_failure(ack: dict[str, Any], row: dict[str, Any]) -> bool:
    if not ack:
        return False
    fingerprint = _cron_failure_fingerprint(row)
    return all(str(ack.get(key) or "") == value for key, value in fingerprint.items())


def _cron_inventory_summary(
    cron_rows: list[dict[str, Any]],
    acknowledgements: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize every configured job without confusing required with active."""
    active = [
        row
        for row in cron_rows
        if row.get("enabled", True) and row.get("state") != "paused"
    ]
    paused = [
        row
        for row in cron_rows
        if row.get("state") == "paused" or not row.get("enabled", True)
    ]
    # Acknowledgement suppresses only the exact failed run fingerprint. The
    # same job automatically becomes actionable again after its next failure.
    acknowledgements = acknowledgements or {}
    observed_failed_rows = _active_failed_cron_rows(cron_rows)
    acknowledged_failed = []
    failed_active = []
    for row in observed_failed_rows:
        name = str(row.get("name") or row.get("id"))
        if _ack_matches_failure(acknowledgements.get(name) or {}, row):
            acknowledged_failed.append(name)
        else:
            failed_active.append(name)
    return {
        "counts": {
            "total": len(cron_rows),
            "active": len(active),
            "paused": len(paused),
            "failed_active": len(failed_active),
            "observed_failed_active": len(observed_failed_rows),
            "acknowledged_failed_active": len(acknowledged_failed),
        },
        "failed_active": sorted(failed_active),
        "observed_failed_active": sorted(
            str(row.get("name") or row.get("id")) for row in observed_failed_rows
        ),
        "acknowledged_failed_active": sorted(acknowledged_failed),
    }


def acknowledge_cron_failures(
    job_names: list[str] | None = None,
    *,
    acknowledged_by: str = "hermes-conversation",
    path: Path | None = None,
) -> dict[str, Any]:
    """Acknowledge current failures only; never create a permanent exception."""
    from cron.jobs import list_jobs

    cron_rows = list_jobs(include_disabled=True)
    failed_rows = _active_failed_cron_rows(cron_rows)
    requested = {str(name).strip() for name in (job_names or []) if str(name).strip()}
    acknowledge_all = not requested or "all" in {name.lower() for name in requested}
    rows = _load_failure_acknowledgements(path)
    acknowledged = []
    for row in failed_rows:
        name = str(row.get("name") or row.get("id"))
        if not acknowledge_all and name not in requested:
            continue
        rows[name] = {
            **_cron_failure_fingerprint(row),
            "job_id": str(row.get("id") or ""),
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
            "acknowledged_by": acknowledged_by,
        }
        acknowledged.append(name)
    _save_failure_acknowledgements(rows, path)
    return {
        "acknowledged": sorted(acknowledged),
        "not_currently_failed": sorted(requested - set(acknowledged))
        if not acknowledge_all
        else [],
        "scope": "exact_failed_run",
        "future_failures_alert_again": True,
    }


def clear_cron_failure_acknowledgements(
    job_names: list[str] | None = None,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    rows = _load_failure_acknowledgements(path)
    requested = {str(name).strip() for name in (job_names or []) if str(name).strip()}
    clear_all = not requested or "all" in {name.lower() for name in requested}
    cleared = sorted(rows) if clear_all else sorted(set(rows) & requested)
    if clear_all:
        rows = {}
    else:
        for name in cleared:
            rows.pop(name, None)
    _save_failure_acknowledgements(rows, path)
    return {"cleared": cleared}


def _worker_lane_healthy(
    _workers: list[dict[str, Any]],
    shells: list[dict[str, Any]],
) -> bool:
    """Evaluate the seven policy-selected routes, not unused candidate inventory."""
    return bool(shells) and all(row.get("healthy") for row in shells)


def build_heartbeat_snapshot() -> dict[str, Any]:
    """Read the three public heartbeat layers without an LLM."""
    from hermes_cli.config import load_config

    config = load_config()
    supervisor_cfg = config.get("supervisor") or {}
    with kb.connect_closing() as conn:
        registry.ensure_schema(conn)
        executor_health_probes = registry.refresh_executor_health_probes(conn)
        workers = registry.build_worker_health(conn)
        shells = registry.build_shell_health(conn)
        receipts = registry.receipt_summary(conn)
        task_rows = conn.execute(
            "SELECT status,COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        tasks = {row["status"]: int(row["n"]) for row in task_rows}

    services_error = None
    services: list[dict[str, Any]] = []
    try:
        from hermes_cli.application_services import status as service_status

        services = service_status(all_services=True).get("services", [])
    except Exception as exc:
        services_error = str(exc)

    artifacts_error = None
    artifacts: dict[str, Any] = {}
    try:
        from hermes_cli.artifact_health import build_artifact_health

        artifacts = build_artifact_health(
            config=supervisor_cfg.get("artifact_health") or {},
        )
    except Exception as exc:
        artifacts_error = str(exc)

    timeline_error = None
    timeline: dict[str, Any] = {}
    timeline_db = str(supervisor_cfg.get("timeline_db") or "").strip()
    if timeline_db:
        try:
            from hermes_timeline_code_map.store import TimelineCodeMap

            timeline_store = TimelineCodeMap(timeline_db)
            integrity = timeline_store.verify_all()
            neural = timeline_store.neural_link_status()
            indexes = timeline_store.list_code_indexes()
            timeline = {
                "configured": True,
                "db_path": timeline_db,
                "healthy": int(integrity.get("invalid_count") or 0) == 0,
                "integrity": integrity,
                "code_map": {
                    "repository_count": len(indexes),
                    "indexes": indexes,
                },
                "neural_link": neural,
            }
        except Exception as exc:
            timeline_error = str(exc)
            timeline = {
                "configured": True,
                "db_path": timeline_db,
                "healthy": False,
            }
    else:
        timeline = {
            "configured": False,
            "healthy": False,
            "status": "not_configured",
        }

    cron_error = None
    cron_rows: list[dict[str, Any]] = []
    try:
        from cron.jobs import list_jobs

        for job in list_jobs(include_disabled=True):
            cron_rows.append(
                {
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "state": job.get("state"),
                    "enabled": bool(job.get("enabled", True)),
                    "no_agent": bool(job.get("no_agent")),
                    "next_run_at": job.get("next_run_at"),
                    "last_run_at": job.get("last_run_at"),
                    "last_status": _cron_last_status(job),
                }
            )
    except Exception as exc:
        cron_error = str(exc)

    enabled_mcp = sorted(
        name
        for name, spec in (config.get("mcp_servers") or {}).items()
        if not isinstance(spec, dict) or spec.get("enabled", True)
    )
    platform_toolsets = config.get("platform_toolsets") or {}
    supervised_platforms = supervisor_cfg.get(
        "platforms", ["cli", "telegram", "discord", "slack"]
    )
    no_mcp_platforms = sorted(
        platform
        for platform in supervised_platforms
        if "no_mcp" in (platform_toolsets.get(platform) or [])
    )
    expected_paused = set(
        supervisor_cfg.get("expected_paused_cron", [])
    )
    actual_paused = {
        str(job.get("name") or job.get("id"))
        for job in cron_rows
        if job.get("state") == "paused" or not job.get("enabled", True)
    }
    failure_ack_error = None
    try:
        failure_acknowledgements = _load_failure_acknowledgements()
    except Exception as exc:
        failure_acknowledgements = {}
        failure_ack_error = str(exc)
    cron_inventory = _cron_inventory_summary(cron_rows, failure_acknowledgements)
    failed_enabled_cron = cron_inventory["failed_active"]
    missing_expected_pause = sorted(expected_paused - actual_paused)
    unexpected_paused = sorted(actual_paused - expected_paused)
    expected_services = set(
        supervisor_cfg.get("expected_services", [])
    )
    actual_services = {str(row.get("name") or row.get("id")) for row in services}
    missing_services = sorted(expected_services - actual_services)
    service_healthy = (
        True
        if not expected_services
        else services_error is None
        and not missing_services
        and all(
            row.get("in_sync")
            for row in services
            if str(row.get("name") or row.get("id")) in expected_services
        )
    )
    artifacts_healthy = bool(artifacts) and bool(artifacts.get("healthy"))
    required_cron = set(supervisor_cfg.get("required_cron", []))
    enabled_cron = {
        str(job.get("name") or job.get("id"))
        for job in cron_rows
        if job.get("enabled", True)
    }
    missing_required_cron = sorted(required_cron - enabled_cron)
    workers_healthy = _worker_lane_healthy(workers, shells)
    isolation_healthy = not enabled_mcp and set(no_mcp_platforms) == set(supervised_platforms)
    configuration_healthy = bool(
        workers_healthy
        and receipts.get("missing", 0) == 0
        and isolation_healthy
        and timeline.get("healthy")
        and timeline_error is None
    )
    service_schedule_healthy = bool(
        service_healthy
        and (services_error is None or not expected_services)
        and cron_error is None
        and not missing_required_cron
        and not missing_expected_pause
        and not unexpected_paused
        and not failed_enabled_cron
        and failure_ack_error is None
    )
    healthy = bool(
        configuration_healthy
        and service_schedule_healthy
        and artifacts_healthy
        and artifacts_error is None
    )
    legacy_lanes = {
        "service": {
            "healthy": service_healthy,
            "error": services_error,
            "services": services,
            "expected_services": sorted(expected_services),
            "missing_services": missing_services,
        },
        "artifacts": {
            **artifacts,
            "healthy": artifacts_healthy,
            "error": artifacts_error,
        },
        "worker": {
            "healthy": workers_healthy,
            "executors": workers,
            "executor_health_probes": executor_health_probes,
            "role_shells": shells,
            "tasks": tasks,
            "receipts": receipts,
        },
        "scheduled": {
            "healthy": service_schedule_healthy,
            "error": cron_error,
            "jobs": cron_rows,
            "counts": cron_inventory["counts"],
            "expected_paused": sorted(expected_paused),
            "missing_expected_pause": missing_expected_pause,
            "unexpected_paused": unexpected_paused,
            "required_cron": sorted(required_cron),
            "missing_required_cron": missing_required_cron,
            "failed_enabled_cron": failed_enabled_cron,
            "observed_failed_enabled_cron": cron_inventory[
                "observed_failed_active"
            ],
            "acknowledged_failed_cron": cron_inventory[
                "acknowledged_failed_active"
            ],
            "failure_acknowledgement_error": failure_ack_error,
        },
        "isolation": {
            "healthy": isolation_healthy,
            "enabled_root_mcp": enabled_mcp,
            "supervised_platforms": sorted(supervised_platforms),
            "no_mcp_platforms": no_mcp_platforms,
        },
    }
    return {
        "schema": HEARTBEAT_SNAPSHOT_SCHEMA,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "healthy": healthy,
        "layers": {
            "configuration": {
                "healthy": configuration_healthy,
                "isolation": legacy_lanes["isolation"],
                "workers_and_receipts": legacy_lanes["worker"],
                "timeline_code_map_neural_link": {
                    **timeline,
                    "error": timeline_error,
                },
            },
            "service_schedule": {
                "healthy": service_schedule_healthy,
                "services": legacy_lanes["service"],
                "schedule": legacy_lanes["scheduled"],
            },
            "artifacts": legacy_lanes["artifacts"],
        },
        "lanes": legacy_lanes,
    }


def build_supervisor_parser(subparsers, *, cmd_supervisor) -> None:
    parser = subparsers.add_parser(
        "supervisor",
        help="Lightweight role-shell supervisor control plane",
    )
    parser.set_defaults(func=cmd_supervisor)
    actions = parser.add_subparsers(dest="supervisor_action")
    actions.add_parser("init")
    install = actions.add_parser(
        "install",
        help="Install the zero-MCP root, executor profiles, role shells, and heartbeat",
    )
    install.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Hermes code checkout containing scripts/hermes_supervisor_heartbeat.py",
    )
    install.add_argument("--dry-run", action="store_true")
    heartbeat = actions.add_parser("heartbeat")
    heartbeat.add_argument("--json", action="store_true")
    heartbeat.add_argument("--quiet-healthy", action="store_true")

    shell = actions.add_parser("shell")
    shell_actions = shell.add_subparsers(dest="shell_action")
    shell_add = shell_actions.add_parser("add-version")
    shell_add.add_argument("--key", required=True)
    shell_add.add_argument("--name", required=True)
    shell_add.add_argument("--description")
    shell_add.add_argument("--contract", required=True, help="JSON or JSON file")
    shell_add.add_argument("--required-capability", action="append", default=[])
    shell_add.add_argument("--allowed-capability", action="append", default=[])
    shell_add.add_argument(
        "--evidence-policy",
        default=(
            '{"timeline_required":true,"neural_recall_required":true,'
            '"code_slice_required":true,"verify_all_invalid_count":0,'
            '"outputs_required":true}'
        ),
    )
    shell_list = shell_actions.add_parser("list")
    shell_list.add_argument("--active", action="store_true")
    shell_list.add_argument("--json", action="store_true")

    executor = actions.add_parser("executor")
    executor_actions = executor.add_subparsers(dest="executor_action")
    executor_add = executor_actions.add_parser("add")
    executor_add.add_argument("--id")
    executor_add.add_argument("--name", required=True)
    executor_add.add_argument("--adapter", required=True, choices=["hermes_profile", "command", "manual"])
    executor_add.add_argument("--launch", required=True, help="JSON or JSON file")
    executor_add.add_argument("--capability", action="append", default=[])
    executor_add.add_argument("--capacity", type=int, default=1)
    executor_add.add_argument("--heartbeat-ttl", type=int, default=300)
    executor_add.add_argument("--no-heartbeat", action="store_true")
    executor_list = executor_actions.add_parser("list")
    executor_list.add_argument("--json", action="store_true")
    executor_heartbeat = executor_actions.add_parser("heartbeat")
    executor_heartbeat.add_argument("executor_id")
    executor_heartbeat.add_argument("--state", default="healthy")
    for action in ("enable", "disable"):
        child = executor_actions.add_parser(action)
        child.add_argument("executor_id")

    binding = actions.add_parser("binding")
    binding_actions = binding.add_subparsers(dest="binding_action")
    binding_add = binding_actions.add_parser("add")
    binding_add.add_argument("--shell", required=True)
    binding_add.add_argument("--executor", required=True)
    binding_add.add_argument("--priority", type=int, default=0)
    binding_add.add_argument("--weight", type=float, default=1.0)
    binding_add.add_argument("--capability", action="append", default=[])
    binding_add.add_argument("--constraints", default="{}")
    binding_list = binding_actions.add_parser("list")
    binding_list.add_argument("--shell")
    binding_list.add_argument("--executor")
    binding_list.add_argument("--json", action="store_true")
    for action in ("enable", "disable"):
        child = binding_actions.add_parser(action)
        child.add_argument("binding_id")

    adapter = actions.add_parser(
        "adapter",
        help="Manage Kanban role-to-executor ownership and auditable overrides",
    )
    adapter_actions = adapter.add_subparsers(dest="adapter_action")
    adapter_list = adapter_actions.add_parser("list")
    adapter_list.add_argument("--history-limit", type=int, default=50)
    adapter_list.add_argument("--json", action="store_true")
    adapter_history = adapter_actions.add_parser("history")
    adapter_history.add_argument("--task")
    adapter_history.add_argument("--scope-type", choices=["task", "shell", "all"])
    adapter_history.add_argument("--scope-key")
    adapter_history.add_argument("--limit", type=int, default=100)
    adapter_assign = adapter_actions.add_parser("assign")
    adapter_assign.add_argument("shell")
    adapter_assign.add_argument("executor")
    ownership = adapter_assign.add_mutually_exclusive_group()
    ownership.add_argument("--primary", action="store_true")
    ownership.add_argument("--candidate", action="store_true")
    adapter_assign.add_argument("--priority", type=int)
    adapter_assign.add_argument("--weight", type=float)
    adapter_assign.add_argument("--note")
    adapter_switch = adapter_actions.add_parser("switch")
    adapter_switch.add_argument("target", help="task id, shell key/id, or all")
    adapter_switch.add_argument("executor")
    adapter_switch.add_argument("--scope-type", choices=["task", "shell", "all"])
    mode = adapter_switch.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--temporary-seconds", type=int)
    mode.add_argument("--permanent", action="store_true")
    adapter_switch.add_argument("--reason")
    adapter_clear = adapter_actions.add_parser("clear")
    adapter_clear.add_argument("override_id")
    adapter_clear.add_argument("--reason")
    adapter_rerun = adapter_actions.add_parser("rerun")
    adapter_rerun.add_argument("task_id")
    adapter_rerun.add_argument("executor")
    adapter_rerun.add_argument("--reason")
    adapter_inspect = adapter_actions.add_parser("inspect")
    adapter_inspect.add_argument("task_id")

    route = actions.add_parser("route")
    route.add_argument("role_shell_id")
    route.add_argument("--json", action="store_true")


def supervisor_command(args: argparse.Namespace) -> int:
    action = args.supervisor_action or "heartbeat"
    if action == "install":
        from hermes_constants import get_hermes_home
        from hermes_cli.supervisor_bootstrap import (
            bootstrap_supervisor,
            format_bootstrap_summary,
        )

        result = bootstrap_supervisor(
            home=get_hermes_home(),
            repo_root=Path(args.repo_root),
            dry_run=bool(args.dry_run),
        )
        print(format_bootstrap_summary(result))
        return 0
    if action == "heartbeat":
        snapshot = build_heartbeat_snapshot()
        if args.quiet_healthy and snapshot["healthy"]:
            return 0
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0 if snapshot["healthy"] else 1
    with kb.connect_closing() as conn:
        registry.ensure_schema(conn)
        if action == "init":
            print("Hermes supervisor registry initialized")
            return 0
        if action == "shell":
            if getattr(args, "shell_action", None) is None:
                print("supervisor shell requires an action", file=sys.stderr)
                return 2
            if args.shell_action == "add-version":
                shell = registry.register_shell_version(
                    conn,
                    shell_key=args.key,
                    name=args.name,
                    description=args.description,
                    contract=_json_arg(args.contract),
                    required_capabilities=args.required_capability,
                    allowed_capabilities=args.allowed_capability,
                    evidence_policy=_json_arg(args.evidence_policy),
                )
                print(json.dumps(_shell_dict(shell), ensure_ascii=False, indent=2))
                return 0
            shells = registry.list_shells(conn, active_only=args.active)
            if args.json:
                print(json.dumps([_shell_dict(item) for item in shells], ensure_ascii=False, indent=2))
            else:
                for item in shells:
                    print(f"{item.id} {item.shell_key} v{item.version} {item.contract_hash[:12]}")
            return 0
        if action == "executor":
            if getattr(args, "executor_action", None) is None:
                print("supervisor executor requires an action", file=sys.stderr)
                return 2
            if args.executor_action == "add":
                item = registry.register_executor(
                    conn,
                    executor_id=args.id,
                    name=args.name,
                    adapter_type=args.adapter,
                    launch_config=_json_arg(args.launch),
                    capabilities=args.capability,
                    capacity=args.capacity,
                    heartbeat_required=not args.no_heartbeat,
                    heartbeat_ttl_seconds=args.heartbeat_ttl,
                )
                print(json.dumps(_executor_dict(item), ensure_ascii=False, indent=2))
                return 0
            if args.executor_action == "heartbeat":
                return 0 if registry.heartbeat_executor(conn, args.executor_id, health_state=args.state) else 1
            if args.executor_action in {"enable", "disable"}:
                return 0 if registry.set_executor_enabled(conn, args.executor_id, args.executor_action == "enable") else 1
            items = registry.list_executors(conn)
            if args.json:
                print(json.dumps([_executor_dict(item) for item in items], ensure_ascii=False, indent=2))
            else:
                for item in items:
                    print(f"{item.id} {item.adapter_type} {item.health_state} {item.capacity}")
            return 0
        if action == "binding":
            if getattr(args, "binding_action", None) is None:
                print("supervisor binding requires an action", file=sys.stderr)
                return 2
            if args.binding_action == "add":
                item = registry.bind_executor(
                    conn,
                    shell_id=args.shell,
                    executor_id=args.executor,
                    priority=args.priority,
                    weight=args.weight,
                    capability_cap=args.capability,
                    constraints=_json_arg(args.constraints),
                )
                print(json.dumps(_binding_dict(item), ensure_ascii=False, indent=2))
                return 0
            if args.binding_action in {"enable", "disable"}:
                return 0 if registry.set_binding_enabled(conn, args.binding_id, args.binding_action == "enable") else 1
            items = registry.list_bindings(conn, shell_id=args.shell, executor_id=args.executor)
            if args.json:
                print(json.dumps([_binding_dict(item) for item in items], ensure_ascii=False, indent=2))
            else:
                for item in items:
                    print(f"{item.id} {item.shell_id} -> {item.executor_id} p={item.priority} enabled={item.enabled}")
            return 0
        if action == "adapter":
            adapter_action = getattr(args, "adapter_action", None)
            if adapter_action is None:
                print("supervisor adapter requires an action", file=sys.stderr)
                return 2
            try:
                if adapter_action == "list":
                    view = registry.adapter_registry_view(
                        conn, history_limit=args.history_limit
                    )
                    if args.json:
                        print(json.dumps(view, ensure_ascii=False, indent=2))
                    else:
                        print(f"{view['control_slot_count']} control slots")
                        for slot in view["control_slots"]:
                            runtime = slot.get("runtime") or {}
                            profile = runtime.get("profile") or "-"
                            label = runtime.get("display_label") or "unconfigured"
                            source = slot.get("selection_source") or "-"
                            print(
                                f"{slot['slot_key']} profile={profile} · {label} "
                                f"[{source}]"
                            )
                        global_override = view.get("global_override")
                        if global_override:
                            print(
                                "ALL override -> "
                                f"{global_override['executor_id']} "
                                f"({global_override['mode']}, {global_override['override_id']})"
                            )
                    return 0
                if adapter_action == "history":
                    rows = registry.list_adapter_events(
                        conn,
                        task_id=args.task,
                        scope_type=args.scope_type,
                        scope_key=args.scope_key,
                        limit=args.limit,
                    )
                    print(json.dumps({"history": rows}, ensure_ascii=False, indent=2))
                    return 0
                if adapter_action == "assign":
                    item = registry.assign_adapter(
                        conn,
                        shell_value=args.shell,
                        executor_value=args.executor,
                        responsibility=("primary" if args.primary else "candidate"),
                        priority=args.priority,
                        weight=args.weight,
                        note=args.note,
                        assigned_by="cli",
                    )
                    print(json.dumps(_binding_dict(item), ensure_ascii=False, indent=2))
                    return 0
                if adapter_action == "switch":
                    switch_mode = (
                        "once" if args.once else
                        "temporary" if args.temporary_seconds is not None else
                        "permanent"
                    )
                    item = registry.create_adapter_override(
                        conn,
                        target=args.target,
                        scope_type=args.scope_type,
                        executor_value=args.executor,
                        mode=switch_mode,
                        duration_seconds=args.temporary_seconds,
                        reason=args.reason,
                        created_by="cli",
                    )
                    print(json.dumps(registry._override_dict(item), ensure_ascii=False, indent=2))
                    return 0
                if adapter_action == "clear":
                    ok = registry.clear_adapter_override(
                        conn,
                        args.override_id,
                        cleared_by="cli",
                        reason=args.reason,
                    )
                    print(json.dumps({"cleared": ok, "override_id": args.override_id}))
                    return 0 if ok else 1
                if adapter_action == "rerun":
                    result = registry.reissue_task_with_adapter(
                        conn,
                        task_id=args.task_id,
                        executor_value=args.executor,
                        reason=args.reason,
                        created_by="cli",
                    )
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
                if adapter_action == "inspect":
                    print(
                        json.dumps(
                            registry.inspect_task_adapter(conn, args.task_id),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return 0
            except registry.SupervisorRegistryError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        if action == "route":
            try:
                selected = registry.select_binding(conn, args.role_shell_id)
            except registry.NoEligibleExecutor as exc:
                print(str(exc), file=sys.stderr)
                return 1
            payload = {
                "role_shell_id": selected.shell.id,
                "executor_id": selected.executor.id,
                "binding_id": selected.binding.id,
                "adapter_override_id": selected.adapter_override_id,
                "effective_capabilities": selected.effective_capabilities,
                "active_runs": selected.active_runs,
                "capacity": selected.executor.capacity,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
    print(f"unknown supervisor action: {action}", file=sys.stderr)
    return 2
