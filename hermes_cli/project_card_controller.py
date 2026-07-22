"""Deterministic Project/Card Controller for governed Hermes work.

This module is control-plane code, not an execution adapter.  It is the sole
writer for the high-level project/card operations exposed to web and messaging
surfaces.  Role adapters still execute cards; they never own project lifecycle
or mutate the graph directly.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_cli import supervisor_registry as registry


SCHEMA = "hermes.project-card-controller.v2"
ACTIVE_CARD_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review"}
)


class ProjectCardControllerError(ValueError):
    """Raised when a deterministic project/card invariant is violated."""


def _clean_list(values: Optional[Iterable[Any]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _acceptance(values: Optional[Iterable[Any]], title: str) -> list[str]:
    cleaned = _clean_list(values)
    return cleaned or [f"Complete and verify the requested result: {title.strip()}"]


def _resolve_shell(conn, shell_value: str):
    value = str(shell_value or "").strip()
    if not value:
        raise ProjectCardControllerError("shell_key is required")
    shell = registry.get_shell(conn, shell_key=value)
    if shell is None:
        shell = registry.get_shell(conn, shell_id=value)
    if shell is None:
        raise ProjectCardControllerError(f"unknown role shell: {value}")
    active = registry.get_shell(conn, shell_key=shell.shell_key)
    if active is None:
        raise ProjectCardControllerError(
            f"no active role shell for {shell.shell_key!r}"
        )
    return active


def _source_shell_key(conn, task: kb.Task) -> str:
    if not task.role_shell_id:
        raise ProjectCardControllerError(
            f"card {task.id} has no Role Shell; choose shell_key explicitly"
        )
    shell = registry.get_shell(conn, shell_id=task.role_shell_id)
    if shell is None:
        raise ProjectCardControllerError(
            f"card {task.id} references unknown Role Shell {task.role_shell_id}"
        )
    return shell.shell_key


def _board_for_project(project: pdb.Project) -> str:
    return str(project.board_slug or kb.DEFAULT_BOARD)


def _workspace_for_card(
    *,
    project: pdb.Project,
    shell_key: str,
    workspace_kind: Optional[str],
    workspace_path: Optional[str],
) -> tuple[str, Optional[str]]:
    """Resolve a safe workspace before the card becomes dispatchable.

    Project folders are not necessarily Git repositories.  Code cards use a
    linked worktree only when the selected anchor is actually inside a Git
    repository; otherwise every role uses the durable project directory.  A
    project with no folder remains an isolated scratch task.

    Explicit workspace choices are honoured, but an explicit worktree is
    rejected here when it has no Git anchor.  That keeps a bad card out of the
    dispatcher instead of discovering the configuration error after claiming
    and retrying it.
    """
    requested_kind = str(workspace_kind or "").strip() or None
    requested_path = str(workspace_path or "").strip() or None
    if requested_kind and requested_kind not in kb.VALID_WORKSPACE_KINDS:
        raise ProjectCardControllerError(
            f"workspace_kind must be one of {sorted(kb.VALID_WORKSPACE_KINDS)}"
        )

    project_path = str(project.primary_path or "").strip() or None
    anchor_text = requested_path or project_path
    anchor = Path(anchor_text).expanduser() if anchor_text else None

    if requested_kind == "worktree":
        if anchor is None:
            raise ProjectCardControllerError(
                "workspace_kind=worktree requires workspace_path or a Project primary_path"
            )
        if not anchor.is_absolute():
            raise ProjectCardControllerError("worktree workspace_path must be absolute")
        if kb._repo_root_for_worktree_target(anchor) is None:
            raise ProjectCardControllerError(
                f"worktree workspace has no Git repository anchor: {anchor}"
            )
        return "worktree", str(anchor.resolve(strict=False))

    if requested_kind == "dir":
        if anchor is None:
            raise ProjectCardControllerError(
                "workspace_kind=dir requires workspace_path or a Project primary_path"
            )
        if not anchor.is_absolute():
            raise ProjectCardControllerError("dir workspace_path must be absolute")
        return "dir", str(anchor.resolve(strict=False))

    if requested_kind == "scratch":
        if requested_path:
            raise ProjectCardControllerError(
                "scratch workspaces are controller-managed; omit workspace_path"
            )
        return "scratch", None

    if anchor is None:
        return "scratch", None
    if not anchor.is_absolute():
        raise ProjectCardControllerError("project workspace path must be absolute")

    # Only coding cards need branch isolation.  Research, browser, operations,
    # and verification cards use the durable Project directory so their
    # artifacts remain visible to later cards in the same Project.
    if shell_key == "code":
        repo_root = kb._repo_root_for_worktree_target(anchor)
        if repo_root is not None:
            return "worktree", str(repo_root)
    return "dir", str(anchor.resolve(strict=False))


def _project(
    project_value: str,
    *,
    include_completed: bool = True,
    include_archived: bool = False,
) -> pdb.Project:
    value = str(project_value or "").strip()
    if not value:
        raise ProjectCardControllerError("project_id is required")
    with pdb.connect_closing() as conn:
        project = pdb.get_project(conn, value)
    if project is None or (project.archived and not include_archived):
        raise ProjectCardControllerError(f"unknown project: {value}")
    if not include_completed and project.status != "active":
        raise ProjectCardControllerError(
            f"project {project.id} is {project.status}; resume or reopen it before adding cards"
        )
    return project


def locate_card(card_id: str, *, board: Optional[str] = None) -> dict[str, Any]:
    """Locate a globally-addressed card across all non-archived boards."""
    task_id = str(card_id or "").strip()
    if not task_id:
        raise ProjectCardControllerError("card_id is required")
    boards = [str(board)] if board else [
        str(item["slug"]) for item in kb.list_boards(include_archived=False)
    ]
    matches: list[tuple[str, kb.Task]] = []
    for slug in dict.fromkeys(boards):
        try:
            with kb.connect_closing(board=slug) as conn:
                task = kb.get_task(conn, task_id)
        except (OSError, ValueError):
            continue
        if task is not None:
            matches.append((slug, task))
    if not matches:
        raise ProjectCardControllerError(f"card not found: {task_id}")
    if len(matches) > 1:
        raise ProjectCardControllerError(
            f"card id {task_id} is ambiguous across boards: "
            + ", ".join(slug for slug, _ in matches)
        )
    slug, task = matches[0]
    return {"board": slug, "task": task}


def _create_card(
    *,
    conn,
    project: pdb.Project,
    title: str,
    shell_key: str,
    body: Optional[str] = None,
    source_task: Optional[kb.Task] = None,
    relation_type: str = "depends_on",
    acceptance_criteria: Optional[Iterable[Any]] = None,
    input_refs: Optional[Iterable[Any]] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    priority: int = 0,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    idempotency_key: Optional[str] = None,
) -> kb.Task:
    title = str(title or "").strip()
    if not title:
        raise ProjectCardControllerError("card title is required")
    shell = _resolve_shell(conn, shell_key)
    kind, resolved_workspace_path = _workspace_for_card(
        project=project,
        shell_key=shell.shell_key,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
    )
    refs = _clean_list(input_refs)
    parents: list[str] = []
    root_task_id: Optional[str] = None
    if source_task is not None:
        parents = [source_task.id]
        root_task_id = source_task.root_task_id or source_task.id
        inherited_ref = f"card:{source_task.id}"
        if inherited_ref not in refs:
            refs.insert(0, inherited_ref)
    task_id = kb.create_task(
        conn,
        title=title,
        body=(str(body).strip() if body and str(body).strip() else None),
        role_shell_id=shell.id,
        project_id=project.id,
        # The controller has already classified the Project path.  Do not let
        # kanban_db's legacy "project path means worktree" inference override
        # a deliberate durable-directory choice for a non-Git project.
        use_project_workspace=False,
        root_task_id=root_task_id,
        acceptance_criteria=_acceptance(acceptance_criteria, title),
        input_refs=refs,
        parents=parents,
        parent_link_type=relation_type,
        workspace_kind=kind,
        workspace_path=resolved_workspace_path,
        priority=int(priority or 0),
        idempotency_key=idempotency_key,
        session_id=(str(session_id).strip() if session_id else None),
        adapter_executor_id=(str(executor_id).strip() if executor_id else None),
        adapter_override_mode="once",
        adapter_reason=(
            "Project/Card Controller explicit executor selection"
            if executor_id
            else None
        ),
        adapter_created_by=created_by,
        created_by=created_by,
        board=_board_for_project(project),
    )
    task = kb.get_task(conn, task_id)
    if task is None:  # pragma: no cover - storage invariant
        raise RuntimeError(f"created card disappeared: {task_id}")
    return task


def _approval_payload(
    *,
    action: str,
    project: pdb.Project,
    title: str,
    shell_key: str,
    body: Optional[str] = None,
    source_task: Optional[kb.Task] = None,
    relation_type: str = "depends_on",
    acceptance_criteria: Optional[Iterable[Any]] = None,
    input_refs: Optional[Iterable[Any]] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    priority: int = 0,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    milestone: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "action": str(action).strip(),
        "project_id": project.id,
        "board": _board_for_project(project),
        "source_card_id": source_task.id if source_task else None,
        "relation_type": str(relation_type or "depends_on").strip(),
        "title": str(title or "").strip(),
        "body": str(body).strip() if body and str(body).strip() else None,
        "shell_key": str(shell_key or "").strip(),
        "acceptance_criteria": _acceptance(acceptance_criteria, title),
        "input_refs": _clean_list(input_refs),
        "workspace_kind": str(workspace_kind).strip() if workspace_kind else None,
        "workspace_path": str(workspace_path).strip() if workspace_path else None,
        "priority": int(priority or 0),
        "executor_id": str(executor_id).strip() if executor_id else None,
        "session_id": str(session_id).strip() if session_id else None,
        "created_by": str(created_by or "project-card-controller").strip(),
        "milestone": str(milestone).strip() if milestone else None,
    }


def _request_code_card(payload: dict[str, Any]) -> dict:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    dedupe = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with pdb.connect_closing() as conn:
        approval = pdb.create_card_approval(
            conn,
            project_id=payload["project_id"],
            action=payload["action"],
            source_card_id=payload.get("source_card_id"),
            title=payload["title"],
            shell_key=payload["shell_key"],
            request=payload,
            dedupe_key=dedupe,
            requested_by=payload.get("created_by"),
        )
        # A proposal is a real control-plane stop, not just a UI label.  Once a
        # governed card proposal exists every card-writing controller action is
        # rejected until an operator approves/rejects and resumes the Project.
        pdb.set_project_status(conn, payload["project_id"], "paused")
        current = pdb.get_project_progress(conn, payload["project_id"]) or {}
        phase = (
            "awaiting_direction_change_approval"
            if payload.get("action") == "direction_change"
            else "awaiting_code_approval"
        )
        pdb.upsert_project_progress(
            conn,
            payload["project_id"],
            phase=phase,
            milestone=payload.get("milestone") or current.get("milestone"),
            summary=f"Project card approval pending: {approval['id']}",
            next_action=f"Approve or reject {approval['id']}",
            last_card_id=current.get("last_card_id"),
            last_verified_card_id=current.get("last_verified_card_id"),
            counts=current.get("counts") or {},
        )
    return approval


def _create_or_request_card(
    *,
    conn,
    action: str,
    project: pdb.Project,
    title: str,
    shell_key: str,
    body: Optional[str] = None,
    source_task: Optional[kb.Task] = None,
    relation_type: str = "depends_on",
    acceptance_criteria: Optional[Iterable[Any]] = None,
    input_refs: Optional[Iterable[Any]] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    priority: int = 0,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    milestone: Optional[str] = None,
) -> tuple[Optional[kb.Task], Optional[dict]]:
    resolved_shell = _resolve_shell(conn, shell_key)
    if project.status != "active" and not (
        project.status == "paused" and resolved_shell.shell_key == "code"
    ):
        raise ProjectCardControllerError(
            f"project {project.id} is {project.status}; resume or reopen it before adding cards"
        )
    if resolved_shell.shell_key == "code":
        resolved_kind, resolved_path = _workspace_for_card(
            project=project,
            shell_key=resolved_shell.shell_key,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )
        payload = _approval_payload(
            action=action,
            project=project,
            title=title,
            body=body,
            shell_key=resolved_shell.shell_key,
            source_task=source_task,
            relation_type=relation_type,
            acceptance_criteria=acceptance_criteria,
            input_refs=input_refs,
            workspace_kind=resolved_kind,
            workspace_path=resolved_path,
            priority=priority,
            executor_id=executor_id,
            session_id=session_id,
            created_by=created_by,
            milestone=milestone,
        )
        return None, _request_code_card(payload)
    return (
        _create_card(
            conn=conn,
            project=project,
            title=title,
            body=body,
            shell_key=resolved_shell.shell_key,
            source_task=source_task,
            relation_type=relation_type,
            acceptance_criteria=acceptance_criteria,
            input_refs=input_refs,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            priority=priority,
            executor_id=executor_id,
            session_id=session_id,
            created_by=created_by,
        ),
        None,
    )


def _card_action_result(
    *,
    action: str,
    project: pdb.Project,
    board: str,
    card: Optional[kb.Task],
    approval: Optional[dict],
    source_card_id: Optional[str] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "action": action,
        "project_id": project.id,
        "board": board,
        "approval_required": approval is not None,
        "card": asdict(card) if card is not None else None,
        "approval_request": approval,
    }
    if source_card_id:
        result["source_card_id"] = source_card_id
    return result


def start_project(
    *,
    name: str,
    goal: str,
    shell_key: str,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    primary_path: Optional[str] = None,
    board: str = kb.DEFAULT_BOARD,
    acceptance_criteria: Optional[Iterable[Any]] = None,
    input_refs: Optional[Iterable[Any]] = None,
    priority: int = 0,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    milestone: Optional[str] = None,
    repo_mode: str = "none",
    github_repo: Optional[str] = None,
    repo_visibility: str = "private",
    default_branch: str = "main",
) -> dict[str, Any]:
    """Create an active project and either its first card or code approval."""
    name = str(name or "").strip()
    goal = str(goal or "").strip()
    if not name or not goal:
        raise ProjectCardControllerError("project name and goal are required")
    board_slug = kb._normalize_board_slug(board) or kb.DEFAULT_BOARD
    if board_slug == kb.DEFAULT_BOARD and not kb.board_exists(board_slug):
        # The default board is lazily created elsewhere in Hermes. Telegram can
        # be the first surface used on a fresh install, so initialise that one
        # canonical board here instead of requiring the web UI to be opened.
        with kb.connect_closing(board=board_slug):
            pass
    if not kb.board_exists(board_slug):
        raise ProjectCardControllerError(f"unknown board: {board_slug}")
    # Fail before writing the Project store when its requested Role Shell is
    # unavailable on the target board.
    with kb.connect_closing(board=board_slug) as conn:
        registry.ensure_schema(conn)
        _resolve_shell(conn, shell_key)
    with pdb.connect_closing() as project_conn:
        if slug and pdb.get_project(project_conn, slug) is not None:
            raise ProjectCardControllerError(f"project already exists: {slug}")
        project_id = pdb.create_project(
            project_conn,
            name=name,
            slug=slug,
            folders=[primary_path] if primary_path else None,
            primary_path=primary_path,
            description=description,
            board_slug=board_slug,
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None
    repository_result = configure_repository(
        project.id,
        mode=repo_mode,
        local_path=primary_path,
        github_repo=github_repo,
        visibility=repo_visibility,
        default_branch=default_branch,
    )
    # Repository setup can promote a supplied folder to the primary path.
    project = _project(project.id, include_completed=False)
    try:
        with kb.connect_closing(board=board_slug) as conn:
            registry.ensure_schema(conn)
            card, approval = _create_or_request_card(
                conn=conn,
                action="start_project",
                project=project,
                title=goal,
                body=description,
                shell_key=shell_key,
                acceptance_criteria=acceptance_criteria,
                input_refs=input_refs,
                priority=priority,
                executor_id=executor_id,
                session_id=session_id,
                created_by=created_by,
                milestone=milestone,
            )
    except Exception:
        # Repository provisioning may already have created durable local or
        # remote state. Keep the Project row as the recovery anchor instead of
        # deleting metadata and orphaning that state.
        with pdb.connect_closing() as project_conn:
            pdb.upsert_project_progress(
                project_conn,
                project_id,
                phase="blocked",
                milestone=milestone,
                summary="First card creation failed after project creation",
                next_action="Repair repository/card configuration and retry",
            )
        raise
    result = _card_action_result(
        action="start_project",
        project=project,
        board=board_slug,
        card=card,
        approval=approval,
    )
    # Code proposals pause the Project inside the Project DB. Return that
    # durable state instead of the stale pre-proposal dataclass.
    project = _project(project.id, include_completed=True)
    result["project"] = project.to_dict()
    result["repository"] = repository_result.get("repository")
    return result


def continue_card(
    card_id: str,
    *,
    title: str,
    body: Optional[str] = None,
    shell_key: Optional[str] = None,
    acceptance_criteria: Optional[Iterable[Any]] = None,
    input_refs: Optional[Iterable[Any]] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    priority: int = 0,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    milestone: Optional[str] = None,
) -> dict[str, Any]:
    located = locate_card(card_id)
    board = located["board"]
    source: kb.Task = located["task"]
    if not source.project_id:
        raise ProjectCardControllerError(
            f"card {source.id} is not attached to a Project"
        )
    project = _project(source.project_id, include_completed=True)
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        key = shell_key or _source_shell_key(conn, source)
        card, approval = _create_or_request_card(
            conn=conn,
            action="continue_card",
            project=project,
            source_task=source,
            relation_type="follows",
            title=title,
            body=body,
            shell_key=key,
            acceptance_criteria=acceptance_criteria,
            input_refs=input_refs,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            priority=priority,
            executor_id=executor_id,
            session_id=session_id,
            created_by=created_by,
            milestone=milestone,
        )
    return _card_action_result(
        action="continue_card",
        project=project,
        board=board,
        card=card,
        approval=approval,
        source_card_id=source.id,
    )


def request_direction_change(
    card_id: str,
    *,
    title: str,
    reason: str,
    body: Optional[str] = None,
    shell_key: Optional[str] = None,
    acceptance_criteria: Optional[Iterable[Any]] = None,
    input_refs: Optional[Iterable[Any]] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    priority: int = 0,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    milestone: Optional[str] = None,
) -> dict[str, Any]:
    """Stop one card and stage an operator-approved successor.

    This is deliberately not live prompt injection.  The source card is first
    archived, which closes its run and terminates any host-local worker.  Its
    Git workspace is then checkpointed when possible.  Only after that durable
    stop is a successor *proposal* persisted; no successor Kanban card exists
    until the operator consumes the ``pa_*`` approval.

    The archived source remains the immutable audit/checkpoint anchor.  The
    successor uses a non-blocking ``references`` link because a superseded card
    is not expected to complete and must not hold the new direction in ``todo``.
    """
    successor_title = str(title or "").strip()
    if not successor_title:
        raise ProjectCardControllerError("successor card title is required")
    direction_reason = str(reason or "").strip()
    if not direction_reason:
        raise ProjectCardControllerError("direction change reason is required")

    located = locate_card(card_id)
    board = located["board"]
    source: kb.Task = located["task"]
    if not source.project_id:
        raise ProjectCardControllerError(
            f"card {source.id} is not attached to a Project"
        )
    if source.status == "done":
        raise ProjectCardControllerError(
            f"card {source.id} is done; use continue_card for follow-up work"
        )
    project = _project(source.project_id, include_completed=False)

    successor_body = (
        f"Direction change from card {source.id}.\n"
        f"Reason: {direction_reason}"
    )
    if body and str(body).strip():
        successor_body += "\n\n" + str(body).strip()

    # Validate the complete successor proposal before stopping live work.  A
    # typo in a Role Shell or workspace must never cancel a healthy worker.
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        key = shell_key or _source_shell_key(conn, source)
        resolved_shell = _resolve_shell(conn, key)
        resolved_kind, resolved_path = _workspace_for_card(
            project=project,
            shell_key=resolved_shell.shell_key,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )
        payload = _approval_payload(
            action="direction_change",
            project=project,
            title=successor_title,
            body=successor_body,
            shell_key=resolved_shell.shell_key,
            source_task=source,
            relation_type="references",
            acceptance_criteria=acceptance_criteria,
            input_refs=input_refs,
            workspace_kind=resolved_kind,
            workspace_path=resolved_path,
            priority=priority,
            executor_id=executor_id,
            session_id=session_id,
            created_by=created_by,
            milestone=milestone,
        )
        source_status_before = source.status
        workspace = kb.resolve_workspace(source, board=board)
        git_root = kb._repo_root_for_worktree_target(workspace)
        # Close the project-level issue gate before touching the worker.  This
        # prevents a concurrent controller request from issuing another card
        # during the stop/checkpoint window.  A checkpoint failure deliberately
        # leaves the Project paused for operator repair.
        with pdb.connect_closing() as project_conn:
            pdb.set_project_status(project_conn, project.id, "paused")
            current = pdb.get_project_progress(project_conn, project.id) or {}
            pdb.upsert_project_progress(
                project_conn,
                project.id,
                phase="direction_change_checkpointing",
                milestone=milestone or current.get("milestone"),
                summary=f"Stopping and checkpointing card {source.id}",
                next_action="Wait for checkpoint or repair a checkpoint failure",
                last_card_id=current.get("last_card_id"),
                last_verified_card_id=current.get("last_verified_card_id"),
                counts=current.get("counts") or {},
            )
        if source.status != "archived" and not kb.archive_task(conn, source.id):
            raise ProjectCardControllerError(
                f"card {source.id} could not be stopped for direction change"
            )
        stopped = kb.get_task(conn, source.id)
        if stopped is None:
            raise ProjectCardControllerError(
                f"card {source.id} disappeared while stopping direction change"
            )
        if stopped.worker_pid is not None:
            kb.add_comment(
                conn,
                source.id,
                created_by,
                "Direction change archived this card, but worker termination "
                f"is still pending for PID {stopped.worker_pid}. The Project "
                "remains paused; no checkpoint or successor approval was created.",
            )
            raise ProjectCardControllerError(
                f"card {source.id} is archived but worker termination is still "
                "pending; no checkpoint or successor approval was created"
            )

    checkpoint: dict[str, Any]
    if git_root is None:
        checkpoint = {
            "status": "not_applicable",
            "reason": "card workspace is not inside a Git repository",
            "workspace": str(workspace),
        }
    else:
        try:
            checkpoint = checkpoint_card_git(
                source.id,
                message=(
                    f"{project.slug}: {source.id} direction-change checkpoint"
                ),
                push=False,
            )
            checkpoint["status"] = "committed"
        except Exception as exc:
            with kb.connect_closing(board=board) as conn:
                kb.add_comment(
                    conn,
                    source.id,
                    created_by,
                    "Direction change stopped this card, but the Git checkpoint "
                    f"failed: {exc}. Fix the checkpoint and request the successor "
                    "again; no successor approval was created.",
                )
            raise ProjectCardControllerError(
                "source card was stopped and preserved, but Git checkpoint failed; "
                "no successor approval was created: " + str(exc)
            ) from exc

    payload["direction_change"] = {
        "reason": direction_reason,
        "source_status_before": source_status_before,
        "source_status_after": "archived",
        "checkpoint": {
            key: checkpoint.get(key)
            for key in ("status", "workspace", "branch", "sha", "pushed", "reason")
            if checkpoint.get(key) is not None
        },
    }
    approval = _request_code_card(payload)
    with kb.connect_closing(board=board) as conn:
        kb.add_comment(
            conn,
            source.id,
            created_by,
            "\n".join(
                [
                    "Direction change checkpointed; original card preserved as archived.",
                    f"Project: {project.id}",
                    f"Source card: {source.id}",
                    f"Successor draft: {successor_title}",
                    f"Reason: {direction_reason}",
                    f"Approval required: {approval['id']}",
                    f"Checkpoint: {checkpoint.get('status')}",
                ]
            ),
        )
    progress = sync_project_progress(project.id, milestone=milestone)
    current_project = _project(project.id, include_completed=True)
    return {
        "schema": SCHEMA,
        "action": "request_direction_change",
        "project": current_project.to_dict(),
        "project_id": project.id,
        "board": board,
        "source_card_id": source.id,
        "source_status": "archived",
        "successor_card": None,
        "approval_required": True,
        "approval_request": approval,
        "checkpoint": checkpoint,
        "progress": progress,
    }


def add_project_card(
    project_id: str,
    *,
    title: str,
    shell_key: str,
    body: Optional[str] = None,
    acceptance_criteria: Optional[Iterable[Any]] = None,
    input_refs: Optional[Iterable[Any]] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    priority: int = 0,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    milestone: Optional[str] = None,
) -> dict[str, Any]:
    """Create an independent root-card thread inside an active Project."""
    project = _project(project_id, include_completed=True)
    board = _board_for_project(project)
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        card, approval = _create_or_request_card(
            conn=conn,
            action="add_project_card",
            project=project,
            title=title,
            body=body,
            shell_key=shell_key,
            acceptance_criteria=acceptance_criteria,
            input_refs=input_refs,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            priority=priority,
            executor_id=executor_id,
            session_id=session_id,
            created_by=created_by,
            milestone=milestone,
        )
    return _card_action_result(
        action="add_project_card",
        project=project,
        board=board,
        card=card,
        approval=approval,
    )


def split_card(
    card_id: str,
    *,
    cards: Iterable[dict[str, Any]],
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
) -> dict[str, Any]:
    items = list(cards or [])
    if not items:
        raise ProjectCardControllerError("split requires at least one child card")
    if len(items) > 20:
        raise ProjectCardControllerError("split is limited to 20 child cards")
    located = locate_card(card_id)
    board = located["board"]
    source: kb.Task = located["task"]
    if not source.project_id:
        raise ProjectCardControllerError(
            f"card {source.id} is not attached to a Project"
        )
    project = _project(source.project_id, include_completed=True)
    created: list[kb.Task] = []
    approvals: list[dict] = []
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        default_shell = _source_shell_key(conn, source)
        normalized: list[dict[str, Any]] = []
        # Validate the complete split before the first child becomes visible to
        # the dispatcher. Unexpected storage errors after validation are
        # compensated by deleting only cards created by this operation.
        for item in items:
            if not isinstance(item, dict):
                raise ProjectCardControllerError("each split card must be an object")
            title = str(item.get("title") or "").strip()
            if not title:
                raise ProjectCardControllerError("each split card needs a title")
            shell_key = str(item.get("shell_key") or default_shell)
            resolved_shell = _resolve_shell(conn, shell_key)
            workspace_kind = item.get("workspace_kind")
            if workspace_kind and workspace_kind not in kb.VALID_WORKSPACE_KINDS:
                raise ProjectCardControllerError(
                    f"workspace_kind must be one of {sorted(kb.VALID_WORKSPACE_KINDS)}"
                )
            normalized.append(
                dict(item, title=title, shell_key=resolved_shell.shell_key)
            )
        try:
            for item in normalized:
                card, approval = _create_or_request_card(
                    conn=conn,
                    action="split_card",
                    project=project,
                    source_task=source,
                    # Split children are parallel parts, not work that waits
                    # for the container card to become done.
                    relation_type="references",
                    title=item["title"],
                    body=item.get("body"),
                    shell_key=item["shell_key"],
                    acceptance_criteria=item.get("acceptance_criteria"),
                    input_refs=item.get("input_refs"),
                    workspace_kind=item.get("workspace_kind"),
                    workspace_path=item.get("workspace_path"),
                    priority=int(item.get("priority") or 0),
                    executor_id=item.get("executor_id"),
                    session_id=session_id,
                    created_by=created_by,
                    milestone=item.get("milestone"),
                )
                if card is not None:
                    created.append(card)
                if approval is not None:
                    approvals.append(approval)
        except Exception:
            for task in reversed(created):
                try:
                    kb.delete_task(conn, task.id)
                except Exception:
                    pass
            if approvals:
                with pdb.connect_closing() as project_conn:
                    for approval in approvals:
                        pdb.update_card_approval(
                            project_conn,
                            approval["id"],
                            status="rejected",
                            decided_by=created_by,
                            decision_reason="split creation rolled back",
                        )
            raise
    return {
        "schema": SCHEMA,
        "action": "split_card",
        "project_id": project.id,
        "board": board,
        "source_card_id": source.id,
        "cards": [asdict(card) for card in created],
        "approval_required": bool(approvals),
        "approval_requests": approvals,
    }


def verify_card(
    card_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    acceptance_criteria: Optional[Iterable[Any]] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
) -> dict[str, Any]:
    located = locate_card(card_id)
    board = located["board"]
    source: kb.Task = located["task"]
    if not source.project_id:
        raise ProjectCardControllerError(
            f"card {source.id} is not attached to a Project"
        )
    project = _project(source.project_id, include_completed=False)
    verify_title = str(title or f"Verify {source.id}: {source.title}").strip()
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        card = _create_card(
            conn=conn,
            project=project,
            source_task=source,
            relation_type="reviews",
            title=verify_title,
            body=body,
            shell_key="verification",
            acceptance_criteria=acceptance_criteria,
            session_id=session_id,
            created_by=created_by,
        )
    return {
        "schema": SCHEMA,
        "action": "verify_card",
        "project_id": project.id,
        "board": board,
        "source_card_id": source.id,
        "card": asdict(card),
    }


def recover_card(
    card_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    shell_key: Optional[str] = None,
    acceptance_criteria: Optional[Iterable[Any]] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    executor_id: Optional[str] = None,
    session_id: Optional[str] = None,
    created_by: str = "project-card-controller",
    milestone: Optional[str] = None,
) -> dict[str, Any]:
    located = locate_card(card_id)
    board = located["board"]
    source: kb.Task = located["task"]
    if not source.project_id:
        raise ProjectCardControllerError(
            f"card {source.id} is not attached to a Project"
        )
    if source.status == "running":
        raise ProjectCardControllerError(
            f"card {source.id} is still running; reclaim or block it before recovery"
        )
    if source.status == "done":
        raise ProjectCardControllerError(
            f"card {source.id} is done; use continue_card or verify_card"
        )
    project = _project(source.project_id, include_completed=True)
    recovery_title = str(title or f"Recover {source.id}: {source.title}").strip()
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        key = shell_key or _source_shell_key(conn, source)
        card, approval = _create_or_request_card(
            conn=conn,
            action="recover_card",
            project=project,
            source_task=source,
            relation_type="recovers",
            title=recovery_title,
            body=body,
            shell_key=key,
            acceptance_criteria=acceptance_criteria,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            executor_id=executor_id,
            session_id=session_id,
            created_by=created_by,
            milestone=milestone,
        )
        recovery_sources = []
        if card is not None:
            recovery_sources = registry.register_task_recovery_sources(
                conn,
                recovery_task_id=card.id,
                source_task_ids=[source.id],
                created_by=created_by,
            )
    result = _card_action_result(
        action="recover_card",
        project=project,
        board=board,
        card=card,
        approval=approval,
        source_card_id=source.id,
    )
    result["recovery_sources"] = recovery_sources
    return result


def approve_code_card(
    approval_id: str,
    *,
    decided_by: str = "operator",
    decision_reason: Optional[str] = None,
) -> dict[str, Any]:
    """Consume one operator-approved proposal and create the code card once."""
    approval_key = str(approval_id or "").strip()
    if not approval_key:
        raise ProjectCardControllerError("approval_id is required")
    with pdb.connect_closing() as project_conn:
        approval = pdb.get_card_approval(project_conn, approval_key)
        if approval is None:
            raise ProjectCardControllerError(
                f"unknown project card approval: {approval_key}"
            )
        if approval["status"] == "rejected":
            raise ProjectCardControllerError(
                f"approval {approval_key} was rejected"
            )
        if approval["status"] == "consumed" and approval.get("created_task_id"):
            located = locate_card(str(approval["created_task_id"]))
            return {
                "schema": SCHEMA,
                "action": "approve_code_card",
                "project_id": approval["project_id"],
                "board": located["board"],
                "approval_required": False,
                "approval_request": approval,
                "card": asdict(located["task"]),
                "idempotent_replay": True,
            }
        project_row = pdb.get_project(project_conn, str(approval["project_id"]))
        if project_row is None:
            raise ProjectCardControllerError(
                f"unknown project: {approval['project_id']}"
            )
        if project_row.status == "completed":
            raise ProjectCardControllerError(
                f"project {project_row.id} is completed; reopen it before approving cards"
            )
        approval = pdb.update_card_approval(
            project_conn,
            approval_key,
            status="approved",
            decided_by=decided_by,
            decision_reason=decision_reason,
        )
    assert approval is not None
    payload = approval.get("request") or {}
    project = _project(str(approval["project_id"]), include_completed=True)
    with pdb.connect_closing() as project_conn:
        pdb.set_project_status(project_conn, project.id, "active")
    project = _project(project.id, include_completed=False)
    board = str(payload.get("board") or _board_for_project(project))
    source = None
    source_id = str(payload.get("source_card_id") or "").strip()
    try:
        with kb.connect_closing(board=board) as conn:
            registry.ensure_schema(conn)
            if source_id:
                source = kb.get_task(conn, source_id)
                if source is None:
                    raise ProjectCardControllerError(
                        f"approval source card not found: {source_id}"
                    )
                if source.project_id != project.id:
                    raise ProjectCardControllerError(
                        f"approval source card belongs to another project: {source_id}"
                    )
            requested_shell = str(
                payload.get("shell_key") or approval.get("shell_key") or "code"
            ).strip()
            if payload.get("action") == "direction_change" and source is not None:
                if source.status != "archived":
                    raise ProjectCardControllerError(
                        "direction-change source must remain archived until approval"
                    )
            card = _create_card(
                conn=conn,
                project=project,
                source_task=source,
                relation_type=str(payload.get("relation_type") or "depends_on"),
                title=str(payload.get("title") or approval["title"]),
                body=payload.get("body"),
                shell_key=requested_shell,
                acceptance_criteria=payload.get("acceptance_criteria"),
                input_refs=payload.get("input_refs"),
                workspace_kind=payload.get("workspace_kind"),
                workspace_path=payload.get("workspace_path"),
                priority=int(payload.get("priority") or 0),
                executor_id=payload.get("executor_id"),
                session_id=payload.get("session_id"),
                created_by=str(payload.get("created_by") or decided_by),
                idempotency_key=f"project-approval:{approval_key}",
            )
            if payload.get("action") == "recover_card" and source is not None:
                registry.register_task_recovery_sources(
                    conn,
                    recovery_task_id=card.id,
                    source_task_ids=[source.id],
                    created_by=str(payload.get("created_by") or decided_by),
                )
    except Exception:
        # Approval without card creation is not permission to continue. Restore
        # the hard stop so a transient Git/workspace failure cannot reopen the
        # project behind the operator's back.
        with pdb.connect_closing() as project_conn:
            pdb.set_project_status(project_conn, project.id, "paused")
            pdb.update_card_approval(
                project_conn,
                approval_key,
                status="pending",
                decided_by=decided_by,
                decision_reason="card creation failed; approval reset to pending",
            )
        raise
    with pdb.connect_closing() as project_conn:
        consumed = pdb.update_card_approval(
            project_conn,
            approval_key,
            status="consumed",
            decided_by=decided_by,
            decision_reason=decision_reason,
            created_task_id=card.id,
        )
    sync_project_progress(project.id, milestone=payload.get("milestone"))
    return {
        "schema": SCHEMA,
        "action": "approve_code_card",
        "project_id": project.id,
        "board": board,
        "approval_required": False,
        "approval_request": consumed,
        "card": asdict(card),
        "idempotent_replay": False,
    }


def reject_code_card(
    approval_id: str,
    *,
    decided_by: str = "operator",
    decision_reason: Optional[str] = None,
) -> dict[str, Any]:
    approval_key = str(approval_id or "").strip()
    with pdb.connect_closing() as conn:
        approval = pdb.get_card_approval(conn, approval_key)
        if approval is None:
            raise ProjectCardControllerError(
                f"unknown project card approval: {approval_key}"
            )
        if approval["status"] == "consumed":
            raise ProjectCardControllerError(
                f"approval {approval_key} already created card "
                f"{approval.get('created_task_id')}"
            )
        rejected = pdb.update_card_approval(
            conn,
            approval_key,
            status="rejected",
            decided_by=decided_by,
            decision_reason=decision_reason,
        )
        remaining = pdb.list_card_approvals(
            conn,
            str(approval["project_id"]),
            statuses=("pending", "approved"),
        )
        if not remaining:
            project = pdb.get_project(conn, str(approval["project_id"]))
            if project is not None and project.status == "paused":
                pdb.set_project_status(conn, project.id, "active")
    sync_project_progress(str(approval["project_id"]))
    return {
        "schema": SCHEMA,
        "action": "reject_code_card",
        "project_id": approval["project_id"],
        "approval_required": False,
        "approval_request": rejected,
        "card": None,
    }


def approve_project_card(
    approval_id: str,
    *,
    decided_by: str = "operator",
    decision_reason: Optional[str] = None,
) -> dict[str, Any]:
    """Approve any governed card proposal; code-card name remains compatible."""
    result = approve_code_card(
        approval_id,
        decided_by=decided_by,
        decision_reason=decision_reason,
    )
    result["action"] = "approve_project_card"
    return result


def reject_project_card(
    approval_id: str,
    *,
    decided_by: str = "operator",
    decision_reason: Optional[str] = None,
) -> dict[str, Any]:
    """Reject any governed card proposal; code-card name remains compatible."""
    result = reject_code_card(
        approval_id,
        decided_by=decided_by,
        decision_reason=decision_reason,
    )
    result["action"] = "reject_project_card"
    return result


def list_code_card_approvals(
    project_id: str,
    *,
    include_decided: bool = False,
) -> dict[str, Any]:
    project = _project(project_id, include_completed=True)
    statuses = None if include_decided else ("pending", "approved")
    with pdb.connect_closing() as conn:
        approvals = pdb.list_card_approvals(
            conn,
            project.id,
            statuses=statuses,
        )
    return {
        "schema": SCHEMA,
        "action": "list_code_card_approvals",
        "project_id": project.id,
        "approval_requests": approvals,
    }


def sync_project_progress(
    project_id: str,
    *,
    milestone: Optional[str] = None,
) -> dict:
    """Persist the Kanban projection in the separate per-profile Project DB."""
    project = _project(project_id, include_completed=True, include_archived=True)
    board = _board_for_project(project)
    with kb.connect_closing(board=board) as conn:
        rows = conn.execute(
            "SELECT id, status, role_shell_id, created_at FROM tasks "
            "WHERE project_id = ? ORDER BY created_at, rowid",
            (project.id,),
        ).fetchall()
        counts: dict[str, int] = {}
        last_card_id = None
        last_verified_card_id = None
        for row in rows:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
            last_card_id = str(row["id"])
            if row["role_shell_id"]:
                shell = registry.get_shell(conn, shell_id=row["role_shell_id"])
                if shell and shell.shell_key == "verification" and status == "done":
                    last_verified_card_id = str(row["id"])
    with pdb.connect_closing() as project_conn:
        pending = pdb.list_card_approvals(
            project_conn,
            project.id,
            statuses=("pending", "approved"),
        )
        previous = pdb.get_project_progress(project_conn, project.id) or {}
        approval_phase = (
            "awaiting_direction_change_approval"
            if pending and pending[0].get("action") == "direction_change"
            else "awaiting_code_approval"
        )
        if project.status == "completed":
            phase = "completed"
            next_action = None
        elif project.status == "paused" and pending:
            phase = approval_phase
            next_action = f"Approve or reject {pending[0]['id']}"
        elif project.status == "paused":
            phase = "paused"
            next_action = "Resume the project when the operator is ready"
        elif pending:
            phase = approval_phase
            next_action = f"Approve or reject {pending[0]['id']}"
        elif counts.get("blocked", 0):
            phase = "blocked"
            next_action = "Resolve or recover blocked cards"
        elif counts.get("running", 0):
            phase = "active"
            next_action = "Wait for running cards or inspect progress"
        elif any(counts.get(status, 0) for status in ACTIVE_CARD_STATUSES):
            phase = "active"
            next_action = "Dispatch or complete open cards"
        elif rows:
            phase = "awaiting_next_step"
            next_action = "Propose the next milestone or close the project"
        else:
            phase = "planning"
            next_action = "Create the first project card"
        progress = pdb.upsert_project_progress(
            project_conn,
            project.id,
            phase=phase,
            milestone=milestone or previous.get("milestone"),
            summary=(
                f"{len(rows)} cards; {len(pending)} code approvals pending"
            ),
            next_action=next_action,
            last_card_id=last_card_id,
            last_verified_card_id=last_verified_card_id,
            counts=counts,
        )
    progress["pending_approval_count"] = len(pending)
    return progress


_REPO_MODE_VALUES = frozenset({"none", "existing", "init_local", "github"})
_REPO_VISIBILITY_VALUES = frozenset({"private", "public"})


def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if allow_failure:
            return subprocess.CompletedProcess(argv, 1, "", str(exc))
        raise ProjectCardControllerError(f"command failed: {argv[0]}: {exc}") from exc
    if result.returncode != 0 and not allow_failure:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise ProjectCardControllerError(
            f"command failed ({' '.join(argv[:3])}): {detail}"
        )
    return result


def _git_text(path: Path, *args: str, allow_failure: bool = False) -> str:
    result = _run_process(
        ["git", "-C", str(path), *args],
        cwd=path,
        allow_failure=allow_failure,
    )
    return str(result.stdout or "").strip() if result.returncode == 0 else ""


def _repository_snapshot(path: Path) -> dict[str, Optional[str]]:
    return {
        "current_branch": _git_text(path, "branch", "--show-current", allow_failure=True) or None,
        "head_sha": _git_text(path, "rev-parse", "HEAD", allow_failure=True) or None,
        "remote_url": _git_text(
            path, "remote", "get-url", "origin", allow_failure=True
        ) or None,
    }


def configure_repository(
    project_id: str,
    *,
    mode: str,
    local_path: Optional[str] = None,
    github_repo: Optional[str] = None,
    visibility: str = "private",
    default_branch: str = "main",
) -> dict[str, Any]:
    """Connect, initialise, or create the Project's governed repository."""
    project = _project(project_id, include_completed=False)
    normalized_mode = str(mode or "none").strip().lower()
    if normalized_mode not in _REPO_MODE_VALUES:
        raise ProjectCardControllerError(
            f"repository mode must be one of {sorted(_REPO_MODE_VALUES)}"
        )
    branch = str(default_branch or "main").strip()
    if not branch or any(ch.isspace() for ch in branch) or branch.startswith("-"):
        raise ProjectCardControllerError("invalid default branch")
    normalized_visibility = str(visibility or "private").strip().lower()
    if normalized_visibility not in _REPO_VISIBILITY_VALUES:
        raise ProjectCardControllerError("visibility must be private or public")
    if normalized_mode == "none":
        with pdb.connect_closing() as conn:
            repository = pdb.upsert_project_repository(
                conn,
                project.id,
                mode="none",
                status="disabled",
                local_path=project.primary_path,
                visibility=None,
                last_error=None,
            )
        return {
            "schema": SCHEMA,
            "action": "configure_repository",
            "project_id": project.id,
            "repository": repository,
        }

    path_text = str(local_path or project.primary_path or "").strip()
    if not path_text:
        raise ProjectCardControllerError(
            "repository setup requires local_path or Project primary_path"
        )
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise ProjectCardControllerError("repository local_path must be absolute")
    path.mkdir(parents=True, exist_ok=True)
    path = path.resolve(strict=False)
    if shutil.which("git") is None:
        raise ProjectCardControllerError("git executable is not installed")

    with pdb.connect_closing() as conn:
        if project.primary_path != str(path):
            pdb.add_folder(conn, project.id, str(path), is_primary=True)
        pdb.upsert_project_repository(
            conn,
            project.id,
            mode=normalized_mode,
            provider="github" if normalized_mode == "github" else "git",
            local_path=str(path),
            remote_name=str(github_repo).strip() if github_repo else None,
            visibility=(normalized_visibility if normalized_mode == "github" else None),
            default_branch=branch,
            integration_branch=f"integration/{project.slug}",
            status="provisioning",
            last_error=None,
        )
    try:
        repo_root = kb._repo_root_for_worktree_target(path)
        if normalized_mode == "existing" and repo_root is None:
            raise ProjectCardControllerError(
                f"existing repository path is not inside Git: {path}"
            )
        if repo_root is None:
            _run_process(["git", "init", "-b", branch, str(path)], cwd=path)
            repo_root = path
        else:
            path = repo_root

        if not _git_text(path, "rev-parse", "HEAD", allow_failure=True):
            _run_process(
                [
                    "git", "-C", str(path),
                    "-c", "user.name=Hermes Project Controller",
                    "-c", "user.email=hermes@localhost",
                    "commit", "--allow-empty", "-m",
                    "chore: initialize project repository",
                ],
                cwd=path,
            )

        if normalized_mode == "github":
            if shutil.which("gh") is None:
                raise ProjectCardControllerError("GitHub CLI (gh) is not installed")
            repo_name = str(github_repo or project.slug).strip()
            if (
                not repo_name
                or repo_name.startswith("-")
                or repo_name.count("/") > 1
                or any(
                    not part
                    or not all(ch.isalnum() or ch in "._-" for ch in part)
                    for part in repo_name.split("/")
                )
            ):
                raise ProjectCardControllerError("invalid GitHub repository name")
            origin = _git_text(
                path, "remote", "get-url", "origin", allow_failure=True
            )
            if not origin:
                _run_process(
                    [
                        "gh", "repo", "create", repo_name, "--source", str(path),
                        "--remote", "origin", f"--{normalized_visibility}",
                    ],
                    cwd=path,
                )
            _run_process(
                ["git", "-C", str(path), "push", "-u", "origin", branch],
                cwd=path,
            )

        snapshot = _repository_snapshot(path)
        with pdb.connect_closing() as conn:
            repository = pdb.upsert_project_repository(
                conn,
                project.id,
                mode=normalized_mode,
                provider="github" if normalized_mode == "github" else "git",
                local_path=str(path),
                remote_name=(str(github_repo or project.slug) if normalized_mode == "github" else None),
                remote_url=snapshot["remote_url"],
                visibility=(normalized_visibility if normalized_mode == "github" else None),
                default_branch=branch,
                integration_branch=f"integration/{project.slug}",
                status="ready",
                last_error=None,
                current_branch=snapshot["current_branch"],
                base_sha=snapshot["head_sha"],
                head_sha=snapshot["head_sha"],
                last_pushed_sha=(snapshot["head_sha"] if normalized_mode == "github" else None),
            )
            event = pdb.record_git_event(
                conn,
                project_id=project.id,
                event_type="repository_configured",
                branch=snapshot["current_branch"],
                sha=snapshot["head_sha"],
                remote=snapshot["remote_url"],
                status="succeeded",
                detail=f"mode={normalized_mode}",
            )
    except Exception as exc:
        with pdb.connect_closing() as conn:
            pdb.upsert_project_repository(
                conn,
                project.id,
                mode=normalized_mode,
                local_path=str(path),
                status="error",
                last_error=str(exc),
            )
            pdb.record_git_event(
                conn,
                project_id=project.id,
                event_type="repository_configured",
                status="failed",
                detail=str(exc),
            )
        raise
    return {
        "schema": SCHEMA,
        "action": "configure_repository",
        "project_id": project.id,
        "repository": repository,
        "git_event": event,
    }


