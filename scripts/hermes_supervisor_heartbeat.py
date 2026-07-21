#!/usr/bin/env python3
"""Deterministic three-layer heartbeat for the public Hermes supervisor.

The heartbeat never invokes a model. It reports only the canonical public
layers: configuration, service/schedule, and artifacts.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Cron executes the installed copy from ~/.hermes/scripts, where the Hermes
# checkout is not automatically on sys.path. Resolve only the configured copy.
_default_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
_agent_root = Path(
    os.environ.get("HERMES_AGENT_ROOT") or _default_home / "hermes-agent"
).expanduser().resolve()
if _agent_root.is_dir() and str(_agent_root) not in sys.path:
    sys.path.insert(0, str(_agent_root))

from hermes_cli.supervisor_cli import build_heartbeat_snapshot, save_heartbeat_snapshot


def _healthy_count(rows: list[dict[str, Any]], *, key: str = "healthy") -> int:
    return sum(1 for row in rows if bool(row.get(key)))


def _names(values: list[Any], *, limit: int = 3) -> str:
    names = [str(value) for value in values if str(value)]
    if len(names) <= limit:
        return ",".join(names)
    return f"{','.join(names[:limit])} 외 {len(names) - limit}개"


def _icon(healthy: bool) -> str:
    return "✅" if healthy else "⚠️"


def _canonical_layers(snapshot: dict[str, Any]) -> dict[str, Any]:
    layers = snapshot.get("layers")
    if isinstance(layers, dict):
        return layers

    # Compatibility for snapshots written before the public v2 schema.
    lanes = snapshot.get("lanes") or {}
    worker = lanes.get("worker") or {}
    isolation = lanes.get("isolation") or {}
    service = lanes.get("service") or {}
    scheduled = lanes.get("scheduled") or {}
    return {
        "configuration": {
            "healthy": bool(worker.get("healthy") and isolation.get("healthy")),
            "workers_and_receipts": worker,
            "isolation": isolation,
            "timeline_code_map_neural_link": {
                "configured": False,
                "healthy": False,
                "status": "not_reported_by_legacy_snapshot",
            },
        },
        "service_schedule": {
            "healthy": bool(service.get("healthy") and scheduled.get("healthy")),
            "services": service,
            "schedule": scheduled,
        },
        "artifacts": lanes.get("artifacts") or {},
    }


def _configuration_lines(layer: dict[str, Any]) -> list[str]:
    worker = layer.get("workers_and_receipts") or {}
    shells = worker.get("role_shells") or []
    executors = [
        row for row in (worker.get("executors") or []) if row.get("enabled", True)
    ]
    receipts = worker.get("receipts") or {}
    isolation = layer.get("isolation") or {}
    timeline = layer.get("timeline_code_map_neural_link") or {}
    integrity = timeline.get("integrity") or {}
    code_map = timeline.get("code_map") or {}
    neural = timeline.get("neural_link") or {}
    lines = [
        f"1층 · 구성 상태 {_icon(bool(layer.get('healthy')))}",
        f"  역할 셸: {_healthy_count(shells)}/{len(shells)}",
        f"  실행기: {_healthy_count(executors)}/{len(executors)}",
        f"  영수증 누락: {int(receipts.get('missing') or 0)}",
        f"  루트 MCP: {len(isolation.get('enabled_root_mcp') or [])}",
    ]
    if timeline.get("configured"):
        lines.extend(
            [
                "  Timeline: "
                + ("정상" if timeline.get("healthy") else "점검 필요")
                + f" / 무결성 오류 {int(integrity.get('invalid_count') or 0)}",
                f"  Code Map: 저장소 {int(code_map.get('repository_count') or 0)}",
                "  NeuralLink: "
                + ("정상" if neural.get("healthy", True) else "점검 필요")
                + f" / 인덱스 {int(neural.get('indexed_nodes') or neural.get('indexed_count') or 0)}",
            ]
        )
    else:
        lines.append(f"  Timeline/Code Map/NeuralLink: {timeline.get('status') or '미구성'}")
    return lines


def _service_schedule_lines(layer: dict[str, Any]) -> list[str]:
    service = layer.get("services") or {}
    schedule = layer.get("schedule") or {}
    services = service.get("services") or []
    jobs = schedule.get("jobs") or []
    counts = schedule.get("counts") or {}
    if not counts:
        active = [row for row in jobs if row.get("enabled", True)]
        counts = {
            "total": len(jobs),
            "active": len(active),
            "paused": len(jobs) - len(active),
            "failed_active": len(schedule.get("failed_enabled_cron") or []),
        }
    lines = [
        f"2층 · 서비스·스케줄 {_icon(bool(layer.get('healthy')))}",
        (
            f"  서비스: {_healthy_count(services, key='in_sync')}/{len(services)}"
            if services
            else "  서비스: 등록 없음"
        ),
        (
            f"  스케줄: 전체 {int(counts.get('total') or 0)}"
            f" / 활성 {int(counts.get('active') or 0)}"
            f" / 일시정지 {int(counts.get('paused') or 0)}"
            f" / 실패 {int(counts.get('failed_active') or 0)}"
        ),
    ]
    for label, values in (
        ("누락 서비스", service.get("missing_services") or []),
        ("누락 필수 스케줄", schedule.get("missing_required_cron") or []),
        ("최근 실패", schedule.get("failed_enabled_cron") or []),
        ("비정상 중지", schedule.get("unexpected_paused") or []),
    ):
        if values:
            lines.append(f"  {label}: {_names(values)}")
    if service.get("error"):
        lines.append("  서비스 점검 오류")
    if schedule.get("error"):
        lines.append("  스케줄 점검 오류")
    return lines


def _artifact_lines(layer: dict[str, Any]) -> list[str]:
    checks = layer.get("checks") or []
    lines = [
        f"3층 · 산출물 {_icon(bool(layer.get('healthy')))}",
        f"  점검: {_healthy_count(checks)}/{len(checks)}",
    ]
    if not checks:
        status = str(layer.get("status") or "not_configured")
        lines.append(f"  등록된 산출물 점검 없음 ({status})")
    for row in checks:
        name = str(row.get("name") or row.get("path") or "unnamed")
        state = str(row.get("status") or ("ok" if row.get("healthy") else "failed"))
        summary = str(row.get("summary") or "")
        suffix = f" — {summary}" if summary else ""
        lines.append(f"  {_icon(bool(row.get('healthy')))} {name}: {state.upper()}{suffix}")
    if layer.get("error"):
        lines.append("  산출물 점검 오류")
    return lines


def _format_summary(snapshot: dict[str, Any]) -> str:
    layers = _canonical_layers(snapshot)
    overall = "정상" if snapshot.get("healthy") else "주의"
    sections = [
        [f"{_icon(bool(snapshot.get('healthy')))} Hermes heartbeat 전체 {overall}"],
        _configuration_lines(layers.get("configuration") or {}),
        _service_schedule_lines(layers.get("service_schedule") or {}),
        _artifact_lines(layers.get("artifacts") or {}),
    ]
    return "\n\n".join("\n".join(section) for section in sections)


def main() -> int:
    snapshot = build_heartbeat_snapshot()
    save_heartbeat_snapshot(snapshot)
    print(_format_summary(snapshot))
    # Degraded state is carried in the snapshot and report. The cron job still
    # exits successfully so delivery failures stay distinct from health alerts.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
