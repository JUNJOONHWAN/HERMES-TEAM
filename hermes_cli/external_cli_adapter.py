"""Generic fail-closed bridge from an external agent CLI to Hermes Kanban.

The external engine performs domain work.  This bridge owns trusted run
provenance, Timeline preflight/evidence, optional research-policy preflight, and the
terminal Kanban transition so an arbitrary CLI does not need direct database
access or a Hermes-native ``kanban_complete`` tool.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


REQUIRED_PROVENANCE = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_ROLE_SHELL_ID",
    "HERMES_EXECUTOR_ID",
    "HERMES_BINDING_ID",
    "HERMES_EFFECTIVE_CAPABILITIES",
    "HERMES_TIMELINE_GOAL_ID",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_CLAIM_LOCK",
)


class ExternalCLIAdapterError(RuntimeError):
    """Raised when an external engine cannot satisfy the bound-run contract."""


@dataclass(frozen=True)
class BoundContext:
    task_id: str
    run_id: int
    shell_id: str
    shell_key: str
    executor_id: str
    binding_id: str
    goal_id: str
    claim_lock: str
    db_path: Path
    workspace: Path
    capabilities: tuple[str, ...]
    evidence_policy: dict[str, Any]
    title: str


def _json_object(text: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(text or "").strip())
    except json.JSONDecodeError as exc:
        raise ExternalCLIAdapterError(f"{label} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExternalCLIAdapterError(f"{label} returned a non-object JSON value")
    return payload


def _run_json(argv: list[str], *, timeout: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalCLIAdapterError(
            f"command timed out after {timeout}s: {argv[0]}"
        ) from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()[:1500]
        raise ExternalCLIAdapterError(
            f"command failed ({completed.returncode}): {argv[0]}: {detail}"
        )
    return _json_object(completed.stdout, label=argv[0])


def _required_env(environ: dict[str, str]) -> dict[str, str]:
    values = {name: str(environ.get(name) or "").strip() for name in REQUIRED_PROVENANCE}
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise ExternalCLIAdapterError(
            "Hermes bound-run provenance is required: " + ", ".join(missing)
        )
    return values


def _load_context(environ: dict[str, str]) -> BoundContext:
    from hermes_cli import kanban_db as kb
    from hermes_cli import supervisor_registry as registry

    values = _required_env(environ)
    try:
        run_id = int(values["HERMES_KANBAN_RUN_ID"])
    except ValueError as exc:
        raise ExternalCLIAdapterError("HERMES_KANBAN_RUN_ID must be an integer") from exc
    db_path = Path(values["HERMES_KANBAN_DB"]).expanduser().resolve()
    workspace = Path(values["HERMES_KANBAN_WORKSPACE"]).expanduser().resolve()
    if not db_path.is_file() or not workspace.is_dir():
        raise ExternalCLIAdapterError("bound Kanban DB or workspace is unavailable")
    conn = kb.connect(db_path)
    try:
        task = kb.get_task(conn, values["HERMES_KANBAN_TASK"])
        if (
            task is None
            or task.status != "running"
            or task.current_run_id != run_id
            or task.claim_lock != values["HERMES_KANBAN_CLAIM_LOCK"]
        ):
            raise ExternalCLIAdapterError("bound Hermes task is no longer current")
        run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ExternalCLIAdapterError("bound Hermes run does not exist")
        expected = {
            "role_shell_id": values["HERMES_ROLE_SHELL_ID"],
            "executor_id": values["HERMES_EXECUTOR_ID"],
            "binding_id": values["HERMES_BINDING_ID"],
        }
        for key, value in expected.items():
            if str(run[key] or "") != value:
                raise ExternalCLIAdapterError(f"Hermes run provenance mismatch: {key}")
        shell = registry.get_shell(conn, shell_id=expected["role_shell_id"])
        executor = registry.get_executor(conn, expected["executor_id"])
        binding = registry.get_binding(conn, expected["binding_id"])
        if shell is None or executor is None or binding is None:
            raise ExternalCLIAdapterError("Hermes registry provenance is incomplete")
        if not executor.enabled or not binding.enabled:
            raise ExternalCLIAdapterError("executor or binding was disabled before launch")
        capabilities = tuple(
            sorted(
                item.strip()
                for item in values["HERMES_EFFECTIVE_CAPABILITIES"].split(",")
                if item.strip()
            )
        )
        if not {"kanban", "hermes-timeline-code-map"}.issubset(capabilities):
            raise ExternalCLIAdapterError(
                "external CLI requires effective Kanban and Timeline capabilities"
            )
        goal_id = registry.timeline_goal_id(task.id, run_id)
        if goal_id != values["HERMES_TIMELINE_GOAL_ID"]:
            raise ExternalCLIAdapterError("Hermes Timeline goal provenance mismatch")
        return BoundContext(
            task_id=task.id,
            run_id=run_id,
            shell_id=shell.id,
            shell_key=shell.shell_key,
            executor_id=executor.id,
            binding_id=binding.id,
            goal_id=goal_id,
            claim_lock=task.claim_lock,
            db_path=db_path,
            workspace=workspace,
            capabilities=capabilities,
            evidence_policy=dict(shell.evidence_policy),
            title=task.title,
        )
    finally:
        conn.close()


def _timeline_base(args: argparse.Namespace) -> list[str]:
    command = [args.timeline_python, args.timeline_client]
    if args.timeline_db:
        command.extend(["--db-path", args.timeline_db])
    return command


def _timeline(
    args: argparse.Namespace,
    operation: str,
    *operation_args: str,
) -> dict[str, Any]:
    values = list(operation_args)

    def take(flag: str, default: str = "") -> str:
        if flag not in values:
            return default
        index = values.index(flag)
        if index + 1 >= len(values):
            raise ExternalCLIAdapterError(f"Timeline flag requires a value: {flag}")
        result = values[index + 1]
        del values[index : index + 2]
        return result

    # The bridge uses named arguments internally for readability; translate
    # them to the bundled hermes-timeline-cli positional contract here.
    if operation == "context":
        goal_id = take("--goal-id")
        command = ["context", goal_id, *values]
    elif operation == "record":
        domain = take("--domain")
        kind = take("--kind")
        title = take("--title")
        command = ["record", domain, kind, title, *values]
    elif operation == "query-slice":
        repo_root = take("--repo-root")
        query = take("--query")
        command = ["query-slice", repo_root, query, *values]
    elif operation == "recall-neural":
        query = take("--query")
        command = ["recall-neural", query, *values]
    elif operation == "link":
        from_id = take("--from-id")
        to_id = take("--to-id")
        relation = take("--relation")
        command = ["link", from_id, to_id, relation, *values]
    elif operation == "verify-all":
        command = ["verify", *values]
    else:
        command = [operation, *values]
    return _run_json(
        [*_timeline_base(args), *command],
        timeout=args.timeline_timeout_seconds,
    )


def _node_id(payload: dict[str, Any], *, label: str) -> str:
    node_id = str(payload.get("node_id") or "").strip()
    if not node_id:
        raise ExternalCLIAdapterError(f"Timeline {label} did not return node_id")
    return node_id


def _find_int(value: Any, name: str) -> Optional[int]:
    if isinstance(value, dict):
        if name in value:
            try:
                return int(value[name])
            except (TypeError, ValueError):
                pass
        for child in value.values():
            found = _find_int(child, name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_int(child, name)
            if found is not None:
                return found
    return None


def _research_policy_preflight(
    args: argparse.Namespace, context: BoundContext
) -> dict[str, Any]:
    """Load an optional user-owned research policy without requiring private data.

    The public Role Shell is complete on its own.  Operators may attach a JSON
    or Markdown policy to narrow evidence/source rules for one command adapter;
    the policy is recorded in the receipt and never treated as permission to
    widen the immutable shell contract.
    """
    if context.shell_key != "market":
        return {"required": False}
    policy_value = str(getattr(args, "research_policy", "") or "").strip()
    if not policy_value:
        return {
            "required": False,
            "status": "not_configured",
            "workflow": "public_market_role_shell",
        }
    policy_path = Path(policy_value).expanduser().resolve()
    try:
        text = policy_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ExternalCLIAdapterError(f"research policy preflight failed: {exc}") from exc
    if not text.strip():
        raise ExternalCLIAdapterError(
            f"research policy is empty: {policy_path}"
        )
    policy_format = "markdown"
    sections: list[str] = []
    if policy_path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExternalCLIAdapterError(
                f"research policy JSON is invalid: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ExternalCLIAdapterError("research policy JSON must be an object")
        policy_format = "json"
        sections = sorted(str(key) for key in payload)
    return {
        "required": False,
        "status": "loaded",
        "path": str(policy_path),
        "format": policy_format,
        "sections": sections,
        "workflow": "public_market_role_shell_with_operator_policy",
    }


def _render_engine_argv(
    raw_argv: list[Any],
    *,
    context: BoundContext,
    prompt_file: Path,
    prompt_text: str,
) -> list[str]:
    values = {
        "prompt_file": str(prompt_file),
        "prompt_text": prompt_text,
        "workspace": str(context.workspace),
        "task_id": context.task_id,
        "run_id": str(context.run_id),
        "goal_id": context.goal_id,
    }
    rendered: list[str] = []
    for raw in raw_argv:
        item = str(raw)
        for key, value in values.items():
            item = item.replace("{" + key + "}", value)
        rendered.append(item)
    return rendered


def _run_engine(
    args: argparse.Namespace,
    context: BoundContext,
    prompt_file: Path,
    prompt_text: str,
) -> tuple[int, str, str]:
    try:
        raw_argv = json.loads(args.engine_argv_json)
    except json.JSONDecodeError as exc:
        raise ExternalCLIAdapterError("--engine-argv-json is invalid JSON") from exc
    if not isinstance(raw_argv, list) or not raw_argv:
        raise ExternalCLIAdapterError("--engine-argv-json must be a non-empty list")
    command = _render_engine_argv(
        raw_argv,
        context=context,
        prompt_file=prompt_file,
        prompt_text=prompt_text,
    )
    try:
        completed = subprocess.run(
            command,
            cwd=str(context.workspace),
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=args.engine_timeout_seconds,
            check=False,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return 124, stdout, stderr or f"engine timed out after {args.engine_timeout_seconds}s"


def _verify_current(context: BoundContext) -> None:
    from hermes_cli import kanban_db as kb

    conn = kb.connect(context.db_path)
    try:
        task = kb.get_task(conn, context.task_id)
        if (
            task is None
            or task.status != "running"
            or task.current_run_id != context.run_id
            or task.claim_lock != context.claim_lock
        ):
            raise ExternalCLIAdapterError("Hermes run changed while the engine ran")
    finally:
        conn.close()


def _finalize(
    context: BoundContext,
    *,
    engine_name: str,
    returncode: int,
    result: str,
    error: str,
    receipt: dict[str, Any],
) -> str:
    from hermes_cli import kanban_db as kb

    conn = kb.connect(context.db_path)
    try:
        metadata = {
            "adapter_mode": "external-cli",
            "external_engine": engine_name,
            "returncode": returncode,
        }
        if returncode == 0 and result.strip():
            closed = kb.complete_task(
                conn,
                context.task_id,
                result=result.strip(),
                summary=f"{engine_name} completed the bound role-shell task.",
                metadata=metadata,
                expected_run_id=context.run_id,
                receipt=receipt,
            )
            outcome = "completed"
        else:
            reason = error.strip() or result.strip() or f"{engine_name} returned no result"
            closed = kb.block_task(
                conn,
                context.task_id,
                reason=reason[:4000],
                kind="transient",
                expected_run_id=context.run_id,
                receipt=receipt,
            )
            outcome = "blocked"
        if not closed:
            raise ExternalCLIAdapterError("Hermes rejected the terminal transition")
        return outcome
    finally:
        conn.close()


def run(
    args: argparse.Namespace,
    *,
    environ: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    context = _load_context(environ or dict(os.environ))
    prompt_path = Path(args.prompt_file).expanduser().resolve()
    if not prompt_path.is_file() or context.workspace not in prompt_path.parents:
        raise ExternalCLIAdapterError("prompt file must be inside the bound workspace")
    research_policy = _research_policy_preflight(args, context)
    timeline_context = _timeline(args, "context", "--goal-id", context.goal_id)
    neural_context = _timeline(
        args,
        "recall-neural",
        "--query",
        context.title,
        "--limit",
        "8",
        "--max-chars",
        "2600",
        "--max-depth",
        "2",
        "--candidate-mode",
    )
    action = _timeline(
        args,
        "record",
        "--domain",
        "hermes-supervisor",
        "--kind",
        "action",
        "--title",
        f"Dispatch {args.engine_name} for {context.task_id}",
        "--body",
        json.dumps(
            {
                "task_id": context.task_id,
                "run_id": context.run_id,
                "executor_id": context.executor_id,
                "engine": args.engine_name,
                "capabilities": context.capabilities,
                "research_policy_preflight": research_policy,
                "neural_recall": {
                    "query": neural_context.get("query"),
                    "candidate_count": len(neural_context.get("items") or []),
                    "context_chars": int(neural_context.get("chars") or 0),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "--goal-id",
        context.goal_id,
        "--author",
        "hermes-external-cli-adapter",
    )
    action_node_id = _node_id(action, label="action record")
    slice_ids: list[str] = []
    slice_payload: dict[str, Any] = {}
    if context.evidence_policy.get("code_slice_required"):
        slice_payload = _timeline(
            args,
            "query-slice",
            "--repo-root",
            str(context.workspace),
            "--query",
            context.title,
            "--limit",
            "12",
            "--goal-id",
            context.goal_id,
            "--author",
            "hermes-external-cli-adapter",
            "--rebuild-if-missing",
        )
        slice_id = str(slice_payload.get("slice_id") or "").strip()
        if not slice_id:
            raise ExternalCLIAdapterError("required Timeline code slice was not returned")
        slice_ids.append(slice_id)
    original_prompt = prompt_path.read_text(encoding="utf-8")
    evidence_appendix = json.dumps(
        {
            "timeline_context": timeline_context,
            "timeline_neural_recall": neural_context,
            "timeline_code_slice": slice_payload,
            "research_policy_preflight": research_policy,
        },
        ensure_ascii=False,
        sort_keys=True,
    )[:16000]
    engine_prompt = (
        original_prompt
        + "\n\n## Adapter-loaded shared evidence\n"
        + evidence_appendix
        + "\n"
    )
    engine_prompt_path = prompt_path.with_name(f"engine-run-{context.run_id}.md")
    engine_prompt_path.write_text(engine_prompt, encoding="utf-8")
    returncode, stdout, stderr = _run_engine(
        args,
        context,
        engine_prompt_path,
        engine_prompt,
    )
    result = stdout.strip()
    error = stderr.strip()
    output = _timeline(
        args,
        "record",
        "--domain",
        "hermes-supervisor",
        "--kind",
        "output",
        "--title",
        f"{args.engine_name} result for {context.task_id}",
        "--body",
        json.dumps(
            {
                "task_id": context.task_id,
                "run_id": context.run_id,
                "engine": args.engine_name,
                "returncode": returncode,
                "result": result[:12000],
                "error": error[:2000],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "--goal-id",
        context.goal_id,
        "--prev-id",
        action_node_id,
        "--author",
        "hermes-external-cli-adapter",
    )
    output_node_id = _node_id(output, label="output record")
    _timeline(
        args,
        "link",
        "--from-id",
        action_node_id,
        "--to-id",
        output_node_id,
        "--relation",
        "produces",
        "--author",
        "hermes-external-cli-adapter",
    )
    verify = _timeline(args, "verify-all")
    invalid_count = _find_int(verify, "invalid_count")
    verified_count = _find_int(verify, "verified_count")
    if verified_count is None:
        verified_count = _find_int(verify, "total") or 0
    if invalid_count != 0:
        raise ExternalCLIAdapterError(
            f"Timeline verify-all did not pass: invalid_count={invalid_count}"
        )
    _verify_current(context)
    receipt = {
        "run_id": context.run_id,
        "task_id": context.task_id,
        "role_shell_id": context.shell_id,
        "executor_id": context.executor_id,
        "binding_id": context.binding_id,
        "outputs": [
            {
                "kind": "external_cli_result",
                "engine": args.engine_name,
                "returncode": returncode,
                "value": result[:12000] or error[:4000],
            },
            {"kind": "research_policy_preflight", "value": research_policy},
        ],
        "timeline": {
            "goal_id": context.goal_id,
            "context_loaded": True,
            "neural_recall": {
                "performed": True,
                "query": str(neural_context.get("query") or context.title),
                "candidate_count": len(neural_context.get("items") or []),
                "context_chars": int(neural_context.get("chars") or 0),
            },
            "slice_ids": slice_ids,
            "node_ids": [action_node_id, output_node_id],
            "verify_all": {
                "invalid_count": invalid_count,
                "verified_count": verified_count,
            },
        },
    }
    outcome = _finalize(
        context,
        engine_name=args.engine_name,
        returncode=returncode,
        result=result,
        error=error,
        receipt=receipt,
    )
    return {
        "status": outcome,
        "mode": "external-cli",
        "engine": args.engine_name,
        "task_id": context.task_id,
        "run_id": context.run_id,
        "timeline_node_ids": [action_node_id, output_node_id],
        "timeline_slice_ids": slice_ids,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--engine-name", required=True)
    parser.add_argument("--engine-argv-json", required=True)
    parser.add_argument("--engine-timeout-seconds", type=float, default=1800)
    parser.add_argument("--timeline-client", required=True)
    parser.add_argument("--timeline-python", default=sys.executable)
    parser.add_argument("--timeline-db")
    parser.add_argument("--timeline-timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--research-policy",
        help="Optional operator-owned JSON or Markdown policy for market research",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = run(args)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "mode": "external-cli", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