def checkpoint_card_git(
    card_id: str,
    *,
    message: Optional[str] = None,
    push: bool = False,
) -> dict[str, Any]:
    """Commit a card worktree and optionally push only its non-main branch."""
    located = locate_card(card_id)
    board = located["board"]
    task: kb.Task = located["task"]
    if not task.project_id:
        raise ProjectCardControllerError(f"card {task.id} is not in a Project")
    project = _project(task.project_id, include_completed=True)
    workspace = kb.resolve_workspace(task, board=board)
    repo_root = kb._repo_root_for_worktree_target(workspace)
    if repo_root is None:
        raise ProjectCardControllerError(
            f"card workspace is not inside a Git repository: {workspace}"
        )
    changed = _git_text(workspace, "status", "--porcelain", allow_failure=True)
    protected = []
    for line in changed.splitlines():
        candidate = line[3:].strip().lower() if len(line) > 3 else ""
        if candidate == ".env" or candidate.endswith("/.env") or "credential" in candidate:
            protected.append(candidate)
    if protected:
        raise ProjectCardControllerError(
            "refusing to auto-stage protected files: " + ", ".join(protected)
        )
    if changed:
        _run_process(["git", "-C", str(workspace), "add", "-A"], cwd=workspace)
        commit_message = str(message or f"{project.slug}: {task.id} {task.title}").strip()
        _run_process(
            ["git", "-C", str(workspace), "commit", "-m", commit_message],
            cwd=workspace,
        )
    branch = _git_text(workspace, "branch", "--show-current")
    sha = _git_text(workspace, "rev-parse", "HEAD")
    remote = _git_text(
        workspace, "remote", "get-url", "origin", allow_failure=True
    ) or None
    pushed = False
    if push:
        with pdb.connect_closing() as conn:
            repository = pdb.get_project_repository(conn, project.id) or {}
        protected_branches = {
            str(repository.get("default_branch") or "main"), "main", "master"
        }
        if branch in protected_branches:
            raise ProjectCardControllerError(
                "direct project default-branch push is forbidden; use a card branch"
            )
        if not remote:
            raise ProjectCardControllerError("card repository has no origin remote")
        _run_process(
            ["git", "-C", str(workspace), "push", "-u", "origin", branch],
            cwd=workspace,
        )
        pushed = True
    with pdb.connect_closing() as conn:
        repository = pdb.upsert_project_repository(
            conn,
            project.id,
            local_path=str(repo_root),
            status="ready",
            current_branch=branch,
            head_sha=sha,
            last_card_id=task.id,
            last_commit_sha=sha,
            last_pushed_sha=sha if pushed else None,
        )
        event = pdb.record_git_event(
            conn,
            project_id=project.id,
            card_id=task.id,
            event_type="card_branch_pushed" if pushed else "card_committed",
            branch=branch,
            sha=sha,
            remote=remote,
            status="succeeded",
            detail="clean checkpoint" if not changed else "changes committed",
        )
    return {
        "schema": SCHEMA,
        "action": "checkpoint_card_git",
        "project_id": project.id,
        "board": board,
        "card_id": task.id,
        "workspace": str(workspace),
        "branch": branch,
        "sha": sha,
        "pushed": pushed,
        "repository": repository,
        "git_event": event,
    }


