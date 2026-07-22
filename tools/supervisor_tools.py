"""Model tools for the fixed lightweight Hermes control tower.

The supervisor may inspect deterministic control-plane state and create a
Kanban card against an active immutable role shell. It cannot execute domain
work, choose an executor directly, or widen the shell's capabilities.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb
from hermes_cli import project_card_controller as project_cards
from hermes_cli import supervisor_registry as registry
from hermes_cli.config import load_config
from hermes_cli.supervisor_tool_catalog import (
    build_tool_catalog,
    compact_tool_catalog,
    search_tool_catalog,
)
from tools.registry import registry as tool_registry, tool_error


DEFAULT_REPAIR_EXECUTOR_ID = "executor_hermes_worker_universal"
_REPAIR_TERMS = (
    "수선", "수정", "고치", "복구", "버그", "오류", "장애", "문제 해결",
    "repair", "fix", "debug", "bug", "incident", "restore", "recover",
    "remediation", "hotfix",
)

_ROLE_ADAPTER_LABELS = {
    "browser-research": "브라우저 조사",
    "code": "코드 변경",
    "market": "시장 분석",
    "operations": "런타임 운영",
    "report": "리포트 작성",
    "verification": "독립 검증",
    "tool-management": "멀티툴 관리",
}
_MOBILE_ROLE_LABELS = {
    "browser-research": "브라우저",
    "code": "코드",
    "market": "시장",
    "operations": "운영",
    "report": "리포트",
    "verification": "검증",
    "tool-management": "멀티툴",
}
_ROLE_OPERATOR_DETAILS = {
    "browser-research": {
        "scope": ("로그인·동적 웹 근거 수집",),
        "tools": ("Browser·Web·Timeline",),
    },
    "code": {
        "scope": ("지정 소스 수정·테스트",),
        "tools": ("File·Terminal·Timeline",),
    },
    "market": {
        "scope": ("공개 시장 자료·공시 조사",),
        "tools": (
            "Web·Browser",
            "선택형 Market MCP",
            "Timeline",
        ),
    },
    "operations": {
        "scope": ("서비스·크론·워치독 관리",),
        "tools": ("File·Terminal", "Cron·Timeline"),
    },
    "report": {
        "scope": ("영수증·근거 종합", "보고서 조립"),
        "tools": ("File·Kanban·Timeline",),
    },
    "verification": {
        "scope": ("독립 재조회·회귀", "완료 판정"),
        "tools": ("File·Terminal", "Kanban·Timeline"),
    },
    "tool-management": {
        "scope": ("MCP·스킬·툴 등록", "역할별 배정·검증"),
        "tools": ("File·Terminal·Web", "Skills·Kanban·Timeline"),
    },
}
_ROLE_ADAPTER_COUNT = len(_ROLE_ADAPTER_LABELS)
_WORKER_STATE_ORDER = {
    "사용 중": 0,
    "대기": 1,
    "주의": 2,
    "꺼짐": 3,
    "사용 불가": 4,
}
_OVERRIDE_MODE_LABELS = {
    "once": "1회",
    "temporary": "임시",
    "permanent": "영구",
}
STATUS_SNAPSHOT_MAX_AGE_SECONDS = 90 * 60


def _check_supervisor_mode() -> bool:
    try:
        return registry.supervisor_root_enabled(load_config())
    except Exception:
        return False


def _repair_executor_id() -> str:
    config = load_config() or {}
    supervisor = config.get("supervisor") or {}
    if not isinstance(supervisor, dict):
        supervisor = {}
    return (
        str(supervisor.get("repair_executor_id") or "").strip()
        or DEFAULT_REPAIR_EXECUTOR_ID
    )


def _repair_work_requested(args: dict[str, Any], shell_key: str) -> bool:
    explicit = str(args.get("work_kind") or "").strip().lower()
    if explicit == "repair":
        return True
    if shell_key not in {"code", "operations"}:
        return False
    text = " ".join(
        str(args.get(name) or "").strip().lower() for name in ("title", "body")
    )
    return any(term in text for term in _REPAIR_TERMS)


def _compact_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    canonical_layers = snapshot.get("layers") or {}
    configuration_layer = canonical_layers.get("configuration") or {}
    service_schedule_layer = canonical_layers.get("service_schedule") or {}
    lanes = snapshot.get("lanes") or {}
    service = lanes.get("service") or {}
    worker = lanes.get("worker") or {}
    scheduled = lanes.get("scheduled") or {}
    isolation = lanes.get("isolation") or {}
    artifacts = lanes.get("artifacts") or {}
    return {
        "schema": snapshot.get("schema"),
        "healthy": bool(snapshot.get("healthy")),
        "layer_health": {
            "configuration": bool(
                configuration_layer.get(
                    "healthy", worker.get("healthy") and isolation.get("healthy")
                )
            ),
            "service_schedule": bool(
                service_schedule_layer.get(
                    "healthy", service.get("healthy") and scheduled.get("healthy")
                )
            ),
            "artifacts": bool(
                (canonical_layers.get("artifacts") or artifacts).get("healthy")
            ),
        },
        "timeline_code_map_neural_link": configuration_layer.get(
            "timeline_code_map_neural_link"
        )
        or {},
        "service": {
            "healthy": bool(service.get("healthy")),
            "missing": service.get("missing_services") or [],
            "services": [
                {
                    "name": row.get("name") or row.get("id"),
                    "state": row.get("state"),
                    "in_sync": bool(row.get("in_sync")),
                }
                for row in service.get("services") or []
            ],
            "error": service.get("error"),
        },
        "artifacts": {
            "healthy": bool(artifacts.get("healthy")),
            "healthy_count": int(artifacts.get("healthy_count") or 0),
            "total": int(artifacts.get("total") or 0),
            "market": artifacts.get("market") or {},
            "checks": [
                {
                    "name": row.get("name"),
                    "healthy": bool(row.get("healthy")),
                    "status": row.get("status"),
                    "lifecycle": row.get("lifecycle"),
                    "summary": row.get("summary"),
                    "group": row.get("group"),
                    "evidence": row.get("evidence") or {},
                }
                for row in artifacts.get("checks") or []
            ],
            "error": artifacts.get("error"),
        },
        "worker": {
            "healthy": bool(worker.get("healthy")),
            "role_shells": worker.get("role_shells") or [],
            "executors": worker.get("executors") or [],
            "tasks": worker.get("tasks") or {},
            "receipts": worker.get("receipts") or {},
        },
        "scheduled": {
            "healthy": bool(scheduled.get("healthy")),
            "counts": scheduled.get("counts") or {},
            "jobs": [
                {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "state": row.get("state"),
                    "enabled": bool(row.get("enabled", True)),
                    "no_agent": bool(row.get("no_agent")),
                    "next_run_at": row.get("next_run_at"),
                    "last_run_at": row.get("last_run_at"),
                    "last_status": row.get("last_status"),
                }
                for row in scheduled.get("jobs") or []
            ],
            "expected_paused": scheduled.get("expected_paused") or [],
            "missing_expected_pause": scheduled.get("missing_expected_pause") or [],
            "unexpected_paused": scheduled.get("unexpected_paused") or [],
            "required_cron": scheduled.get("required_cron") or [],
            "missing_required_cron": scheduled.get("missing_required_cron") or [],
            "failed_enabled_cron": scheduled.get("failed_enabled_cron") or [],
            "observed_failed_enabled_cron": scheduled.get(
                "observed_failed_enabled_cron"
            ) or [],
            "acknowledged_failed_cron": scheduled.get(
                "acknowledged_failed_cron"
            ) or [],
            "failure_acknowledgement_error": scheduled.get(
                "failure_acknowledgement_error"
            ),
            "error": scheduled.get("error"),
        },
        "isolation": {
            "healthy": bool(isolation.get("healthy")),
            "enabled_root_mcp": isolation.get("enabled_root_mcp") or [],
            "no_mcp_platforms": isolation.get("no_mcp_platforms") or [],
        },
    }


def _status_state_icon(state: str) -> str:
    return {
        "OK": "✅",
        "COMPLETE": "✅",
        "CRITICAL": "❌",
        "FAILED": "❌",
    }.get(state, "⚠️")


def _status_operator_text(status: dict[str, Any]) -> str:
    """Render only the three canonical public heartbeat layers."""
    layer_health = status.get("layer_health") or {}
    worker = status.get("worker") or {}
    shells = worker.get("role_shells") or []
    executors = [
        row for row in (worker.get("executors") or []) if row.get("enabled", True)
    ]
    receipts = worker.get("receipts") or {}
    isolation = status.get("isolation") or {}
    timeline = status.get("timeline_code_map_neural_link") or {}
    integrity = timeline.get("integrity") or {}
    code_map = timeline.get("code_map") or {}
    neural = timeline.get("neural_link") or {}
    snapshot = status.get("snapshot") or {}

    services = (status.get("service") or {}).get("services") or []
    scheduled = status.get("scheduled") or {}
    jobs = scheduled.get("jobs") or []
    counts = scheduled.get("counts") or {}
    active = int(counts.get("active") or sum(1 for row in jobs if row.get("enabled")))
    paused = int(counts.get("paused") or (len(jobs) - active))
    failures = scheduled.get("failed_enabled_cron") or []

    artifacts = status.get("artifacts") or {}
    checks = artifacts.get("checks") or []
    headline = "Hermes 정상" if status.get("healthy") and not snapshot.get("stale") else "Hermes 주의"
    lines = [
        headline,
        f"1층 구성 상태 {_status_state_icon('OK' if layer_health.get('configuration') else 'FAILED')}",
        f"  역할 셸: {sum(1 for row in shells if row.get('healthy'))}/{len(shells)}",
        f"  실행기: {sum(1 for row in executors if row.get('healthy'))}/{len(executors)}",
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
        lines.append("  Timeline/Code Map/NeuralLink: 미구성 또는 구형 스냅샷")
    if snapshot.get("stale"):
        age_minutes = max(0, int(float(snapshot.get("age_seconds") or 0) // 60))
        lines.append(f"  상태 스냅샷: {age_minutes}분 전 / 상세 점검 필요")
    else:
        lines.append("  상태 스냅샷: 최신")

    lines.extend(
        [
            f"2층 서비스·스케줄 {_status_state_icon('OK' if layer_health.get('service_schedule') else 'FAILED')}",
            (
                f"  서비스: {sum(1 for row in services if row.get('in_sync'))}/{len(services)}"
                if services
                else "  서비스: 등록 없음"
            ),
            (
                f"  스케줄: 전체 {int(counts.get('total') or len(jobs))}"
                f" / 활성 {active} / 일시정지 {paused} / 실패 {len(failures)}"
            ),
        ]
    )
    if failures:
        lines.append("  최근 실패: " + ", ".join(str(value) for value in failures))

    lines.extend(
        [
            f"3층 산출물 {_status_state_icon('OK' if layer_health.get('artifacts') else 'FAILED')}",
            f"  점검: {sum(1 for row in checks if row.get('healthy'))}/{len(checks)}",
        ]
    )
    if not checks:
        lines.append(f"  등록된 산출물 점검 없음 ({artifacts.get('status') or 'not_configured'})")
    for row in checks:
        name = str(row.get("name") or "unnamed")
        state = str(row.get("status") or ("ok" if row.get("healthy") else "failed"))
        lines.append(
            f"  {_status_state_icon('OK' if row.get('healthy') else 'FAILED')} {name}: {state.upper()}"
        )
    return "\n".join(lines)


def _automation_operator_text(scheduled: dict[str, Any]) -> str:
    jobs = scheduled.get("jobs") or []
    active = sum(1 for row in jobs if row.get("enabled"))
    paused_rows = [row for row in jobs if not row.get("enabled")]
    failures = scheduled.get("failed_enabled_cron") or []
    lines = [
        f"자동화 {len(jobs)}개",
        f"활성 {active} · 일시정지 {len(paused_rows)} · 실패 {len(failures)}",
    ]
    if paused_rows:
        lines.append(
            "일시정지: "
            + ", ".join(str(row.get("name") or row.get("id")) for row in paused_rows)
        )
    if failures:
        lines.append(
            "확인 필요: "
            + ", ".join(
                str(row.get("name") or row.get("id") or row)
                if isinstance(row, dict)
                else str(row)
                for row in failures
            )
        )
    return "\n".join(lines)


def _compact_runtime(runtime: Any) -> dict[str, Any]:
    runtime = runtime if isinstance(runtime, dict) else {}
    return {
        key: runtime.get(key)
        for key in (
            "backend",
            "backend_label",
            "provider",
            "model",
            "reasoning_effort",
            "display_label",
        )
    }


def _reasoning_abbreviation(value: Any) -> str:
    return {
        "low": "L",
        "medium": "M",
        "high": "H",
        "xhigh": "XH",
        "max": "MAX",
        "ultra": "U",
    }.get(str(value or "").strip().lower(), "")


def _mobile_runtime_label(runtime: Any, *, worker_name: Any = None) -> str:
    """Return a short engine/model label that fits one Telegram phone line."""
    runtime = runtime if isinstance(runtime, dict) else {}
    worker = str(worker_name or "").strip().lower()
    provider = str(runtime.get("provider") or "").strip().lower()
    backend = str(runtime.get("backend") or "").strip().lower()
    model = str(runtime.get("model") or "").strip()
    model_lower = model.lower()

    if worker == "grok-build":
        return "Grok"
    if worker == "claude-qwen":
        return "Claude-Qwen"
    if (
        provider == "openai-codex"
        or backend == "codex_app_server"
        or model_lower.startswith("gpt-")
    ):
        brand = "Codex"
        short_model = model[4:] if model_lower.startswith("gpt-") else model
    elif "opencode" in provider or "opencode" in backend or worker == "opencode":
        brand = "OpenCode"
        short_model = model.split("/", 1)[-1]
        if short_model.lower() == "hy3-free":
            short_model = "HY3"
    else:
        brand = ""
        short_model = model

    if not short_model or short_model.lower() == "unknown":
        return brand or "미등록"
    reasoning = _reasoning_abbreviation(runtime.get("reasoning_effort"))
    label = " ".join(part for part in (brand, short_model) if part)
    return f"{label}({reasoning})" if reasoning else label


def _abnormal_state_suffix(state: Any) -> str:
    return {
        "주의": " [주의]",
        "꺼짐": " [꺼짐]",
        "사용 불가": " [불가]",
    }.get(str(state or ""), "")


def _worker_state(
    *,
    binding_enabled: bool,
    worker_enabled: bool,
    health_state: str,
    current: bool,
) -> str:
    """Collapse enabled + health into one unambiguous operator state."""
    health = str(health_state or "unknown").strip().lower()
    if not binding_enabled:
        return "꺼짐"
    if not worker_enabled:
        return "사용 불가" if health in {"unhealthy", "missing"} else "꺼짐"
    if health == "healthy":
        return "사용 중" if current else "대기"
    if health == "degraded":
        return "주의"
    return "사용 불가"


def _compact_role_override(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    return {
        "scope": value.get("scope_type"),
        "target": value.get("scope_key"),
        "worker_id": value.get("executor_id"),
        "mode": value.get("mode"),
    }


def _compact_adapter_view(view: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic operator view for conversational adapter status.

    The complete registry remains available to web/CLI callers through
    ``adapter_registry_view``.  The controller gets one stable topology:
    Hermes controller, seven role adapters, each role's current execution
    worker, and a grouped switch-candidate pool.  Raw history, repeated runtime
    blobs, epoch timestamps, and contradictory enabled/healthy glyphs are
    intentionally excluded.
    """
    executors = {
        row.get("executor_id"): row
        for row in view.get("executors") or []
        if row.get("executor_id")
    }

    controller_source = view.get("controller") or {}
    controller_runtime = _compact_runtime(controller_source.get("runtime"))
    controller_id = controller_source.get("effective_controller_adapter_id")
    controller_candidates = {
        row.get("controller_adapter_id"): row
        for row in controller_source.get("alternatives") or []
        if row.get("controller_adapter_id")
    }
    selected_controller = controller_candidates.get(controller_id) or {}
    if selected_controller:
        controller_state = _worker_state(
            binding_enabled=True,
            worker_enabled=bool(selected_controller.get("enabled")),
            health_state=str(selected_controller.get("health_state") or "unknown"),
            current=True,
        )
    else:
        controller_state = "사용 중"
    fallback_id = selected_controller.get("fallback_adapter_id")
    fallback_row = controller_candidates.get(fallback_id) or {}
    controller_override = controller_source.get("active_override") or None
    controller = {
        "adapter": "hermes",
        "controller_adapter_id": controller_id,
        "runtime": controller_runtime,
        "state": controller_state,
        "override": (
            {
                "scope": controller_override.get("scope_type"),
                "mode": controller_override.get("mode"),
            }
            if controller_override
            else None
        ),
        "fallback": (
            {
                "controller_adapter_id": fallback_id,
                "runtime": _compact_runtime(fallback_row.get("runtime")),
                "state": _worker_state(
                    binding_enabled=True,
                    worker_enabled=bool(fallback_row.get("enabled")),
                    health_state=str(fallback_row.get("health_state") or "unknown"),
                    current=False,
                ),
            }
            if fallback_row
            else None
        ),
    }

    role_slots = {
        str(slot.get("slot_key") or "").strip(): slot
        for slot in view.get("control_slots") or []
        if slot.get("slot_type") == "role_shell"
    }
    role_adapters: list[dict[str, Any]] = []
    candidate_workers: dict[str, dict[str, Any]] = {}
    for role_key in _ROLE_ADAPTER_LABELS:
        slot = role_slots.get(role_key) or {}
        current_id = slot.get("executor_id")
        alternatives = slot.get("alternatives") or []
        current_binding = next(
            (row for row in alternatives if row.get("executor_id") == current_id),
            {},
        )
        current_executor = executors.get(current_id) or {}
        current_state = _worker_state(
            binding_enabled=bool(current_binding.get("enabled", True)),
            worker_enabled=bool(current_executor.get("enabled")),
            health_state=str(current_executor.get("health_state") or "missing"),
            current=True,
        )
        route_health = slot.get("route_health") or {}
        if route_health and not route_health.get("healthy"):
            current_state = "사용 불가"
        current_worker = {
            "worker_id": current_id,
            "worker_name": current_executor.get("name")
            or current_binding.get("executor_name")
            or "미배정",
            "runtime": _compact_runtime(current_executor.get("runtime")),
            "state": current_state,
            "route_health": route_health,
        }
        role_adapters.append(
            {
                "adapter": role_key,
                "label": _ROLE_ADAPTER_LABELS.get(role_key, role_key),
                "worker": current_worker,
                "selection": str(slot.get("selection_source") or "unbound"),
                "override": _compact_role_override(slot.get("active_override")),
            }
        )

        for binding in alternatives:
            worker_id = binding.get("executor_id")
            if not worker_id or worker_id == current_id:
                continue
            executor = executors.get(worker_id) or {}
            state = _worker_state(
                binding_enabled=bool(binding.get("enabled")),
                worker_enabled=bool(executor.get("enabled")),
                health_state=str(executor.get("health_state") or "missing"),
                current=False,
            )
            candidate = candidate_workers.setdefault(
                worker_id,
                {
                    "worker_id": worker_id,
                    "worker_name": executor.get("name")
                    or binding.get("executor_name"),
                    "runtime": _compact_runtime(executor.get("runtime")),
                    "state": state,
                    "adapters": [],
                },
            )
            candidate["adapters"].append(role_key)
            if _WORKER_STATE_ORDER[state] > _WORKER_STATE_ORDER[candidate["state"]]:
                candidate["state"] = state

    candidate_rows = sorted(
        candidate_workers.values(),
        key=lambda row: (
            _WORKER_STATE_ORDER.get(str(row.get("state")), 99),
            str(row.get("worker_name") or row.get("worker_id") or ""),
        ),
    )

    controller_label = _mobile_runtime_label(controller_runtime)
    override_label = ""
    if controller["override"]:
        override_mode = _OVERRIDE_MODE_LABELS.get(
            str(controller["override"]["mode"]),
            str(controller["override"]["mode"]),
        )
        override_label = f" · {override_mode}"
    fallback_label = "없음"
    if controller["fallback"]:
        fallback_label = _mobile_runtime_label(
            controller["fallback"]["runtime"]
        )
    lines = [
        f"Hermes: {controller_label}{override_label}"
        f"{_abnormal_state_suffix(controller_state)}",
    ]
    for index, row in enumerate(role_adapters):
        worker = row["worker"]
        worker_label = _mobile_runtime_label(
            worker["runtime"], worker_name=worker.get("worker_name")
        )
        override_suffix = ""
        if row["override"]:
            role_override_mode = _OVERRIDE_MODE_LABELS.get(
                str(row["override"].get("mode")),
                str(row["override"].get("mode")),
            )
            override_suffix = f" [{role_override_mode}]"
        branch = "└" if index == len(role_adapters) - 1 else "├"
        lines.append(
            f"{branch} {_MOBILE_ROLE_LABELS[row['adapter']]}: {worker_label}"
            f"{_abnormal_state_suffix(worker['state'])}{override_suffix}"
        )

    lines.extend(
        [
            "",
            "컨트롤러 폴백",
            "  현재 모델 실패",
            f"  → {fallback_label}",
            "  변경: Hermes 판단 모델만",
            f"  유지: 역할 {_ROLE_ADAPTER_COUNT}개 연결",
            "",
            "역할·도구",
        ]
    )
    for row in role_adapters:
        role_key = row["adapter"]
        detail = _ROLE_OPERATOR_DETAILS[role_key]
        lines.extend(
            [
                _MOBILE_ROLE_LABELS[role_key],
                f"  범위: {detail['scope'][0]}",
            ]
        )
        lines.extend(f"       {item}" for item in detail["scope"][1:])
        lines.append(f"  툴: {detail['tools'][0]}")
        lines.extend(f"       {item}" for item in detail["tools"][1:])

    lines.extend(
        [
            "",
            "후보 상태(상세 조회 시)",
            "  추가 가능: 정상·겸임 가능",
            "  비활성: 운영자가 제외",
            "  불가: 헬스 실패로 배정 금지",
        ]
    )

    return {
        "schema": "hermes.supervisor.adapter_operator_view.v1",
        "view": "operator_compact",
        "operator_text": "\n".join(lines),
        "controller": controller,
        "role_adapter_count": len(role_adapters),
        "role_adapters": role_adapters,
        "candidate_workers": candidate_rows,
        "role_overrides": [
            row["override"] for row in role_adapters if row["override"]
        ],
        "role_details": _ROLE_OPERATOR_DETAILS,
    }


