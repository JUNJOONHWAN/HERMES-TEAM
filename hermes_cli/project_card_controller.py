"""Deterministic Project/Card Controller for governed Hermes work.

This module is control-plane code, not an execution adapter.  It is the sole
writer for the high-level project/card operations exposed to web and messaging
surfaces.  Role adapters still execute cards; they never own project lifecycle
or mutate the graph directly.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_cli import supervisor_registry as registry


SCHEMA = "hermes.project-card-controller.v1"
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
            f"project {project.id} is {project.status}; reopen it before adding cards"
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
) -> kb.Task:
    title = str(title or "").strip()
    if not title:
        raise ProjectCardControllerError("card title is required")
    shell = _resolve_shell(conn, shell_key)
    use_project_workspace = shell.shell_key == "code" and bool(project.primary_path)
    kind = str(workspace_kind or ("worktree" if use_project_workspace else "scratch"))
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
        use_project_workspace=use_project_workspace,
        root_task_id=root_task_id,
        acceptance_criteria=_acceptance(acceptance_criteria, title),
        input_refs=refs,
        parents=parents,
        parent_link_type=relation_type,
        workspace_kind=kind,
        workspace_path=(str(workspace_path).strip() if workspace_path else None),
        priority=int(priority or 0),
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
) -> dict[str, Any]:
    """Create an active project and its first root card."""
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
    try:
        with kb.connect_closing(board=board_slug) as conn:
            registry.ensure_schema(conn)
            card = _create_card(
                conn=conn,
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
            )
    except Exception:
        # Compensate only the metadata row created by this call. No existing
        # board or operator state is deleted.
        with pdb.connect_closing() as project_conn:
            pdb.delete_project(project_conn, project_id)
        raise
    return {
        "schema": SCHEMA,
        "action": "start_project",
        "project": project.to_dict(),
        "card": asdict(card),
        "board": board_slug,
    }


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
) -> dict[str, Any]:
    located = locate_card(card_id)
    board = located["board"]
    source: kb.Task = located["task"]
    if not source.project_id:
        raise ProjectCardControllerError(
            f"card {source.id} is not attached to a Project"
        )
    project = _project(source.project_id, include_completed=False)
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        key = shell_key or _source_shell_key(conn, source)
        card = _create_card(
            conn=conn,
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
        )
    return {
        "schema": SCHEMA,
        "action": "continue_card",
        "project_id": project.id,
        "board": board,
        "source_card_id": source.id,
        "card": asdict(card),
    }


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
    project = _project(source.project_id, include_completed=False)
    created: list[kb.Task] = []
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
            _resolve_shell(conn, shell_key)
            workspace_kind = item.get("workspace_kind")
            if workspace_kind and workspace_kind not in kb.VALID_WORKSPACE_KINDS:
                raise ProjectCardControllerError(
                    f"workspace_kind must be one of {sorted(kb.VALID_WORKSPACE_KINDS)}"
                )
            normalized.append(dict(item, title=title, shell_key=shell_key))
        try:
            for item in normalized:
                created.append(
                    _create_card(
                        conn=conn,
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
                    )
                )
        except Exception:
            for task in reversed(created):
                try:
                    kb.delete_task(conn, task.id)
                except Exception:
                    pass
            raise
    return {
        "schema": SCHEMA,
        "action": "split_card",
        "project_id": project.id,
        "board": board,
        "source_card_id": source.id,
        "cards": [asdict(card) for card in created],
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
    executor_id: Optional[str] = None,
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
    if source.status == "running":
        raise ProjectCardControllerError(
            f"card {source.id} is still running; reclaim or block it before recovery"
        )
    if source.status == "done":
        raise ProjectCardControllerError(
            f"card {source.id} is done; use continue_card or verify_card"
        )
    project = _project(source.project_id, include_completed=False)
    recovery_title = str(title or f"Recover {source.id}: {source.title}").strip()
    with kb.connect_closing(board=board) as conn:
        registry.ensure_schema(conn)
        key = shell_key or _source_shell_key(conn, source)
        card = _create_card(
            conn=conn,
            project=project,
            source_task=source,
            relation_type="recovers",
            title=recovery_title,
            body=body,
            shell_key=key,
            acceptance_criteria=acceptance_criteria,
            executor_id=executor_id,
            session_id=session_id,
            created_by=created_by,
        )
        recovery_sources = registry.register_task_recovery_sources(
            conn,
            recovery_task_id=card.id,
            source_task_ids=[source.id],
            created_by=created_by,
        )
    return {
        "schema": SCHEMA,
        "action": "recover_card",
        "project_id": project.id,
        "board": board,
        "source_card_id": source.id,
        "recovery_sources": recovery_sources,
        "card": asdict(card),
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
    return {
        "schema": SCHEMA,
        "action": "inspect_project",
        "project": project.to_dict(),
        "board": board,
        "counts": counts,
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
        if not pdb.set_project_status(conn, project.id, "completed"):
            raise ProjectCardControllerError(f"project not found: {project.id}")
        closed = pdb.get_project(conn, project.id)
    return {
        "schema": SCHEMA,
        "action": "close_project",
        "project": closed.to_dict() if closed else None,
        "board": board,
    }


def reopen_project(project_id: str) -> dict[str, Any]:
    project = _project(project_id)
    with pdb.connect_closing() as conn:
        if not pdb.set_project_status(conn, project.id, "active"):
            raise ProjectCardControllerError(f"project not found: {project.id}")
        reopened = pdb.get_project(conn, project.id)
    return {
        "schema": SCHEMA,
        "action": "reopen_project",
        "project": reopened.to_dict() if reopened else None,
        "board": _board_for_project(project),
    }