def inspect_card(card_id: str, *, board: Optional[str] = None) -> dict[str, Any]:
    located = locate_card(card_id, board=board)
    slug = located["board"]
    task: kb.Task = located["task"]
    with kb.connect_closing(board=slug) as conn:
        registry.ensure_schema(conn)
        links = kb.task_link_details(conn, task.id)
        recovery_sources = [
            str(item["source_task_id"])
            for item in registry.list_task_recovery_sources(conn, task.id)
        ]
        runs = [asdict(run) for run in kb.list_runs(conn, task.id)]
        adapter = registry.inspect_task_adapter(conn, task.id)
    project = None
    if task.project_id:
        with pdb.connect_closing() as project_conn:
            item = pdb.get_project(project_conn, task.project_id)
            project = item.to_dict() if item else None
    return {
        "schema": SCHEMA,
        "action": "inspect_card",
        "board": slug,
        "project": project,
        "card": asdict(task),
        "links": links,
        "recovery_source_task_ids": recovery_sources,
        "runs": runs,
        "adapter": adapter,
    }


def inspect_project(
    project_id: str,
    *,
    include_archived: bool = False,
) -> dict[str, Any]:
    project = _project(project_id, include_archived=include_archived)
    board = _board_for_project(project)
    with kb.connect_closing(board=board) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at, rowid",
            (project.id,),
        ).fetchall()
        cards = [kb.Task.from_row(row) for row in rows]
        counts: dict[str, int] = {}
        for card in cards:
            counts[card.status] = counts.get(card.status, 0) + 1
    roots: dict[str, list[str]] = {}
    for card in cards:
        roots.setdefault(card.root_task_id or card.id, []).append(card.id)
    progress = sync_project_progress(project.id)
    with pdb.connect_closing() as project_conn:
        approvals = pdb.list_card_approvals(
            project_conn,
            project.id,
            statuses=("pending", "approved"),
        )
        repository = pdb.get_project_repository(project_conn, project.id)
        git_events = pdb.list_git_events(project_conn, project.id, limit=20)
    return {
        "schema": SCHEMA,
        "action": "inspect_project",
        "project": project.to_dict(),
        "board": board,
        "counts": counts,
        "progress": progress,
        "approval_requests": approvals,
        "repository": repository,
        "git_events": git_events,
        "threads": [
            {"root_card_id": root_id, "card_ids": ids}
            for root_id, ids in roots.items()
        ],
        "cards": [asdict(card) for card in cards],
    }


