"""Strict free-only, strongest-first router for the OpenCode command executor.

The router discovers the live OpenCode catalog on every invocation.  It
preflights models in priority order, caches one healthy selection briefly, and
puts failed models on cooldown.  A real task prompt is executed exactly once;
if that run fails, a later Hermes retry rotates to the next free model instead
of replaying a possibly side-effecting prompt in the same process. Catalog
metadata proves zero cost, while a bounded health-only calibration can promote
a newly discovered model to the first fallback without replacing the proven
primary from a single synthetic result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Iterable, Optional


FREE_MODEL_PRIORITY = (
    "opencode/deepseek-v4-flash-free",
    "opencode/big-pickle",
    "opencode/mimo-v2.5-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/north-mini-code-free",
)
EXPLICIT_FREE_MODEL_IDS = frozenset({"opencode/big-pickle"})
STATE_SCHEMA = "hermes.opencode-free-router.v1"
CALIBRATION_VERSION = "hermes.opencode-free-calibration.v1"
CALIBRATION_MAX_SCORE = 4
MAX_CALIBRATIONS_PER_HEALTH = 2
CALIBRATION_FAILURE_RETRY_SECONDS = 6 * 60 * 60
CALIBRATION_RECHECK_SECONDS = 7 * 24 * 60 * 60
CATALOG_REFRESH_SECONDS = 6 * 60 * 60
CALIBRATION_PROMPT = """Do not call tools. Solve the four checks below.
Return exactly one line in this form and nothing else:
HERMES_CALIBRATION_V1|logic=<letter>|math=<integer>|code=<integer>|token=<word>

1) Logic: All Zorps are Lems. No Lems are Nibs. Which must be true?
   A) No Zorps are Nibs  B) Some Zorps are Nibs  C) All Nibs are Zorps  D) None
2) Math: What is the smallest positive integer n where n % 7 = 3 and n % 5 = 4?
3) Code: What integer does this Python expression produce: sum(n*n for n in [1,2,3] if n%2)
4) Token: Reverse the characters in KRAMHCNEB.
"""
CALIBRATION_PATTERN = re.compile(
    r"HERMES_CALIBRATION_V1\|logic=([A-D])\|math=(-?\d+)\|"
    r"code=(-?\d+)\|token=([A-Z]+)"
)


class OpenCodeFreeRouterError(RuntimeError):
    """Raised when no verified free OpenCode route can run safely."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_declared_free_model(model_id: str) -> bool:
    """Accept only OpenCode-provider models with an explicit free contract."""
    model = str(model_id or "").strip()
    return bool(
        model.startswith("opencode/")
        and (model.endswith("-free") or model in EXPLICIT_FREE_MODEL_IDS)
    )


def _qualified_dynamic_profile(profile: Any) -> bool:
    return bool(
        isinstance(profile, dict)
        and profile.get("status") == "qualified_top_fallback"
        and profile.get("calibration_version") == CALIBRATION_VERSION
        and profile.get("score") == CALIBRATION_MAX_SCORE
        and profile.get("format_exact") is True
    )


def rank_free_models(
    catalog: Iterable[str],
    dynamic_profiles: Optional[dict[str, Any]] = None,
    *,
    verified_free_models: Optional[Iterable[str]] = None,
) -> list[str]:
    """Rank live strict-free models with evidence-qualified newcomers near the top.

    The proven primary remains the anchor when it is available. A newly
    discovered model can become the first fallback only after passing the
    calibration contract; an unknown or failed model remains usable at the
    tail and can never introduce a paid/provider fallback.
    """
    metadata_verified = {
        str(model or "").strip() for model in (verified_free_models or ())
    }
    available = {
        str(model or "").strip()
        for model in catalog
        if (
            is_declared_free_model(str(model or "").strip())
            or str(model or "").strip() in metadata_verified
        )
    }
    known = [model for model in FREE_MODEL_PRIORITY if model in available]
    known_set = set(known)
    profiles = dynamic_profiles if isinstance(dynamic_profiles, dict) else {}
    dynamic = sorted(available - known_set)
    promoted = [
        model for model in dynamic if _qualified_dynamic_profile(profiles.get(model))
    ]
    promoted.sort(
        key=lambda model: (
            -int(profiles[model].get("score") or 0),
            float(profiles[model].get("latency_ms") or float("inf")),
            model,
        )
    )
    unranked = [model for model in dynamic if model not in set(promoted)]
    primary = [FREE_MODEL_PRIORITY[0]] if FREE_MODEL_PRIORITY[0] in available else []
    remaining_known = [model for model in known if model not in set(primary)]
    return primary + promoted + remaining_known + unranked