def _status_snapshot_unavailable(exc: Exception) -> str:
    return json.dumps(
        {
            "schema": "hermes.supervisor.status.v1",
            "healthy": False,
            "snapshot": {
                "source": "heartbeat_snapshot",
                "available": False,
                "error": str(exc),
            },
            "operator_text": (
                "Hermes 상태 확인 필요\n"
                "저장된 상태 스냅샷 없음\n"
                "상세 점검을 요청해 주세요"
            ),
        },
        ensure_ascii=False,
    )


def _handle_status(args: dict[str, Any], **_kwargs) -> str:
    mode = str(args.get("mode") or "snapshot").strip().lower()
    if mode not in {"snapshot", "deep"}:
        return tool_error("supervisor_status: mode must be snapshot or deep")
    try:
        from hermes_cli.supervisor_cli import (
            build_heartbeat_snapshot,
            load_heartbeat_snapshot,
            save_heartbeat_snapshot,
        )

        if mode == "deep":
            raw_snapshot = build_heartbeat_snapshot()
            target = save_heartbeat_snapshot(raw_snapshot)
            observed_at = target.stat().st_mtime
        else:
            raw_snapshot, observed_at = load_heartbeat_snapshot()
        age_seconds = max(0.0, time.time() - observed_at)
        status = _compact_status(raw_snapshot)
        status["snapshot"] = {
            "source": "deep_audit" if mode == "deep" else "heartbeat_snapshot",
            "available": True,
            "age_seconds": round(age_seconds, 3),
            "stale": age_seconds > STATUS_SNAPSHOT_MAX_AGE_SECONDS,
        }
        if status["snapshot"]["stale"]:
            status["healthy"] = False
        status["operator_text"] = _status_operator_text(status)
        if mode == "snapshot":
            status = {
                "schema": "hermes.supervisor.status_screen.v1",
                "healthy": status["healthy"],
                "snapshot": status["snapshot"],
                "operator_text": status["operator_text"],
            }
        return json.dumps(status, ensure_ascii=False)
    except Exception as exc:
        return _status_snapshot_unavailable(exc)