def list_projects(*, include_archived: bool = False) -> dict[str, Any]:
    with pdb.connect_closing() as conn:
        projects = pdb.list_projects(conn, include_archived=include_archived)
    rows: list[dict[str, Any]] = []
    for project in projects:
        summary = inspect_project(
            project.id,
            include_archived=include_archived,
        )
        item = project.to_dict()
        item["board"] = summary["board"]
        item["counts"] = summary["counts"]
        item["thread_count"] = len(summary["threads"])
        item["card_count"] = len(summary["cards"])
        item["progress"] = summary["progress"]
        item["pending_approval_count"] = len(summary["approval_requests"])
        item["approval_requests"] = summary["approval_requests"]
        item["repository"] = summary["repository"]
        rows.append(item)
    return {"schema": SCHEMA, "action": "list_projects", "projects": rows}


def close_project(project_id: str) -> dict[str, Any]:
    project = _project(project_id)
    board = _board_for_project(project)
    with kb.connect_closing(board=board) as conn:
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE project_id = ? "
            "AND status NOT IN ('done', 'archived') ORDER BY created_at, id",
            (project.id,),
        ).fetchall()
    if rows:
        raise ProjectCardControllerError(
            "project has open cards: "
            + ", ".join(f"{row['id']}({row['status']})" for row in rows[:20])
        )
    with pdb.connect_closing() as conn:
        pending = pdb.list_card_approvals(
            conn,
            project.id,
            statuses=("pending", "approved"),
        )
    if pending:
        raise ProjectCardControllerError(
            "project has pending code-card approvals: "
            + ", ".join(item["id"] for item in pending[:20])
        )
    with pdb.connect_closing() as conn:
        if not pdb.set_project_status(conn, project.id, "completed"):
            raise ProjectCardControllerError(f"project not found: {project.id}")
        closed = pdb.get_project(conn, project.id)
    progress = sync_project_progress(project.id)
    return {
        "schema": SCHEMA,
        "action": "close_project",
        "project": closed.to_dict() if closed else None,
        "board": board,
        "progress": progress,
    }


