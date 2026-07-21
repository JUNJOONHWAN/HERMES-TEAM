"""Desired-state control for Hermes-managed application services.

The process supervisor is systemd.  Hermes owns the durable operator intent:
``running`` means reconcile/start after crashes and reboots; ``stopped`` means
leave the service down until the operator explicitly starts it again.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DEFAULT_HOME = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
DEFAULT_CATALOG = DEFAULT_HOME / "service-manager/services.json"


class ApplicationServiceError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


@contextmanager
def _locked_catalog(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ApplicationServiceError(f"service catalog missing: {path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
            raise ApplicationServiceError(f"invalid service catalog: {path}")
        yield data
        _write_catalog(path, data)


def _write_catalog(path: Path, data: dict[str, Any]) -> None:
    """Atomically persist a catalog while its caller holds the lock."""
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _state(unit: str) -> dict[str, Any]:
    active = _systemctl("is-active", unit).stdout.strip()
    enabled = _systemctl("is-enabled", unit).stdout.strip()
    show = _systemctl("show", unit, "--property=MainPID,NRestarts,Result")
    values = dict(
        line.split("=", 1) for line in show.stdout.splitlines() if "=" in line
    )
    return {
        "active_state": active or "unknown",
        "enabled_state": enabled or "unknown",
        "main_pid": int(values.get("MainPID", "0")) if values.get("MainPID", "0").isdigit() else 0,
        "restart_count": int(values.get("NRestarts", "0")) if values.get("NRestarts", "0").isdigit() else 0,
        "result": values.get("Result", "unknown"),
        "healthy": active == "active",
    }


def _select(data: dict[str, Any], names: Sequence[str] | None, all_services: bool) -> list[str]:
    services = data["services"]
    if all_services or not names:
        return list(services)
    aliases = data.get("aliases", {})
    selected: list[str] = []
    for raw in names:
        expanded = aliases.get(raw, [raw])
        for name in expanded:
            if name not in services:
                raise ApplicationServiceError(f"unknown service {name!r}")
            if name not in selected:
                selected.append(name)
    return selected


def status(names: Sequence[str] | None = None, *, all_services: bool = False,
           catalog: Path = DEFAULT_CATALOG) -> dict[str, Any]:
    with _locked_catalog(catalog) as data:
        selected = _select(data, names, all_services)
        rows = []
        for name in selected:
            spec = data["services"][name]
            row = {"name": name, "unit": spec["unit"], "desired_state": spec.get("desired_state", "stopped")}
            row.update(_state(spec["unit"]))
            row["in_sync"] = row["healthy"] == (row["desired_state"] == "running")
            rows.append(row)
    return {"checked_at_kst": _now(), "services": rows, "in_sync": all(r["in_sync"] for r in rows)}


def set_state(action: str, names: Sequence[str], *, catalog: Path = DEFAULT_CATALOG,
              dry_run: bool = False) -> dict[str, Any]:
    if action not in {"start", "stop", "restart"}:
        raise ApplicationServiceError(f"unsupported action: {action}")
    with _locked_catalog(catalog) as data:
        selected = _select(data, names, False)
        rows = []
        desired = "stopped" if action == "stop" else "running"
        if not dry_run:
            for name in selected:
                spec = data["services"][name]
                spec["desired_state"] = desired
                spec["desired_state_updated_at_kst"] = _now()
            # Commit operator intent before touching any process.  A watchdog
            # racing an explicit stop can never undo that stop.
            _write_catalog(catalog, data)
        for name in selected:
            spec = data["services"][name]
            unit = spec["unit"]
            if not dry_run:
                command = ("disable", "--now", unit) if action == "stop" else (
                    ("enable", "--now", unit) if action == "start" else ("enable", unit)
                )
                result = _systemctl(*command)
                if result.returncode != 0:
                    raise ApplicationServiceError(result.stderr.strip() or f"systemctl {action} failed: {unit}")
                if action == "restart":
                    result = _systemctl("restart", unit)
                    if result.returncode != 0:
                        raise ApplicationServiceError(result.stderr.strip() or f"systemctl restart failed: {unit}")
            rows.append({"name": name, "unit": unit, "desired_state": desired, "dry_run": dry_run})
    result = status(selected, catalog=catalog)
    result["action"] = action
    result["requested"] = rows
    return result


def reconcile(names: Sequence[str] | None = None, *, all_services: bool = False,
              catalog: Path = DEFAULT_CATALOG) -> dict[str, Any]:
    actions: list[dict[str, str]] = []
    with _locked_catalog(catalog) as data:
        selected = _select(data, names, all_services)
        for name in selected:
            spec = data["services"][name]
            unit = spec["unit"]
            desired = spec.get("desired_state", "stopped")
            actual = _state(unit)
            if desired == "running" and not actual["healthy"]:
                result = _systemctl("enable", "--now", unit)
                actions.append({"name": name, "action": "start", "result": "ok" if result.returncode == 0 else "error"})
            elif desired == "stopped" and actual["healthy"]:
                result = _systemctl("disable", "--now", unit)
                actions.append({"name": name, "action": "stop", "result": "ok" if result.returncode == 0 else "error"})
    report = status(selected, catalog=catalog)
    report["actions"] = actions
    report["status"] = "ok" if report["in_sync"] else "error"
    return report
