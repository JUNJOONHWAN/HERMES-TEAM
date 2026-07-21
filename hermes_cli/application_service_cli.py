"""Command handler for ``hermes service``."""

from __future__ import annotations

import json
import sys

from hermes_cli.application_services import ApplicationServiceError, reconcile, set_state, status


def _print(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    for row in report.get("services", []):
        mark = "ok" if row.get("in_sync") else "warn"
        print(
            f"[{mark}] {row['name']} desired={row['desired_state']} "
            f"actual={row['active_state']} enabled={row['enabled_state']} "
            f"pid={row['main_pid']} restarts={row['restart_count']}"
        )
    for action in report.get("actions", []):
        print(f"[reconcile] {action['name']} {action['action']} {action['result']}")


def service_command(args) -> int:
    try:
        action = args.service_action or "status"
        if action in {"status", "list"}:
            report = status(args.services, all_services=args.all)
        elif action in {"start", "stop", "restart"}:
            if not args.services:
                raise ApplicationServiceError(f"{action} requires a service or alias")
            report = set_state(action, args.services, dry_run=args.dry_run)
        elif action == "reconcile":
            report = reconcile(args.services, all_services=args.all)
        else:
            raise ApplicationServiceError(f"unknown service action: {action}")
    except ApplicationServiceError as exc:
        print(f"hermes service: {exc}", file=sys.stderr)
        return 2
    _print(report, args.json)
    return 0 if report.get("in_sync", True) else 1