def _handle_automation(args: dict[str, Any], **_kwargs) -> str:
    """Mutate only supervisor-owned acknowledgement state."""
    action = str(args.get("action") or "list_failures").strip().lower()
    jobs = args.get("jobs") or []
    if isinstance(jobs, str):
        jobs = [jobs]
    if not isinstance(jobs, list):
        return tool_error("supervisor_automation: jobs must be a string array")
    job_names = [str(name).strip() for name in jobs if str(name).strip()]
    try:
        from hermes_cli.supervisor_cli import (
            acknowledge_cron_failures,
            build_heartbeat_snapshot,
            clear_cron_failure_acknowledgements,
        )

        if action == "acknowledge_failures":
            result = acknowledge_cron_failures(
                job_names,
                acknowledged_by="hermes-conversation",
            )
            result["status"] = _compact_status(build_heartbeat_snapshot())["scheduled"]
        elif action == "clear_acknowledgements":
            result = clear_cron_failure_acknowledgements(job_names)
            result["status"] = _compact_status(build_heartbeat_snapshot())["scheduled"]
        elif action == "list_failures":
            result = _compact_status(build_heartbeat_snapshot())["scheduled"]
        else:
            return tool_error(
                "supervisor_automation: action must be list_failures, "
                "acknowledge_failures, or clear_acknowledgements"
            )
        scheduled_status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(scheduled_status, dict):
            scheduled_status = result
        if isinstance(result, dict):
            result["operator_text"] = _automation_operator_text(scheduled_status)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"supervisor_automation: {exc}")