def reopen_project(project_id: str) -> dict[str, Any]:
    project = _project(project_id)
    with pdb.connect_closing() as conn:
        if not pdb.set_project_status(conn, project.id, "active"):
            raise ProjectCardControllerError(f"project not found: {project.id}")
        reopened = pdb.get_project(conn, project.id)
    progress = sync_project_progress(project.id)
    return {
        "schema": SCHEMA,
        "action": "reopen_project",
        "project": reopened.to_dict() if reopened else None,
        "board": _board_for_project(project),
        "progress": progress,
    }


def pause_project(project_id: str) -> dict[str, Any]:
    """Apply a durable operator stop without pretending the Project is done."""
    project = _project(project_id)
    if project.status == "completed":
        raise ProjectCardControllerError(
            f"project {project.id} is completed; reopen it before pausing"
        )
    board = _board_for_project(project)
    with kb.connect_closing(board=board) as conn:
        running = conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND status = 'running' "
            "ORDER BY created_at, id",
            (project.id,),
        ).fetchall()
    if running:
        raise ProjectCardControllerError(
            "project has running cards; archive or reclaim them before pausing: "
            + ", ".join(str(row["id"]) for row in running[:20])
        )
    with pdb.connect_closing() as conn:
        pdb.set_project_status(conn, project.id, "paused")
        pdb.set_active(conn, None)
        paused = pdb.get_project(conn, project.id)
    progress = sync_project_progress(project.id)
    return {
        "schema": SCHEMA,
        "action": "pause_project",
        "project": paused.to_dict() if paused else None,
        "board": board,
        "progress": progress,
    }
