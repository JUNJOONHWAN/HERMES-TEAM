"""Fail-closed executor adapters for role-shell Kanban runs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from hermes_cli import supervisor_registry as registry


class ExecutorAdapterError(RuntimeError):
    pass


def _run_row(conn, run_id: int):
    row = conn.execute(
        "SELECT * FROM task_runs WHERE id=?",
        (int(run_id),),
    ).fetchone()
    if row is None:
        raise ExecutorAdapterError(f"run {run_id} not found")
    if not row["role_shell_id"] or not row["executor_id"] or not row["binding_id"]:
        raise ExecutorAdapterError(f"run {run_id} has incomplete binding provenance")
    return row


def _worker_prompt(conn, task, selection: registry.Selection) -> str:
    from hermes_cli import kanban_db as kb

    context = kb.build_worker_context(conn, task.id)
    recovery_context = _recovery_source_prompt(conn, task.id)
    contract = selection.shell.contract
    role_instructions = str(contract.get("instructions") or "").strip()
    evidence = selection.shell.evidence_policy
    run = _run_row(conn, task.current_run_id)
    goal_id = registry.timeline_goal_id(task.id, run["id"])
    if selection.executor.adapter_type == "command":
        terminal_guidance = (
            "This external command adapter does not expose kanban_complete or "
            "kanban_block. Return the exact complete user-facing answer on stdout; "
            "the trusted bridge records the receipt and performs the terminal task "
            "transition. Do not add a warning that a close tool is unavailable."
        )
    else:
        terminal_guidance = (
            "Before closing a bound task, submit a receipt through kanban_complete "
            "or kanban_block. The receipt must include Timeline context/slice/node ids "
            "and verify_all invalid_count=0 when the shell requires Timeline. When "
            "neural_recall_required is true, call NeuralLink recall before acting and "
            "include timeline.neural_recall with performed=true, the query, candidate "
            "count, and context character count. For "
            "kanban_complete, pass the exact complete user-facing final answer in "
            "result, a separate 1-3 sentence handoff in summary, and structured "
            "evidence in receipt.outputs. Never leave result empty merely because "
            "the receipt contains data."
        )
    return (
        f"# Hermes role-shell assignment\n\n"
        f"Task: {task.id} — {task.title}\n"
        f"Role shell: {selection.shell.id} ({selection.shell.shell_key})\n"
        f"Executor: {selection.executor.id}\n"
        f"Binding: {selection.binding.id}\n"
        f"Effective capabilities: {', '.join(selection.effective_capabilities) or '(none)'}\n\n"
        f"The role contract is authoritative. Your executor identity does not widen "
        f"the effective capabilities above.\n\n"
        f"{role_instructions}\n\n"
        f"## Timeline evidence contract\n"
        f"{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Dispatcher-stamped Timeline goal ID (use exactly): {goal_id}\n"
        f"Trusted receipt provenance: task_id={task.id}, run_id={run['id']}, "
        f"role_shell_id={selection.shell.id}, executor_id={selection.executor.id}, "
        f"binding_id={selection.binding.id}.\n\n"
        f"{terminal_guidance}\n\n"
        f"{recovery_context}"
        f"{context}"
    )


def _recovery_source_prompt(conn, task_id: str) -> str:
    """Render bounded source comments/runs for a result-recovery card."""
    sources = registry.list_task_recovery_sources(conn, task_id)
    if not sources:
        return ""
    lines = [
        "## Non-blocking result-recovery sources",
        "These source cards are lineage/evidence references, not dependencies. "
        "Recover their output, independently verify it, and close this card; "
        "do not create another recovery card merely because a source is blocked.",
        "",
    ]
    total_budget = 16_000

    def _append(text: str) -> None:
        nonlocal total_budget
        if total_budget <= 0:
            return
        rendered = str(text)
        if len(rendered) > total_budget:
            rendered = rendered[:total_budget] + "… [recovery context truncated]"
        lines.append(rendered)
        total_budget -= len(rendered)

    for source in sources:
        if total_budget <= 0:
            break
        source_id = source["source_task_id"]
        _append(
            f"### {source_id} — {source['title']} (status={source['status']})"
        )
        run_rows = conn.execute(
            "SELECT id,status,outcome,executor_id,error,summary FROM task_runs "
            "WHERE task_id=? ORDER BY id DESC LIMIT 3",
            (source_id,),
        ).fetchall()
        for run in reversed(run_rows):
            _append(
                f"- run {run['id']}: status={run['status']}, "
                f"outcome={run['outcome'] or '-'}, "
                f"executor={run['executor_id'] or '-'}, "
                f"error={run['error'] or '-'}"
            )
            if run["summary"]:
                _append(f"  summary: {run['summary']}")
        comment_rows = conn.execute(
            "SELECT id,author,body FROM task_comments WHERE task_id=? "
            "ORDER BY id DESC LIMIT 6",
            (source_id,),
        ).fetchall()
        for comment in reversed(comment_rows):
            _append(
                f"comment {comment['id']} from {comment['author']}:\n"
                f"{comment['body']}"
            )
        if source.get("result"):
            _append(f"recorded result:\n{source['result']}")
        _append("")
    return "\n".join(lines).rstrip() + "\n\n"


def _binding_env(
    task,
    run,
    selection: registry.Selection,
    board: Optional[str],
    workspace: str,
) -> dict[str, str]:
    from hermes_cli import kanban_db as kb
    from hermes_constants import get_default_hermes_root

    env = dict(os.environ)
    env.update(
        {
            "HERMES_KANBAN_TASK": task.id,
            "HERMES_KANBAN_RUN_ID": str(run["id"]),
            "HERMES_ROLE_SHELL_ID": selection.shell.id,
            "HERMES_EXECUTOR_ID": selection.executor.id,
            "HERMES_BINDING_ID": selection.binding.id,
            "HERMES_EFFECTIVE_CAPABILITIES": ",".join(selection.effective_capabilities),
            "HERMES_TIMELINE_GOAL_ID": registry.timeline_goal_id(task.id, run["id"]),
            "HERMES_KANBAN_DB": str(kb.kanban_db_path(board=board)),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(kb.workspaces_root(board=board)),
            "HERMES_KANBAN_BOARD": kb._normalize_board_slug(board) or kb.get_current_board(),
            "HERMES_KANBAN_WORKSPACE": str(Path(workspace).resolve()),
            "HERMES_SUPERVISOR_ROOT": str(get_default_hermes_root()),
        }
    )
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    return env


def _spawn_hermes_profile(
    conn,
    task,
    workspace: str,
    run,
    selection: registry.Selection,
    *,
    board: Optional[str],
) -> Optional[int]:
    from hermes_cli.kanban_db import _default_spawn

    profile = str(selection.executor.launch_config.get("profile") or "").strip()
    if not profile:
        raise ExecutorAdapterError("hermes_profile executor has no profile")
    routed_task = replace(task, assignee=profile)
    routed_task.role_shell_id = selection.shell.id
    routed_task.executor_id = selection.executor.id
    routed_task.binding_id = selection.binding.id
    routed_task.effective_capabilities = list(selection.effective_capabilities)
    routed_task.timeline_goal_id = registry.timeline_goal_id(task.id, run["id"])
    routed_task.worker_prompt = _worker_prompt(conn, task, selection)
    return _default_spawn(routed_task, workspace, board=board)


def _render_argv(argv: list[Any], values: dict[str, str]) -> list[str]:
    rendered: list[str] = []
    for raw in argv:
        item = str(raw)
        for key, value in values.items():
            item = item.replace("{" + key + "}", value)
        rendered.append(item)
    return rendered


def _spawn_command(
    conn,
    task,
    workspace: str,
    run,
    selection: registry.Selection,
    *,
    board: Optional[str],
) -> int:
    launch = selection.executor.launch_config
    argv = launch.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ExecutorAdapterError("command executor requires non-empty argv list")
    if not any("{prompt_file}" in str(item) for item in argv):
        raise ExecutorAdapterError(
            "command executor argv must contain {prompt_file}; prompt delivery is mandatory"
        )
    if launch.get("shell"):
        raise ExecutorAdapterError("command executor shell mode is forbidden")
    prompt_dir = Path(workspace) / ".hermes-supervisor"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompt_dir / f"run-{run['id']}.md"
    prompt_file.write_text(
        _worker_prompt(conn, task, selection),
        encoding="utf-8",
    )
    values = {
        "task_id": task.id,
        "run_id": str(run["id"]),
        "workspace": workspace,
        "prompt_file": str(prompt_file),
        "shell_id": selection.shell.id,
        "executor_id": selection.executor.id,
        "binding_id": selection.binding.id,
        "capabilities_csv": ",".join(selection.effective_capabilities),
    }
    command = _render_argv(argv, values)
    env = _binding_env(task, run, selection, board, workspace)
    configured_env = launch.get("env") or {}
    if not isinstance(configured_env, dict):
        raise ExecutorAdapterError("command executor launch_config.env must be an object")
    reserved_env = {
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_ROLE_SHELL_ID",
        "HERMES_EXECUTOR_ID",
        "HERMES_BINDING_ID",
        "HERMES_EFFECTIVE_CAPABILITIES",
        "HERMES_TIMELINE_GOAL_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_TENANT",
    }
    conflicts = sorted(reserved_env & {str(key) for key in configured_env})
    if conflicts:
        raise ExecutorAdapterError(
            "command executor cannot override supervisor provenance env: "
            + ", ".join(conflicts)
        )
    for key, value in configured_env.items():
        env[str(key)] = str(value)
    log_dir = Path(workspace) / ".hermes-supervisor"
    log_path = log_dir / f"run-{run['id']}.log"
    log_handle = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except Exception:
        log_handle.close()
        raise
    return int(proc.pid)


def spawn_bound_task(
    conn,
    task,
    workspace: str,
    *,
    board: Optional[str] = None,
) -> Optional[int]:
    """Spawn the executor already recorded on the task's current run."""
    if task.current_run_id is None:
        raise ExecutorAdapterError(f"task {task.id} has no current run")
    run = _run_row(conn, task.current_run_id)
    shell = registry.get_shell(conn, shell_id=run["role_shell_id"])
    executor = registry.get_executor(conn, run["executor_id"])
    binding = registry.get_binding(conn, run["binding_id"])
    if shell is None or executor is None or binding is None:
        raise ExecutorAdapterError("bound run references missing registry records")
    if not executor.enabled or not binding.enabled:
        raise ExecutorAdapterError("bound run executor or binding is disabled")
    if binding.constraints.get("auto_spawn", True) is False:
        raise ExecutorAdapterError("bound run binding is not auto-spawnable")
    if executor.adapter_type not in set(shell.contract.get("allowed_adapters") or []):
        raise ExecutorAdapterError("bound run adapter is forbidden by the role shell")
    effective = sorted(
        set(shell.allowed_capabilities)
        & set(executor.capabilities)
        & (set(binding.capability_cap) if binding.capability_cap else set(shell.allowed_capabilities))
    )
    if not effective or not set(shell.required_capabilities).issubset(effective):
        raise ExecutorAdapterError("bound run has insufficient effective capabilities")
    selection = registry.Selection(
        shell=shell,
        executor=executor,
        binding=binding,
        effective_capabilities=effective,
        active_runs=registry.active_run_count(conn, executor.id),
    )
    if executor.adapter_type == "hermes_profile":
        return _spawn_hermes_profile(
            conn, task, workspace, run, selection, board=board
        )
    if executor.adapter_type == "command":
        return _spawn_command(conn, task, workspace, run, selection, board=board)
    raise ExecutorAdapterError(
        f"executor {executor.id} adapter {executor.adapter_type!r} is pull/manual only"
    )


def heartbeat_from_pid(conn, executor_id: str, pid: Optional[int]) -> bool:
    """Deterministically refresh executor health from an owned worker PID."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        registry.heartbeat_executor(conn, executor_id, health_state="unhealthy")
        return False
    registry.heartbeat_executor(conn, executor_id, health_state="healthy")
    return True


def cancel_pid(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.killpg(int(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, ValueError):
        return False