def _load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"schema": STATE_SCHEMA, "model_failures": {}}
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        return {"schema": STATE_SCHEMA, "model_failures": {}}
    if not isinstance(payload.get("model_failures"), dict):
        payload["model_failures"] = {}
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["schema"] = STATE_SCHEMA
    payload["updated_at_utc"] = _iso(_utc_now())
    fd, tmp_name = tempfile.mkstemp(prefix=".opencode-free-router-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _run_command(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(
            argv,
            124,
            stdout=stdout,
            stderr=stderr or f"timed out after {timeout}s",
        )


def _command_env(config_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["OPENCODE_CONFIG"] = str(config_path)
    return env


def _discover_catalog(
    *,
    opencode: str,
    env: dict[str, str],
    workspace: Path,
    timeout: float,
    refresh: bool = False,
) -> tuple[list[str], str, dict[str, dict[str, Any]]]:
    command = [opencode, "models", "opencode", "--verbose"]
    if refresh:
        command.append("--refresh")
    completed = _run_command(
        command,
        env=env,
        cwd=workspace,
        timeout=timeout,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()[:2000]
        raise OpenCodeFreeRouterError(f"OpenCode catalog failed: {detail}")
    catalog = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith("opencode/")
    ]
    metadata = _parse_verbose_model_metadata(completed.stdout)
    available = sorted(
        {
            model
            for model in catalog
            if (
                _metadata_declares_zero_cost_free(metadata.get(model))
                if model in metadata
                else is_declared_free_model(model)
            )
        }
    )
    if not available:
        raise OpenCodeFreeRouterError(
            "OpenCode catalog contains no declared free model; paid fallback is forbidden"
        )
    return available, "\n".join(available), metadata


def _catalog_refresh_due(state: dict[str, Any], now: datetime) -> bool:
    refreshed_at = _parse_iso(state.get("last_catalog_refresh_at_utc"))
    return bool(
        refreshed_at is None
        or refreshed_at + timedelta(seconds=CATALOG_REFRESH_SECONDS) <= now
    )


def _parse_verbose_model_metadata(output: str) -> dict[str, dict[str, Any]]:
    header_pattern = re.compile(r"(?m)^(opencode/[^\s]+)\s*$")
    matches = list(header_pattern.finditer(str(output or "")))
    parsed: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        raw_metadata = output[match.end() : end].strip()
        if not raw_metadata.startswith("{"):
            continue
        try:
            payload = json.loads(raw_metadata)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            parsed[match.group(1)] = payload
    return parsed


def _cost_tree_is_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, dict) and value:
        return all(_cost_tree_is_zero(item) for item in value.values())
    return False


