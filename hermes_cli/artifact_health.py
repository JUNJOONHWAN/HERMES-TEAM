"""Portable, configuration-driven artifact checks for supervisor heartbeat.

The public distribution has no knowledge of an operator's private repositories,
trading services, report names, or home-directory layout.  Operators opt in to
small path contracts under ``supervisor.artifact_health.checks``.  Disabled or
unconfigured checks are explicitly reported as ``not_configured`` and do not
make an otherwise clean supervisor unhealthy.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARTIFACT_SCHEMA = "hermes.supervisor.artifacts.public.v1"


def _now_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _resolve_path(raw: Any, *, base_dir: Path) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("path is required")
    path = Path(text).expanduser()
    return (path if path.is_absolute() else base_dir / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_path(
    spec: dict[str, Any], *, base_dir: Path, now_utc: datetime
) -> dict[str, Any]:
    name = str(spec.get("name") or spec.get("path") or "unnamed").strip()
    required = bool(spec.get("required", True))
    try:
        path = _resolve_path(spec.get("path"), base_dir=base_dir)
        expected_kind = str(spec.get("kind") or "any").strip().lower()
        if expected_kind not in {"any", "file", "directory"}:
            raise ValueError("kind must be any, file, or directory")
        exists = path.exists()
        if not exists:
            return {
                "name": name,
                "type": "path",
                "path": str(path),
                "required": required,
                "healthy": not required,
                "status": "missing" if required else "optional_missing",
                "evidence": {"exists": False},
            }
        if expected_kind == "file" and not path.is_file():
            raise ValueError("expected a file")
        if expected_kind == "directory" and not path.is_dir():
            raise ValueError("expected a directory")
        stat = path.stat()
        evidence: dict[str, Any] = {
            "exists": True,
            "kind": "file" if path.is_file() else "directory",
            "size_bytes": stat.st_size,
            "mtime_utc": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
        }
        failures: list[str] = []
        min_bytes = spec.get("min_bytes")
        if min_bytes is not None:
            minimum = max(0, int(min_bytes))
            evidence["min_bytes"] = minimum
            if stat.st_size < minimum:
                failures.append("below_min_bytes")
        max_age = spec.get("max_age_seconds")
        if max_age is not None:
            maximum = max(0.0, float(max_age))
            age = max(
                0.0,
                (now_utc - datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                )).total_seconds(),
            )
            evidence.update({"age_seconds": age, "max_age_seconds": maximum})
            if age > maximum:
                failures.append("stale")
        expected_sha = str(spec.get("sha256") or "").strip().lower()
        if expected_sha:
            if not path.is_file():
                failures.append("sha256_requires_file")
            else:
                actual_sha = _sha256(path)
                evidence.update(
                    {"expected_sha256": expected_sha, "actual_sha256": actual_sha}
                )
                if actual_sha != expected_sha:
                    failures.append("sha256_mismatch")
        return {
            "name": name,
            "type": "path",
            "path": str(path),
            "required": required,
            "healthy": not failures,
            "status": "ok" if not failures else failures[0],
            "failures": failures,
            "evidence": evidence,
        }
    except Exception as exc:
        return {
            "name": name,
            "type": "path",
            "required": required,
            "healthy": False,
            "status": "invalid_contract",
            "error": str(exc),
            "evidence": {},
        }


def build_artifact_health(
    *,
    now_utc: datetime | None = None,
    hermes_home: Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate configured artifact contracts without running shell commands."""
    current = _now_utc(now_utc)
    config = dict(config or {})
    if hermes_home is None:
        from hermes_constants import get_hermes_home

        hermes_home = get_hermes_home()
    base_dir = Path(hermes_home).expanduser().resolve()
    enabled = bool(config.get("enabled", False))
    raw_checks = config.get("checks") or []
    if not isinstance(raw_checks, list):
        raise ValueError("artifact_health.checks must be a list")
    if not enabled:
        return {
            "schema": ARTIFACT_SCHEMA,
            "checked_at_utc": current.isoformat(),
            "enabled": False,
            "healthy": True,
            "status": "not_configured",
            "healthy_count": 0,
            "total": 0,
            "checks": [],
        }
    checks: list[dict[str, Any]] = []
    for raw in raw_checks:
        if not isinstance(raw, dict):
            checks.append(
                {
                    "name": "unnamed",
                    "type": "unknown",
                    "required": True,
                    "healthy": False,
                    "status": "invalid_contract",
                    "error": "check must be an object",
                    "evidence": {},
                }
            )
            continue
        check_type = str(raw.get("type") or "path").strip().lower()
        if check_type != "path":
            checks.append(
                {
                    "name": str(raw.get("name") or "unnamed"),
                    "type": check_type,
                    "required": bool(raw.get("required", True)),
                    "healthy": False,
                    "status": "unsupported_type",
                    "error": "the public checker supports path contracts only",
                    "evidence": {},
                }
            )
            continue
        checks.append(_check_path(raw, base_dir=base_dir, now_utc=current))
    healthy_count = sum(1 for row in checks if row.get("healthy"))
    return {
        "schema": ARTIFACT_SCHEMA,
        "checked_at_utc": current.isoformat(),
        "enabled": True,
        "healthy": healthy_count == len(checks),
        "status": "ok" if healthy_count == len(checks) else "failed",
        "healthy_count": healthy_count,
        "total": len(checks),
        "checks": checks,
    }