def _handle_roles(_args: dict[str, Any], **_kwargs) -> str:
    try:
        with kb.connect_closing() as conn:
            registry.ensure_schema(conn)
            adapter_view = registry.adapter_registry_view(conn, history_limit=1)
            ownership_by_shell = {
                row["role_shell_id"]: row for row in adapter_view["shells"]
            }
            health = {
                row["role_shell_id"]: row
                for row in registry.build_shell_health(conn)
            }
            rows = []
            for shell in registry.list_shells(conn, active_only=True):
                state = health.get(shell.id) or {}
                rows.append(
                    {
                        "shell_key": shell.shell_key,
                        "role_shell_id": shell.id,
                        "version": shell.version,
                        "name": shell.name,
                        "description": shell.description,
                        "required_capabilities": shell.required_capabilities,
                        "routable_binding_count": state.get(
                            "routable_binding_count", 0
                        ),
                        "healthy": bool(state.get("healthy")),
                        "adapter_ownership": ownership_by_shell.get(shell.id),
                    }
                )
        return json.dumps({"roles": rows}, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"supervisor_roles: {exc}")


def _subscribe_delegated_task(
    conn: Any,
    *,
    task_id: str,
    route: Any,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Subscribe a controller-created card to its trusted gateway return path."""
    resolved_route = route if isinstance(route, dict) else None

    # OpenAI-compatible supervisor controllers execute through Hermes' normal
    # registry path, which does not add the Codex bridge's explicit
    # ``notification_route`` kwarg.  Recover the same trusted route directly
    # from the gateway's task-local ContextVars so controller choice never
    # changes completion-delivery semantics.
    if not resolved_route:
        try:
            from gateway.session_context import (
                async_delivery_supported,
                get_session_env,
            )

            platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
            chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
            if async_delivery_supported() and platform and chat_id:
                resolved_route = {
                    "platform": platform,
                    "chat_id": chat_id,
                    "chat_type": get_session_env(
                        "HERMES_SESSION_CHAT_TYPE", ""
                    ).strip(),
                    "thread_id": get_session_env(
                        "HERMES_SESSION_THREAD_ID", ""
                    ).strip(),
                    "user_id": get_session_env(
                        "HERMES_SESSION_USER_ID", ""
                    ).strip(),
                    "notifier_profile": get_session_env(
                        "HERMES_SESSION_PROFILE", ""
                    ).strip(),
                }
        except Exception:
            resolved_route = None

    # A controller session may outlive the task-local ContextVars (or a
    # provider-specific bridge may invoke the handler on another thread).
    # ``session_id`` is injected by Hermes, not supplied by the model, so its
    # persisted gateway peer is a safe final recovery source.
    if not resolved_route and session_id:
        state_path = get_hermes_home() / "state.db"
        if state_path.is_file():
            try:
                from hermes_state import SessionDB

                session_db = SessionDB(db_path=state_path, read_only=True)
                try:
                    session = session_db.get_session(session_id)
                finally:
                    session_db.close()
                if session:
                    platform = str(session.get("source") or "").strip().lower()
                    chat_id = str(session.get("chat_id") or "").strip()
                    if platform == "tui":
                        chat_id = str(session.get("session_key") or "").strip()
                    non_routable = {
                        "", "api", "api_server", "cli", "cron", "local",
                        "tool", "unknown",
                    }
                    if platform not in non_routable and chat_id:
                        resolved_route = {
                            "platform": platform,
                            "chat_id": chat_id,
                            "chat_type": str(
                                session.get("chat_type") or ""
                            ).strip(),
                            "thread_id": str(
                                session.get("thread_id") or ""
                            ).strip(),
                            "user_id": str(
                                session.get("user_id") or ""
                            ).strip(),
                            "notifier_profile": str(
                                os.environ.get("HERMES_PROFILE") or ""
                            ).strip(),
                        }
            except Exception:
                resolved_route = None

    if not isinstance(resolved_route, dict):
        return {"subscribed": False, "reason": "no_routable_session"}
    platform = str(resolved_route.get("platform") or "").strip().lower()
    chat_id = str(resolved_route.get("chat_id") or "").strip()
    if not platform or not chat_id:
        return {"subscribed": False, "reason": "no_routable_session"}
    try:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=(
                str(resolved_route.get("thread_id") or "").strip() or None
            ),
            user_id=(str(resolved_route.get("user_id") or "").strip() or None),
            notifier_profile=(
                str(resolved_route.get("notifier_profile") or "").strip() or None
            ),
            chat_type=(
                str(resolved_route.get("chat_type") or "").strip() or None
            ),
        )
        return {
            "subscribed": True,
            "platform": platform,
            "threaded": bool(
                str(resolved_route.get("thread_id") or "").strip()
            ),
        }
    except Exception as exc:
        return {
            "subscribed": False,
            "reason": "subscription_failed",
            "error": str(exc),
        }


def _record_controller_provenance(
    conn,
    *,
    task_id: str,
    session_id: Optional[str],
) -> dict[str, Any]:
    """Stamp the controller used to create/reopen a card into its history."""
    selection = registry.resolve_controller_selection(conn, session_id=session_id)
    if selection is None:
        runtime = registry.controller_runtime_descriptor()
        override_payload = None
        controller_adapter_id = next(
            (
                item.id
                for item in registry.list_controller_adapters(conn)
                if item.provider == runtime.get("provider")
                and item.model == runtime.get("model")
            ),
            None,
        )
        selection_source = "controller_config"
    else:
        override, controller_adapter = selection
        runtime = registry.controller_adapter_runtime_descriptor(controller_adapter)
        override_payload = registry._controller_override_dict(override)
        controller_adapter_id = controller_adapter.id
        selection_source = f"{override.scope_type}_override"
    payload = {
        "session_id": session_id,
        "controller_adapter_id": controller_adapter_id,
        "selection_source": selection_source,
        "runtime": {
            key: runtime.get(key)
            for key in (
                "backend",
                "backend_label",
                "provider",
                "model",
                "reasoning_effort",
                "api_mode",
                "endpoint",
                "display_label",
            )
        },
        "override": override_payload,
    }
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "supervisor_provenance", payload)
    return payload


def _handle_delegate(args: dict[str, Any], **_kwargs) -> str:
    try:
        shell_key = str(args.get("shell_key") or "").strip()
        title = str(args.get("title") or "").strip()
        if not shell_key:
            return tool_error("supervisor_delegate: shell_key is required")
        if not title:
            return tool_error("supervisor_delegate: title is required")
        workspace_kind = str(args.get("workspace_kind") or "scratch").strip()
        workspace_path = args.get("workspace_path")
        raw_source_task_ids = args.get("source_task_ids") or []
        if not isinstance(raw_source_task_ids, list):
            return tool_error(
                "supervisor_delegate: source_task_ids must be an array"
            )
        source_task_ids: list[str] = []
        for raw_source_id in raw_source_task_ids:
            source_id = str(raw_source_id or "").strip()
            if source_id and source_id not in source_task_ids:
                source_task_ids.append(source_id)
        if workspace_kind not in {"scratch", "dir", "worktree"}:
            return tool_error(
                "supervisor_delegate: workspace_kind must be scratch, dir, or worktree"
            )
        if workspace_kind in {"dir", "worktree"} and not workspace_path:
            return tool_error(
                f"supervisor_delegate: workspace_path is required for {workspace_kind}"
            )
        session_id = str(_kwargs.get("session_id") or "").strip() or None
        with kb.connect_closing() as conn:
            registry.ensure_schema(conn)
            if source_task_ids:
                existing_source_ids = {
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM tasks WHERE id IN ("
                        + ",".join("?" for _ in source_task_ids)
                        + ")",
                        source_task_ids,
                    ).fetchall()
                }
                missing_source_ids = [
                    source_id
                    for source_id in source_task_ids
                    if source_id not in existing_source_ids
                ]
                if missing_source_ids:
                    return tool_error(
                        "supervisor_delegate: unknown source task(s): "
                        + ", ".join(missing_source_ids)
                    )
                source_task_ids = registry.canonical_task_recovery_sources(
                    conn, source_task_ids
                )
            shell = registry.get_shell(conn, shell_key=shell_key)
            if shell is None:
                return tool_error(
                    f"supervisor_delegate: no active role shell for {shell_key!r}"
                )
            existing_recovery = None
            if source_task_ids:
                existing_recovery = registry.find_existing_recovery_task(
                    conn,
                    source_task_ids=source_task_ids,
                    role_shell_id=shell.id,
                    session_id=session_id,
                )
                if existing_recovery and existing_recovery["status"] != "blocked":
                    task_id = str(existing_recovery["id"])
                    controller_provenance = _record_controller_provenance(
                        conn,
                        task_id=task_id,
                        session_id=session_id,
                    )
                    notification = _subscribe_delegated_task(
                        conn,
                        task_id=task_id,
                        route=_kwargs.get("notification_route"),
                        session_id=session_id,
                    )
                    with kb.write_txn(conn):
                        kb._append_event(
                            conn,
                            task_id,
                            "result_recovery_reused",
                            {
                                "source_task_ids": source_task_ids,
                                "status": existing_recovery["status"],
                                "created_by": "hermes-conversation",
                            },
                        )
                    detail = registry.inspect_task_adapter(conn, task_id)
                    return json.dumps(
                        {
                            "created": False,
                            "reused": True,
                            "reopened": False,
                            "task_id": task_id,
                            "shell_key": shell.shell_key,
                            "role_shell_id": shell.id,
                            "status": existing_recovery["status"],
                            "requested_executor_id": None,
                            "adapter_override": detail.get("effective_override"),
                            "notification": notification,
                            "source_task_ids": source_task_ids,
                            "lineage_mode": "canonical_non_blocking_result_recovery",
                            "controller_provenance": controller_provenance,
                        },
                        ensure_ascii=False,
                    )
            requested_executor = str(args.get("executor_id") or "").strip() or None
            requested_work_kind = str(
                args.get("work_kind") or "normal"
            ).strip().lower()
            if requested_work_kind not in {"normal", "repair", "tooling"}:
                return tool_error(
                    "supervisor_delegate: work_kind must be normal, repair, or tooling"
                )
            if (
                requested_work_kind == "tooling"
                and shell.shell_key != "tool-management"
            ):
                return tool_error(
                    "supervisor_delegate: tooling work must use the "
                    "tool-management role shell"
                )
            repair_work = _repair_work_requested(args, shell.shell_key)
            if repair_work:
                if shell.shell_key not in {"code", "operations"}:
                    return tool_error(
                        "supervisor_delegate: repair work must use the code or "
                        "operations role shell"
                    )
                repair_executor = _repair_executor_id()
                if requested_executor and requested_executor != repair_executor:
                    return tool_error(
                        "supervisor_delegate: repair work is pinned to "
                        f"{repair_executor}; requested {requested_executor}"
                    )
                requested_executor = repair_executor
            recovery_required_capabilities: set[str] = set()
            if source_task_ids:
                source_shell_rows = conn.execute(
                    "SELECT DISTINCT rs.required_capabilities "
                    "FROM tasks t JOIN role_shells rs ON rs.id=t.role_shell_id "
                    "WHERE t.id IN ("
                    + ",".join("?" for _ in source_task_ids)
                    + ")",
                    source_task_ids,
                ).fetchall()
                for source_shell_row in source_shell_rows:
                    try:
                        recovery_required_capabilities.update(
                            str(item).strip()
                            for item in json.loads(
                                source_shell_row["required_capabilities"] or "[]"
                            )
                            if str(item).strip()
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        return tool_error(
                            "supervisor_delegate: invalid source role capability contract"
                        )
            # Fail before card creation when the requested/default route cannot
            # ever satisfy the immutable shell. Runtime health/capacity remains
            # an atomic claim-time gate.
            if requested_executor:
                executor = registry.resolve_executor(conn, requested_executor)
                if executor is None:
                    return tool_error(
                        f"supervisor_delegate: unknown executor {requested_executor!r}"
                    )
                registry._validate_override_executor(
                    conn, executor=executor, shells=[shell]
                )
                binding = next(
                    (
                        item
                        for item in registry.list_bindings(
                            conn, shell_id=shell.id
                        )
                        if item.executor_id == executor.id
                    ),
                    None,
                )
                effective = set(shell.allowed_capabilities) & set(
                    executor.capabilities
                )
                if binding is not None and binding.capability_cap:
                    effective &= set(binding.capability_cap)
                missing_recovery = sorted(
                    recovery_required_capabilities - effective
                )
                if missing_recovery:
                    return tool_error(
                        f"supervisor_delegate: executor {executor.id} lacks "
                        "recovery capabilities: " + ", ".join(missing_recovery)
                    )
            else:
                selected = registry.select_binding(
                    conn,
                    shell.id,
                    additional_required_capabilities=(
                        recovery_required_capabilities
                    ),
                )
                if recovery_required_capabilities:
                    requested_executor = selected.executor.id
            if existing_recovery:
                task_id = str(existing_recovery["id"])
                if not requested_executor:
                    requested_executor = registry.select_binding(
                        conn,
                        shell.id,
                        additional_required_capabilities=(
                            recovery_required_capabilities
                        ),
                    ).executor.id
                override = registry.create_adapter_override(
                    conn,
                    target=task_id,
                    scope_type="task",
                    executor_value=requested_executor,
                    mode="once",
                    reason=(
                        str(args.get("adapter_reason") or "").strip()
                        or "reopen canonical result recovery with capable executor"
                    ),
                    created_by="hermes-conversation",
                )
                if not kb.unblock_task(conn, task_id):
                    return tool_error(
                        f"supervisor_delegate: recovery task {task_id} could not be reopened"
                    )
                controller_provenance = _record_controller_provenance(
                    conn,
                    task_id=task_id,
                    session_id=session_id,
                )
                notification = _subscribe_delegated_task(
                    conn,
                    task_id=task_id,
                    route=_kwargs.get("notification_route"),
                    session_id=session_id,
                )
                with kb.write_txn(conn):
                    kb._append_event(
                        conn,
                        task_id,
                        "result_recovery_reopened",
                        {
                            "source_task_ids": source_task_ids,
                            "executor_id": requested_executor,
                            "override_id": override.id,
                            "created_by": "hermes-conversation",
                        },
                    )
                detail = registry.inspect_task_adapter(conn, task_id)
                return json.dumps(
                    {
                        "created": False,
                        "reused": True,
                        "reopened": True,
                        "task_id": task_id,
                        "shell_key": shell.shell_key,
                        "role_shell_id": shell.id,
                        "status": "ready",
                        "requested_executor_id": requested_executor,
                        "adapter_override": detail.get("effective_override"),
                        "notification": notification,
                        "source_task_ids": source_task_ids,
                        "recovery_required_capabilities": sorted(
                            recovery_required_capabilities
                        ),
                        "lineage_mode": "canonical_non_blocking_result_recovery",
                        "controller_provenance": controller_provenance,
                    },
                    ensure_ascii=False,
                )
            task_id = kb.create_task(
                conn,
                title=title,
                body=(str(args.get("body") or "").strip() or None),
                role_shell_id=shell.id,
                workspace_kind=workspace_kind,
                workspace_path=(str(workspace_path).strip() if workspace_path else None),
                branch_name=(str(args.get("branch_name") or "").strip() or None),
                priority=int(args.get("priority") or 0),
                session_id=session_id,
                adapter_executor_id=requested_executor,
                # Task-scoped once means the whole card lifecycle.  The
                # registry keeps it active across crash/timeout retries and
                # consumes it only when the card becomes done/archived.
                adapter_override_mode="once",
                adapter_reason=(
                    str(args.get("adapter_reason") or "").strip()
                    or (
                        "repair policy pins the configured remediation executor"
                        if repair_work
                        else (
                            "result recovery requires source capabilities: "
                            + ", ".join(sorted(recovery_required_capabilities))
                            if recovery_required_capabilities
                            else None
                        )
                    )
                ),
                adapter_created_by="hermes-conversation",
            )
            controller_provenance = _record_controller_provenance(
                conn,
                task_id=task_id,
                session_id=session_id,
            )
            notification = _subscribe_delegated_task(
                conn,
                task_id=task_id,
                route=_kwargs.get("notification_route"),
                session_id=session_id,
            )
            recovery_sources = registry.register_task_recovery_sources(
                conn,
                recovery_task_id=task_id,
                source_task_ids=source_task_ids,
                created_by="hermes-conversation",
            )
            detail = registry.inspect_task_adapter(conn, task_id)
        return json.dumps(
            {
                "created": True,
                "task_id": task_id,
                "shell_key": shell.shell_key,
                "role_shell_id": shell.id,
                "status": "ready",
                "executor_selection": (
                    "pinned_configured_repair"
                    if repair_work
                    else "deferred_to_atomic_claim"
                ),
                "requested_executor_id": requested_executor,
                "work_kind": "repair" if repair_work else requested_work_kind,
                "routing_policy": (
                    "configured_repair_executor_required" if repair_work else None
                ),
                "adapter_override": detail.get("effective_override"),
                "notification": notification,
                "source_task_ids": recovery_sources,
                "recovery_required_capabilities": sorted(
                    recovery_required_capabilities
                ),
                "lineage_mode": (
                    "canonical_non_blocking_result_recovery"
                    if recovery_sources
                    else None
                ),
                "controller_provenance": controller_provenance,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return tool_error(f"supervisor_delegate: {exc}")


def _handle_project_cards(args: dict[str, Any], **_kwargs) -> str:
    """Deterministic Project/Card Controller shared by chat and web.

    The root model may translate a user's request into this strict schema, but
    it cannot directly write project/card state. No worker adapter is invoked
    here; adapters are selected only when the resulting card is dispatched.
    """
    action = str(args.get("action") or "list_projects").strip().lower()
    session_id = str(_kwargs.get("session_id") or "").strip() or None
    common = {
        "session_id": session_id,
        "created_by": "hermes-project-card-controller",
    }
    try:
        if action == "list_projects":
            result = project_cards.list_projects(
                include_archived=bool(args.get("include_archived", False))
            )
        elif action == "start_project":
            result = project_cards.start_project(
                name=str(args.get("project_name") or ""),
                slug=(str(args.get("project_slug") or "").strip() or None),
                description=(str(args.get("body") or "").strip() or None),
                primary_path=(str(args.get("primary_path") or "").strip() or None),
                board=str(args.get("board") or kb.DEFAULT_BOARD),
                goal=str(args.get("title") or ""),
                shell_key=str(args.get("shell_key") or ""),
                acceptance_criteria=args.get("acceptance_criteria"),
                input_refs=args.get("input_refs"),
                priority=int(args.get("priority") or 0),
                executor_id=(str(args.get("executor_id") or "").strip() or None),
                **common,
            )
        elif action == "add_project_card":
            result = project_cards.add_project_card(
                str(args.get("project_id") or ""),
                title=str(args.get("title") or ""),
                body=(str(args.get("body") or "").strip() or None),
                shell_key=str(args.get("shell_key") or ""),
                acceptance_criteria=args.get("acceptance_criteria"),
                input_refs=args.get("input_refs"),
                workspace_kind=(
                    str(args.get("workspace_kind") or "").strip() or None
                ),
                workspace_path=(
                    str(args.get("workspace_path") or "").strip() or None
                ),
                priority=int(args.get("priority") or 0),
                executor_id=(str(args.get("executor_id") or "").strip() or None),
                **common,
            )
        elif action == "continue_card":
            result = project_cards.continue_card(
                str(args.get("card_id") or ""),
                title=str(args.get("title") or ""),
                body=(str(args.get("body") or "").strip() or None),
                shell_key=(str(args.get("shell_key") or "").strip() or None),
                acceptance_criteria=args.get("acceptance_criteria"),
                input_refs=args.get("input_refs"),
                workspace_kind=(
                    str(args.get("workspace_kind") or "").strip() or None
                ),
                workspace_path=(
                    str(args.get("workspace_path") or "").strip() or None
                ),
                priority=int(args.get("priority") or 0),
                executor_id=(str(args.get("executor_id") or "").strip() or None),
                **common,
            )
        elif action == "split_card":
            result = project_cards.split_card(
                str(args.get("card_id") or ""),
                cards=args.get("cards") or [],
                **common,
            )
        elif action == "verify_card":
            result = project_cards.verify_card(
                str(args.get("card_id") or ""),
                title=(str(args.get("title") or "").strip() or None),
                body=(str(args.get("body") or "").strip() or None),
                acceptance_criteria=args.get("acceptance_criteria"),
                **common,
            )
        elif action == "recover_card":
            result = project_cards.recover_card(
                str(args.get("card_id") or ""),
                title=(str(args.get("title") or "").strip() or None),
                body=(str(args.get("body") or "").strip() or None),
                shell_key=(str(args.get("shell_key") or "").strip() or None),
                acceptance_criteria=args.get("acceptance_criteria"),
                executor_id=(str(args.get("executor_id") or "").strip() or None),
                **common,
            )
        elif action == "inspect_card":
            result = project_cards.inspect_card(
                str(args.get("card_id") or ""),
                board=(str(args.get("board") or "").strip() or None),
            )
        elif action == "inspect_project":
            result = project_cards.inspect_project(
                str(args.get("project_id") or "")
            )
        elif action == "close_project":
            result = project_cards.close_project(
                str(args.get("project_id") or "")
            )
        elif action == "reopen_project":
            result = project_cards.reopen_project(
                str(args.get("project_id") or "")
            )
        elif action == "locate_card":
            located = project_cards.locate_card(
                str(args.get("card_id") or ""),
                board=(str(args.get("board") or "").strip() or None),
            )
            result = {
                "schema": project_cards.SCHEMA,
                "action": "locate_card",
                "board": located["board"],
                "card": {
                    "id": located["task"].id,
                    "project_id": located["task"].project_id,
                    "root_task_id": located["task"].root_task_id,
                    "status": located["task"].status,
                    "title": located["task"].title,
                },
            }
        else:
            return tool_error(f"supervisor_project: unknown action {action!r}")

        created_ids: list[str] = []
        if isinstance(result.get("card"), dict) and result["card"].get("id"):
            created_ids.append(str(result["card"]["id"]))
        for card in result.get("cards") or []:
            if isinstance(card, dict) and card.get("id"):
                created_ids.append(str(card["id"]))
        if created_ids and _kwargs.get("notification_route"):
            notifications = []
            board = str(result.get("board") or kb.DEFAULT_BOARD)
            with kb.connect_closing(board=board) as conn:
                for task_id in created_ids:
                    notifications.append(
                        _subscribe_delegated_task(
                            conn,
                            task_id=task_id,
                            route=_kwargs.get("notification_route"),
                            session_id=session_id,
                        )
                    )
            result["notifications"] = notifications
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"supervisor_project: {exc}")


def _handle_adapter(args: dict[str, Any], **_kwargs) -> str:
    """One conversational control surface shared by Telegram and CLI root."""
    action = str(args.get("action") or "list").strip().lower()
    session_id = str(_kwargs.get("session_id") or "").strip() or None
    try:
        with kb.connect_closing() as conn:
            registry.ensure_schema(conn)
            if action == "list":
                result = registry.adapter_registry_view(
                    conn,
                    history_limit=int(args.get("limit") or 50),
                    session_id=session_id,
                )
                selected_executor_ids = {
                    str(slot.get("executor_id") or "").strip()
                    for slot in result.get("control_slots") or []
                    if slot.get("slot_type") == "role_shell"
                    and str(slot.get("executor_id") or "").strip()
                }
                now = int(time.time())
                selected_probe_ids = []
                for executor in result.get("executors") or []:
                    executor_id = str(executor.get("executor_id") or "").strip()
                    if (
                        executor_id not in selected_executor_ids
                        or executor.get("adapter_type") != "command"
                        or not executor.get("enabled")
                    ):
                        continue
                    last_heartbeat_at = executor.get("last_heartbeat_at")
                    ttl = int(executor.get("heartbeat_ttl_seconds") or 0)
                    stale = bool(
                        executor.get("heartbeat_required")
                        and (
                            last_heartbeat_at is None
                            or now - int(last_heartbeat_at) > ttl
                        )
                    )
                    if executor.get("health_state") != "healthy" or stale:
                        selected_probe_ids.append(executor_id)
                if selected_probe_ids:
                    registry.refresh_executor_health_probes(
                        conn,
                        executor_ids=selected_probe_ids,
                    )
                    result = registry.adapter_registry_view(
                        conn,
                        history_limit=int(args.get("limit") or 50),
                        session_id=session_id,
                    )
                if str(args.get("view") or "compact").strip().lower() != "full":
                    result = _compact_adapter_view(result)
            elif action == "tools":
                result = build_tool_catalog(conn=conn)
                query = str(args.get("query") or "").strip()
                matches = search_tool_catalog(result, query) if query else []
                if str(args.get("view") or "compact").strip().lower() != "full":
                    result = compact_tool_catalog(result)
                if query:
                    result["query"] = query
                    result["matches"] = matches
            elif action == "recent":
                result = {
                    "recent_tasks": registry.list_recent_adapter_tasks(
                        conn,
                        session_id=session_id,
                        limit=int(args.get("limit") or 10),
                        completed_only=bool(args.get("completed_only", False)),
                        fallback_global=True,
                    )
                }
            elif action == "history":
                result = {
                    "history": registry.list_adapter_events(
                        conn,
                        task_id=(str(args.get("task_id") or "").strip() or None),
                        scope_type=(
                            str(args.get("scope_type") or "").strip() or None
                        ),
                        scope_key=(str(args.get("target") or "").strip() or None),
                        limit=int(args.get("limit") or 100),
                    )
                }
            elif action == "inspect":
                resolution = registry.resolve_adapter_task_reference(
                    conn,
                    str(args.get("task_id") or "").strip(),
                    session_id=session_id,
                )
                result = registry.inspect_task_adapter(conn, resolution["task_id"])
                result["reference_resolution"] = resolution
            elif action == "assign":
                target = str(args.get("target") or "").strip()
                executor_id = str(args.get("executor_id") or "").strip()
                if not target or not executor_id:
                    return tool_error(
                        "supervisor_adapter: assign requires target and executor_id"
                    )
                binding = registry.assign_adapter(
                    conn,
                    shell_value=target,
                    executor_value=executor_id,
                    responsibility=str(args.get("responsibility") or "candidate"),
                    priority=(
                        int(args["priority"])
                        if args.get("priority") is not None
                        else None
                    ),
                    weight=(
                        float(args["weight"])
                        if args.get("weight") is not None
                        else None
                    ),
                    note=(str(args.get("reason") or "").strip() or None),
                    assigned_by="hermes-conversation",
                )
                result = {
                    "assigned": True,
                    "binding_id": binding.id,
                    "shell_id": binding.shell_id,
                    "executor_id": binding.executor_id,
                    "responsibility": binding.responsibility,
                    "priority": binding.priority,
                    "weight": binding.weight,
                }
            elif action == "switch":
                target = str(args.get("target") or "").strip()
                executor_id = str(args.get("executor_id") or "").strip()
                controller_id = str(args.get("controller_id") or "").strip()
                mode = str(args.get("mode") or "").strip().lower()
                if target.lower() in {"hermes", "controller", "supervisor"}:
                    candidate_value = controller_id or executor_id
                    if not candidate_value or not mode:
                        return tool_error(
                            "supervisor_adapter: Hermes switch requires "
                            "controller_id and mode"
                        )
                    candidate = registry.resolve_controller_adapter(
                        conn, candidate_value
                    )
                    if candidate is None:
                        return tool_error(
                            f"supervisor_adapter: unknown controller adapter "
                            f"{candidate_value}"
                        )
                    activation = None
                    if candidate.health_url or not candidate.routable():
                        activation = registry.set_controller_adapter_operational_state(
                            conn,
                            candidate.id,
                            enabled=True,
                            reason=(
                                str(args.get("reason") or "").strip()
                                or "explicit controller switch request"
                            ),
                            changed_by="hermes-conversation",
                        )
                        if not activation.get("enabled"):
                            return tool_error(
                                "supervisor_adapter: controller health gate failed: "
                                + json.dumps(activation, ensure_ascii=False)
                            )
                    override = registry.create_controller_override(
                        conn,
                        controller_adapter_value=candidate.id,
                        mode=mode,
                        session_id=session_id,
                        scope_type=(
                            str(args.get("scope_type") or "").strip() or None
                        ),
                        duration_seconds=(
                            int(args["duration_seconds"])
                            if args.get("duration_seconds") is not None
                            else None
                        ),
                        reason=(str(args.get("reason") or "").strip() or None),
                        created_by="hermes-conversation",
                    )
                    result = {
                        "switched": True,
                        "target": "hermes",
                        "applies_from": "next_turn",
                        "activation": activation,
                        **registry._controller_override_dict(override),
                    }
                elif not target or not executor_id or not mode:
                    return tool_error(
                        "supervisor_adapter: switch requires target, executor_id, and mode"
                    )
                else:
                    override = registry.create_adapter_override(
                        conn,
                        target=target,
                        scope_type=(str(args.get("scope_type") or "").strip() or None),
                        executor_value=executor_id,
                        mode=mode,
                        duration_seconds=(
                            int(args["duration_seconds"])
                            if args.get("duration_seconds") is not None
                            else None
                        ),
                        reason=(str(args.get("reason") or "").strip() or None),
                        created_by="hermes-conversation",
                    )
                    result = {"switched": True, **registry._override_dict(override)}
            elif action == "controller_state":
                controller_id = str(args.get("controller_id") or "").strip()
                if not controller_id or not isinstance(args.get("enabled"), bool):
                    return tool_error(
                        "supervisor_adapter: controller_state requires controller_id "
                        "and boolean enabled"
                    )
                result = registry.set_controller_adapter_operational_state(
                    conn,
                    controller_id,
                    enabled=bool(args["enabled"]),
                    reason=(str(args.get("reason") or "").strip() or None),
                    changed_by="hermes-conversation",
                )
            elif action == "controller_model":
                controller_id = str(args.get("controller_id") or "").strip()
                model = str(args.get("model") or "").strip()
                if not controller_id or not model:
                    return tool_error(
                        "supervisor_adapter: controller_model requires "
                        "controller_id and model"
                    )
                result = registry.set_controller_adapter_model(
                    conn,
                    controller_id,
                    model=model,
                    reason=(str(args.get("reason") or "").strip() or None),
                    changed_by="hermes-conversation",
                )
            elif action == "executor_state":
                executor_id = str(args.get("executor_id") or "").strip()
                if not executor_id or not isinstance(args.get("enabled"), bool):
                    return tool_error(
                        "supervisor_adapter: executor_state requires executor_id "
                        "and boolean enabled"
                    )
                result = registry.set_executor_operational_state(
                    conn,
                    executor_id,
                    enabled=bool(args["enabled"]),
                    reason=(str(args.get("reason") or "").strip() or None),
                    changed_by="hermes-conversation",
                )
            elif action == "clear":
                override_id = str(args.get("override_id") or "").strip()
                if not override_id:
                    return tool_error("supervisor_adapter: clear requires override_id")
                controller_override = registry.get_controller_override(
                    conn, override_id
                )
                if controller_override is not None:
                    cleared = registry.clear_controller_override(
                        conn,
                        override_id,
                        cleared_by="hermes-conversation",
                        reason=(str(args.get("reason") or "").strip() or None),
                    )
                else:
                    cleared = registry.clear_adapter_override(
                        conn,
                        override_id,
                        cleared_by="hermes-conversation",
                        reason=(str(args.get("reason") or "").strip() or None),
                    )
                result = {
                    "cleared": cleared,
                    "override_id": override_id,
                }
            elif action == "rerun":
                resolution = registry.resolve_adapter_task_reference(
                    conn,
                    str(args.get("task_id") or "").strip(),
                    session_id=session_id,
                    completed_only=True,
                )
                executor_id = str(args.get("executor_id") or "").strip() or None
                result = registry.reissue_task_with_adapter(
                    conn,
                    task_id=resolution["task_id"],
                    executor_value=executor_id,
                    reason=(str(args.get("reason") or "").strip() or None),
                    created_by="hermes-conversation",
                )
                result["notification"] = _subscribe_delegated_task(
                    conn,
                    task_id=result["revision_task_id"],
                    route=_kwargs.get("notification_route"),
                    session_id=session_id,
                )
                result["reference_resolution"] = resolution
            else:
                return tool_error(
                    "supervisor_adapter: action must be list, recent, history, inspect, "
                    "assign, switch, controller_state, controller_model, "
                    "executor_state, clear, or rerun"
                )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"supervisor_adapter: {exc}")


SUPERVISOR_STATUS_SCHEMA = {
    "name": "supervisor_status",
    "description": (
        "Read the hourly persisted Hermes heartbeat across service, worker, "
        "scheduled-job, receipt, and zero-MCP isolation lanes without rerunning "
        "those checks. Use mode=deep only when the operator explicitly asks for "
        "a fresh detailed audit. The scheduled "
        "lane includes every configured job plus active/paused/failure counts; "
        "required_cron is only the protected baseline, not the active-job list. "
        "This performs no domain work and invokes no model."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["snapshot", "deep"],
                "description": (
                    "snapshot is the lightweight default; deep reruns all health "
                    "probes and refreshes the snapshot."
                ),
            }
        },
        "required": [],
    },
}

SUPERVISOR_AUTOMATION_SCHEMA = {
    "name": "supervisor_automation",
    "description": (
        "Manage heartbeat failure acknowledgement from conversation. Acknowledge "
        "one or all currently failed active cron jobs after the operator has seen "
        "them, so the same failed run is not repeated in hourly heartbeat alerts. "
        "Acknowledgement is fingerprinted to that exact run; a later failed run "
        "automatically alerts again. This is not a permanent allowed-failure rule."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_failures",
                    "acknowledge_failures",
                    "clear_acknowledgements",
                ],
            },
            "jobs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Cron names, or ['all']. Omit for all current failures when "
                    "acknowledging, and all acknowledgements when clearing."
                ),
            },
        },
        "required": ["action"],
    },
}

SUPERVISOR_ROLES_SCHEMA = {
    "name": "supervisor_roles",
    "description": (
        "List active immutable role shells and whether each currently has one "
        "or more safe executor bindings. Use shell_key with supervisor_delegate."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SUPERVISOR_DELEGATE_SCHEMA = {
    "name": "supervisor_delegate",
    "description": (
        "Create a durable ready Kanban card for an active role shell and, when "
        "called from an async gateway conversation, subscribe its terminal "
        "events to that trusted originating route. Hermes does not do the work "
        "or impersonate an executor; the dispatcher binds one atomically through "
        "the many-to-many registry. Recovery sources are flattened to their "
        "original cards; an existing recovery card is reused or reopened instead "
        "of creating a recovery-of-recovery chain."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "shell_key": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "priority": {"type": "integer", "default": 0},
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "default": "scratch",
            },
            "workspace_path": {"type": "string"},
            "branch_name": {"type": "string"},
            "executor_id": {
                "type": "string",
                "description": (
                    "Optional executor pinned for this entire card, including "
                    "automatic retries. The task-scoped override is released "
                    "only when the card is done or archived."
                ),
            },
            "work_kind": {
                "type": "string",
                "enum": ["normal", "repair", "tooling"],
                "description": (
                    "Use repair for bug diagnosis, code fixes, incident recovery, "
                    "runtime remediation, and automation repair. Repair work is "
                    "pinned to the configured Codex repair executor. Use tooling "
                    "for MCP, skill, plugin, or toolset lifecycle work; it is "
                    "allowed only on tool-management."
                ),
            },
            "adapter_reason": {"type": "string"},
            "source_task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Blocked/failed card ids whose comments and run output must "
                    "be recovered and independently verified. Creates audited "
                    "non-blocking lineage; unlike dependency parents, these do "
                    "not park the new card in todo. Nested recovery cards are "
                    "canonicalized and deduplicated automatically."
                ),
            },
        },
        "required": ["shell_key", "title"],
    },
}

SUPERVISOR_PROJECT_SCHEMA = {
    "name": "supervisor_project",
    "description": (
        "Native deterministic Project/Card Controller. Use it for long-lived "
        "team projects and card chains: start a project, add an independent "
        "root card inside it, continue a completed or active card with a "
        "follow-up, split work into parallel role cards, "
        "create an independent verification card, recover failed work through "
        "another compatible adapter, inspect/locate old cards, or close/reopen "
        "a project. This is controller authority, not a worker adapter: workers "
        "execute the resulting immutable Role Shell cards and cannot mutate the "
        "project graph themselves. Completed cards stay immutable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_projects",
                    "start_project",
                    "add_project_card",
                    "continue_card",
                    "split_card",
                    "verify_card",
                    "recover_card",
                    "inspect_card",
                    "inspect_project",
                    "locate_card",
                    "close_project",
                    "reopen_project",
                ],
            },
            "project_id": {"type": "string"},
            "project_name": {"type": "string"},
            "project_slug": {"type": "string"},
            "card_id": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "shell_key": {
                "type": "string",
                "description": (
                    "Active Role Shell key. Follow-up and recovery inherit the "
                    "source card when omitted; verification always uses verification."
                ),
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
            },
            "input_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "cards": {
                "type": "array",
                "maxItems": 20,
                "description": "Child card objects for split_card.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "shell_key": {"type": "string"},
                        "acceptance_criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "input_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "workspace_kind": {
                            "type": "string",
                            "enum": ["scratch", "dir", "worktree"],
                        },
                        "workspace_path": {"type": "string"},
                        "priority": {"type": "integer"},
                        "executor_id": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
            "board": {"type": "string", "default": "default"},
            "primary_path": {"type": "string"},
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
            },
            "workspace_path": {"type": "string"},
            "executor_id": {"type": "string"},
            "priority": {"type": "integer", "default": 0},
            "include_archived": {"type": "boolean", "default": False},
        },
        "required": ["action"],
    },
}

SUPERVISOR_ADAPTER_SCHEMA = {
    "name": "supervisor_adapter",
    "description": (
        "Manage the Kanban adapter control plane from conversation. For action=list, "
        "the default compact response contains a deterministic operator_text: reply "
        "with that text verbatim and do not call supervisor_roles or supervisor_status. "
        "It shows a mobile-width Hermes/seven-role runtime tree followed by explicit "
        "controller-fallback scope, role coverage, and core tool groups. Candidate "
        "rosters are detail-only, and states never mix enabled/healthy glyphs. List owners and "
        "their actual provider/model/reasoning; recall recent cards; strengthen/assign "
        "an adapter; switch the Hermes controller, a task, role, or all roles "
        "once, temporarily, or permanently; clear an override; inspect step/run "
        "failures; health-gate enable or disable an executor; or reissue a "
        "completed card through a different executor while "
        "preserving its original history. For phrases such as 'the last task' or "
        "'방금 태스크', use task_id='latest'; Hermes resolves it from the current "
        "conversation first. Every mutation is audited."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "tools", "recent", "history", "inspect", "assign", "switch", "controller_state", "controller_model", "executor_state", "clear", "rerun"],
                "default": "list",
            },
            "target": {
                "type": "string",
                "description": "Use 'hermes' for the controller; otherwise a role shell key/id, task id, or 'all'.",
            },
            "scope_type": {"type": "string", "enum": ["session", "task", "shell", "all"]},
            "task_id": {
                "type": "string",
                "description": "Exact task id, or latest/last/recent/방금/최근. Optional for inspect and rerun; omitted means latest.",
            },
            "executor_id": {
                "type": "string",
                "description": "Executor id/name. Optional for rerun; omitted reuses the previous run adapter.",
            },
            "controller_id": {
                "type": "string",
                "description": "Registered Hermes controller adapter id/name for target='hermes'.",
            },
            "model": {
                "type": "string",
                "description": (
                    "For controller_model: exact provider catalog model id. "
                    "The change is accepted only after catalog and tool-call health gates."
                ),
            },
            "enabled": {
                "type": "boolean",
                "description": "For executor_state: requested executor availability. External command executors must pass all health URLs and their declared MCP/tool probe before enable succeeds.",
            },
            "responsibility": {
                "type": "string", "enum": ["primary", "candidate"],
            },
            "priority": {"type": "integer"},
            "weight": {"type": "number"},
            "mode": {
                "type": "string",
                "enum": ["once", "temporary", "permanent"],
                "description": (
                    "once means one whole task when target is a task, or one "
                    "claim for shell/all; temporary expires by duration; "
                    "permanent remains until explicitly cleared."
                ),
            },
            "duration_seconds": {"type": "integer", "minimum": 1},
            "override_id": {"type": "string"},
            "reason": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "completed_only": {"type": "boolean", "default": False},
            "query": {
                "type": "string",
                "description": (
                    "For action=tools: semantic capability name. Searches all "
                    "profile MCPs, skills, toolsets, built-in callable tools, "
                    "plugins, and executor capabilities without exposing secrets."
                ),
            },
            "view": {
                "type": "string",
                "enum": ["compact", "full"],
                "default": "compact",
                "description": (
                    "Compact is mandatory for conversation and includes ready-to-send "
                    "operator_text. Full is raw registry JSON for non-chat diagnostics."
                ),
            },
        },
        "required": ["action"],
    },
}


tool_registry.register(
    name="supervisor_status",
    toolset="supervisor",
    schema=SUPERVISOR_STATUS_SCHEMA,
    handler=_handle_status,
    check_fn=_check_supervisor_mode,
    emoji="control",
)

tool_registry.register(
    name="supervisor_automation",
    toolset="supervisor",
    schema=SUPERVISOR_AUTOMATION_SCHEMA,
    handler=_handle_automation,
    check_fn=_check_supervisor_mode,
    emoji="control",
)

tool_registry.register(
    name="supervisor_adapter",
    toolset="supervisor",
    schema=SUPERVISOR_ADAPTER_SCHEMA,
    handler=_handle_adapter,
    check_fn=_check_supervisor_mode,
    emoji="control",
)

tool_registry.register(
    name="supervisor_roles",
    toolset="supervisor",
    schema=SUPERVISOR_ROLES_SCHEMA,
    handler=_handle_roles,
    check_fn=_check_supervisor_mode,
    emoji="control",
)

tool_registry.register(
    name="supervisor_delegate",
    toolset="supervisor",
    schema=SUPERVISOR_DELEGATE_SCHEMA,
    handler=_handle_delegate,
    check_fn=_check_supervisor_mode,
    emoji="control",
)

tool_registry.register(
    name="supervisor_project",
    toolset="supervisor",
    schema=SUPERVISOR_PROJECT_SCHEMA,
    handler=_handle_project_cards,
    check_fn=_check_supervisor_mode,
    emoji="control",
)
