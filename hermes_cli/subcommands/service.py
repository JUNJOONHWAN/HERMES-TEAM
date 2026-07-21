"""``hermes service`` parser."""

from __future__ import annotations

from typing import Callable


def build_service_parser(subparsers, *, cmd_service: Callable) -> None:
    parser = subparsers.add_parser(
        "service", help="Hermes-managed application service control"
    )
    actions = parser.add_subparsers(dest="service_action")
    for action in ("status", "list", "start", "stop", "restart", "reconcile"):
        child = actions.add_parser(action)
        child.add_argument("services", nargs="*")
        child.add_argument("--all", action="store_true", help="Target every registered application service")
        child.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
        if action in {"start", "stop", "restart"}:
            child.add_argument("--dry-run", action="store_true")
        else:
            child.set_defaults(dry_run=False)
    parser.set_defaults(func=cmd_service, services=[], all=True, json=False, dry_run=False)