def _metadata_declares_zero_cost_free(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    cost = metadata.get("cost")
    return bool(
        metadata.get("providerID") == "opencode"
        and metadata.get("status") == "active"
        and isinstance(cost, dict)
        and "input" in cost
        and "output" in cost
        and _cost_tree_is_zero(cost)
    )


def _catalog_metadata_summary(
    metadata: dict[str, dict[str, Any]],
    *,
    available: list[str],
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for model in available:
        item = metadata.get(model)
        if not isinstance(item, dict):
            continue
        capabilities = item.get("capabilities")
        limits = item.get("limit")
        summary[model] = {
            "name": item.get("name"),
            "family": item.get("family"),
            "release_date": item.get("release_date"),
            "context_limit": limits.get("context") if isinstance(limits, dict) else None,
            "output_limit": limits.get("output") if isinstance(limits, dict) else None,
            "reasoning": (
                capabilities.get("reasoning")
                if isinstance(capabilities, dict)
                else None
            ),
            "toolcall": (
                capabilities.get("toolcall")
                if isinstance(capabilities, dict)
                else None
            ),
            "zero_cost_verified": _metadata_declares_zero_cost_free(item),
        }
    return summary


def _catalog_fingerprint(catalog: Iterable[str]) -> str:
    normalized = "\n".join(sorted(set(catalog))).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _update_catalog_state(
    state: dict[str, Any],
    *,
    available: list[str],
    now: datetime,
) -> tuple[list[str], list[str]]:
    previous_raw = state.get("catalog_available")
    if not isinstance(previous_raw, list):
        previous_raw = state.get("catalog")
    previous = {
        str(model).strip()
        for model in (previous_raw if isinstance(previous_raw, list) else [])
        if str(model).strip().startswith("opencode/")
    }
    current = set(available)
    added = sorted(current - previous)
    removed = sorted(previous - current)
    state["catalog_available"] = sorted(current)
    state["catalog_fingerprint"] = _catalog_fingerprint(current)
    if added or removed:
        state["last_catalog_change"] = {
            "at_utc": _iso(now),
            "added": added,
            "removed": removed,
        }
    return added, removed


def _parse_calibration_output(output: str) -> tuple[int, bool, dict[str, Any]]:
    text = str(output or "").strip()
    match = CALIBRATION_PATTERN.fullmatch(text)
    if match is None:
        return 0, False, {}
    logic, math_text, code_text, token = match.groups()
    answers = {
        "logic": logic,
        "math": int(math_text),
        "code": int(code_text),
        "token": token,
    }
    expected = {"logic": "A", "math": 24, "code": 10, "token": "BENCHMARK"}
    score = sum(answers[key] == value for key, value in expected.items())
    return score, True, answers


def _calibration_due_models(
    *,
    available: list[str],
    state: dict[str, Any],
    now: datetime,
) -> list[str]:
    profiles = state.get("dynamic_model_profiles")
    profiles = profiles if isinstance(profiles, dict) else {}
    known = set(FREE_MODEL_PRIORITY)
    due: list[str] = []
    for model in sorted(set(available) - known):
        profile = profiles.get(model)
        if not isinstance(profile, dict):
            due.append(model)
            continue
        if profile.get("calibration_version") != CALIBRATION_VERSION:
            due.append(model)
            continue
        retry_at = _parse_iso(profile.get("next_calibration_after_utc"))
        if retry_at is not None and retry_at <= now:
            due.append(model)
    return due[:MAX_CALIBRATIONS_PER_HEALTH]


def _calibrate_dynamic_models(
    *,
    available: list[str],
    state: dict[str, Any],
    opencode: str,
    env: dict[str, str],
    workspace: Path,
    agent: str,
    probe_timeout: float,
    now: datetime,
) -> list[dict[str, Any]]:
    profiles = state.setdefault("dynamic_model_profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        state["dynamic_model_profiles"] = profiles
    results: list[dict[str, Any]] = []
    for model in _calibration_due_models(available=available, state=state, now=now):
        started = monotonic()
        completed = _run_command(
            _model_command(
                opencode=opencode,
                workspace=workspace,
                model=model,
                agent=agent,
                prompt=CALIBRATION_PROMPT,
            ),
            env=env,
            cwd=workspace,
            timeout=probe_timeout,
        )
        latency_ms = round((monotonic() - started) * 1000, 1)
        score, format_exact, answers = _parse_calibration_output(completed.stdout)
        qualified = (
            completed.returncode == 0
            and format_exact
            and score == CALIBRATION_MAX_SCORE
        )
        if qualified:
            status = "qualified_top_fallback"
            next_calibration = None
        elif completed.returncode == 0:
            status = "available_unranked"
            next_calibration = now + timedelta(seconds=CALIBRATION_RECHECK_SECONDS)
        else:
            status = "calibration_failed"
            next_calibration = now + timedelta(
                seconds=CALIBRATION_FAILURE_RETRY_SECONDS
            )
        profile: dict[str, Any] = {
            "status": status,
            "calibration_version": CALIBRATION_VERSION,
            "calibrated_at_utc": _iso(now),
            "score": score,
            "max_score": CALIBRATION_MAX_SCORE,
            "format_exact": format_exact,
            "latency_ms": latency_ms,
            "returncode": completed.returncode,
        }
        if answers:
            profile["answers"] = answers
        if next_calibration is not None:
            profile["next_calibration_after_utc"] = _iso(next_calibration)
        if completed.returncode:
            profile["failure"] = _failure_detail(completed)
        profiles[model] = profile
        results.append({"model": model, **profile})
    return results


def _failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()[:2000]


def _on_cooldown(state: dict[str, Any], model: str, now: datetime) -> bool:
    failures = state.get("model_failures")
    item = failures.get(model) if isinstance(failures, dict) else None
    until = _parse_iso(item.get("cooldown_until_utc")) if isinstance(item, dict) else None
    return bool(until and until > now)


def _mark_failure(
    state: dict[str, Any],
    *,
    model: str,
    reason: str,
    now: datetime,
    cooldown_seconds: int,
) -> None:
    failures = state.setdefault("model_failures", {})
    failures[model] = {
        "failed_at_utc": _iso(now),
        "cooldown_until_utc": _iso(now + timedelta(seconds=cooldown_seconds)),
        "reason": reason[:2000],
    }
    if state.get("selected_model") == model:
        state["selected_model"] = None
        state["healthy_until_utc"] = None


def _mark_healthy(
    state: dict[str, Any],
    *,
    model: str,
    catalog: list[str],
    now: datetime,
    health_ttl_seconds: int,
) -> None:
    failures = state.setdefault("model_failures", {})
    failures.pop(model, None)
    state.update(
        {
            "catalog": catalog,
            "selected_model": model,
            "selected_at_utc": _iso(now),
            "healthy_until_utc": _iso(now + timedelta(seconds=health_ttl_seconds)),
            "policy": "strict-free-only-proven-primary-calibrated-newcomers",
        }
    )


def _cached_model(
    state: dict[str, Any],
    *,
    candidates: list[str],
    now: datetime,
) -> Optional[str]:
    selected = str(state.get("selected_model") or "").strip()
    healthy_until = _parse_iso(state.get("healthy_until_utc"))
    if (
        selected in candidates
        and healthy_until is not None
        and healthy_until > now
        and not _on_cooldown(state, selected, now)
    ):
        return selected
    return None


def _model_command(
    *,
    opencode: str,
    workspace: Path,
    model: str,
    agent: str,
    prompt: str,
) -> list[str]:
    return [
        opencode,
        "run",
        "--dir",
        str(workspace),
        "--model",
        model,
        "--agent",
        agent,
        "--format",
        "default",
        "--auto",
        prompt,
    ]


def _select_model(
    *,
    candidates: list[str],
    state: dict[str, Any],
    opencode: str,
    env: dict[str, str],
    workspace: Path,
    agent: str,
    probe_timeout: float,
    health_ttl_seconds: int,
    cooldown_seconds: int,
    now: datetime,
) -> tuple[str, list[dict[str, Any]]]:
    cached = _cached_model(state, candidates=candidates, now=now)
    if cached:
        return cached, [{"model": cached, "status": "cached_healthy"}]
    attempts: list[dict[str, Any]] = []
    for model in candidates:
        if _on_cooldown(state, model, now):
            attempts.append({"model": model, "status": "cooldown"})
            continue
        completed = _run_command(
            _model_command(
                opencode=opencode,
                workspace=workspace,
                model=model,
                agent=agent,
                prompt="Reply exactly READY. Do not call tools.",
            ),
            env=env,
            cwd=workspace,
            timeout=probe_timeout,
        )
        passed = completed.returncode == 0 and "READY" in completed.stdout
        attempts.append(
            {
                "model": model,
                "status": "healthy" if passed else "failed",
                "returncode": completed.returncode,
            }
        )
        if passed:
            _mark_healthy(
                state,
                model=model,
                catalog=candidates,
                now=now,
                health_ttl_seconds=health_ttl_seconds,
            )
            return model, attempts
        _mark_failure(
            state,
            model=model,
            reason=_failure_detail(completed),
            now=now,
            cooldown_seconds=cooldown_seconds,
        )
    raise OpenCodeFreeRouterError(
        "all catalog-confirmed free OpenCode models failed preflight"
    )


def health_check(args: argparse.Namespace) -> str:
    workspace = Path(args.workspace).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    state_path = Path(args.state_file).expanduser().resolve()
    if not workspace.is_dir() or not config_path.is_file():
        raise OpenCodeFreeRouterError("OpenCode workspace or MCP config is unavailable")
    state = _load_state(state_path)
    now = _utc_now()
    env = _command_env(config_path)
    mcp = _run_command(
        [args.opencode, "mcp", "list"],
        env=env,
        cwd=workspace,
        timeout=args.catalog_timeout_seconds,
    )
    if mcp.returncode:
        raise OpenCodeFreeRouterError(f"OpenCode MCP probe failed: {_failure_detail(mcp)}")
    refresh_due = _catalog_refresh_due(state, now)
    refresh_status = "cached"
    try:
        available, catalog_output, catalog_metadata = _discover_catalog(
            opencode=args.opencode,
            env=env,
            workspace=workspace,
            timeout=args.catalog_timeout_seconds,
            refresh=refresh_due,
        )
    except OpenCodeFreeRouterError as exc:
        if not refresh_due:
            raise
        state["last_catalog_refresh_error"] = str(exc)
        available, catalog_output, catalog_metadata = _discover_catalog(
            opencode=args.opencode,
            env=env,
            workspace=workspace,
            timeout=args.catalog_timeout_seconds,
            refresh=False,
        )
        refresh_status = "stale_cache_fallback"
    else:
        if refresh_due:
            state["last_catalog_refresh_at_utc"] = _iso(now)
            state.pop("last_catalog_refresh_error", None)
            refresh_status = "refreshed"
    added, removed = _update_catalog_state(state, available=available, now=now)
    state["catalog_metadata"] = _catalog_metadata_summary(
        catalog_metadata,
        available=available,
    )
    calibrations = _calibrate_dynamic_models(
        available=available,
        state=state,
        opencode=args.opencode,
        env=env,
        workspace=workspace,
        agent=args.agent,
        probe_timeout=args.probe_timeout_seconds,
        now=now,
    )
    candidates = rank_free_models(
        available,
        state.get("dynamic_model_profiles"),
        verified_free_models=available,
    )
    state["effective_priority"] = candidates
    try:
        model, attempts = _select_model(
            candidates=candidates,
            state=state,
            opencode=args.opencode,
            env=env,
            workspace=workspace,
            agent=args.agent,
            probe_timeout=args.probe_timeout_seconds,
            health_ttl_seconds=args.health_ttl_seconds,
            cooldown_seconds=args.cooldown_seconds,
            now=now,
        )
    except Exception as exc:
        state["catalog"] = candidates
        state["last_health_error"] = str(exc)
        _save_state(state_path, state)
        raise
    state["last_health_attempts"] = attempts
    state["last_health_calibrations"] = calibrations
    _save_state(state_path, state)
    change_line = ""
    if added or removed:
        change_line = (
            f"OPENCODE_FREE_CATALOG_ADDED={','.join(added) or '-'} "
            f"REMOVED={','.join(removed) or '-'}"
        )
    calibration_line = ""
    if calibrations:
        calibration_line = "OPENCODE_FREE_CALIBRATED=" + ",".join(
            f"{item['model']}:{item['status']}" for item in calibrations
        )
    return "\n".join(
        part.rstrip()
        for part in (
            mcp.stdout,
            catalog_output,
            f"OPENCODE_FREE_CATALOG_REFRESH={refresh_status}",
            change_line,
            calibration_line,
            "OPENCODE_FREE_PRIORITY=" + ",".join(candidates),
            f"OPENCODE_FREE_MODEL_SELECTED={model}",
            "READY",
        )
        if part.strip()
    )


def run_task(args: argparse.Namespace) -> str:
    workspace = Path(args.workspace).expanduser().resolve()
    prompt_file = Path(args.prompt_file).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    state_path = Path(args.state_file).expanduser().resolve()
    if not workspace.is_dir() or not prompt_file.is_file() or not config_path.is_file():
        raise OpenCodeFreeRouterError(
            "OpenCode workspace, prompt file, or MCP config is unavailable"
        )
    env = _command_env(config_path)
    available, _catalog_output, catalog_metadata = _discover_catalog(
        opencode=args.opencode,
        env=env,
        workspace=workspace,
        timeout=args.catalog_timeout_seconds,
    )
    state = _load_state(state_path)
    now = _utc_now()
    _update_catalog_state(state, available=available, now=now)
    state["catalog_metadata"] = _catalog_metadata_summary(
        catalog_metadata,
        available=available,
    )
    candidates = rank_free_models(
        available,
        state.get("dynamic_model_profiles"),
        verified_free_models=available,
    )
    state["effective_priority"] = candidates
    try:
        model, attempts = _select_model(
            candidates=candidates,
            state=state,
            opencode=args.opencode,
            env=env,
            workspace=workspace,
            agent=args.agent,
            probe_timeout=args.probe_timeout_seconds,
            health_ttl_seconds=args.health_ttl_seconds,
            cooldown_seconds=args.cooldown_seconds,
            now=now,
        )
    except Exception as exc:
        state["catalog"] = candidates
        state["last_task_preflight_error"] = str(exc)
        _save_state(state_path, state)
        raise
    state["last_task_preflight_attempts"] = attempts
    _save_state(state_path, state)
    prompt = prompt_file.read_text(encoding="utf-8")
    completed = _run_command(
        _model_command(
            opencode=args.opencode,
            workspace=workspace,
            model=model,
            agent=args.agent,
            prompt=prompt,
        ),
        env=env,
        cwd=workspace,
        timeout=args.task_timeout_seconds,
    )
    result = completed.stdout.strip()
    if completed.returncode or not result:
        reason = _failure_detail(completed) if completed.returncode else "empty model output"
        _mark_failure(
            state,
            model=model,
            reason=reason,
            now=_utc_now(),
            cooldown_seconds=args.cooldown_seconds,
        )
        state["last_task_failure"] = {
            "model": model,
            "returncode": completed.returncode,
            "reason": reason,
            "prompt_replayed": False,
        }
        _save_state(state_path, state)
        raise OpenCodeFreeRouterError(
            f"OpenCode free model {model} failed; prompt was not replayed: {reason}"
        )
    _mark_healthy(
        state,
        model=model,
        catalog=candidates,
        now=_utc_now(),
        health_ttl_seconds=args.health_ttl_seconds,
    )
    state["last_task_success"] = {"model": model, "prompt_replayed": False}
    state.pop("last_task_failure", None)
    state.pop("last_task_preflight_error", None)
    _save_state(state_path, state)
    return result


def _default_paths() -> tuple[Path, Path]:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    return (
        home / "supervisor" / "mcp" / "opencode.json",
        home / "supervisor" / "opencode_free_router_state.json",
    )


def build_parser() -> argparse.ArgumentParser:
    config_default, state_default = _default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--health-check", action="store_true")
    mode.add_argument("--prompt-file")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--config", default=str(config_default))
    parser.add_argument("--state-file", default=str(state_default))
    parser.add_argument(
        "--opencode",
        default=shutil.which("opencode") or "opencode",
        help="OpenCode executable (defaults to the first opencode on PATH)",
    )
    parser.add_argument("--agent", default="remote-vibe-build")
    parser.add_argument("--catalog-timeout-seconds", type=float, default=30)
    parser.add_argument("--probe-timeout-seconds", type=float, default=60)
    parser.add_argument("--task-timeout-seconds", type=float, default=1740)
    parser.add_argument("--health-ttl-seconds", type=int, default=900)
    parser.add_argument("--cooldown-seconds", type=int, default=900)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        output = health_check(args) if args.health_check else run_task(args)
    except Exception as exc:
        print(f"OpenCode free router failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
