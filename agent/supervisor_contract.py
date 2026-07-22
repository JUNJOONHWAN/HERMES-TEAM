"""Shared behavioral contract for every Hermes supervisor controller model."""

from __future__ import annotations

import re
import json
from typing import Any


SUPERVISOR_CONTROL_TOOL_SEQUENCE = (
    "supervisor_status",
    "supervisor_automation",
    "supervisor_roles",
    "supervisor_delegate",
    "supervisor_project",
    "supervisor_adapter",
)
SUPERVISOR_CONTROL_TOOL_NAMES = frozenset(SUPERVISOR_CONTROL_TOOL_SEQUENCE)

SUPERVISOR_DEVELOPER_INSTRUCTIONS = """\
You are Hermes, the fixed lightweight central control tower. You are not a
general coding, research, market-analysis, or operations worker.

Your permanent architecture:
- The controller is one control slot. Eight immutable role shells are the
  other slots: browser-research, code, market, operations, report,
  verification, tool-management (Multitool), and hermes-repair (Hermes
  Maintainer).
- Role shells are durable identities and Kanban ownership boundaries, not
  model processes. Real executors bind many-to-many: one executor may serve
  several shells, and a shell may have a primary plus candidate executors.
- Every delegated task becomes a durable Kanban card. Adapter switches,
  overrides, reruns, claims, receipts, and failures remain auditable history.
- Hermes Timeline Code Map is shared evidence infrastructure used by workers.
  The controller does not need to own or load the whole code map; it consumes
  task summaries, receipts, and audit state.
- MCP and domain capabilities belong to worker executors. The controller's
  supervisor tools below are local control-plane calls, not MCP tools.
- Project/card lifecycle is native controller authority. Project metadata,
  root-card threads, typed relations, pause/close/reopen transitions, and recovery
  lineage are never delegated to an adapter.
- Project progress, pending code-card approvals, repository identity, card
  branch commits, and push receipts live in the separate Project DB. A code
  card proposal is not a card until the operator explicitly approves its
  pa_* request. Workers may commit/push their card branch through the
  controller, but never push main/master directly.

Operating rules:
1. Never perform domain work yourself. Choose the matching role shell and call
   supervisor_delegate so an executor can claim the Kanban card atomically.
   For a clear role request, call supervisor_delegate directly; do not list
   roles first. After a successful create response, report its card id, say it
   is queued for automatic dispatch (not merely "waiting for assignment"), and
   report whether completion notification is subscribed to the originating
   conversation. Stop after that; do not inspect the new card unless the user
   explicitly asks. If an async messaging conversation was not subscribed,
   surface that as a control-plane failure instead of promising a later result.
   When a verifier/recovery card must consume output from blocked or failed
   cards, pass their exact ids as source_task_ids. This creates non-blocking
   audited lineage and injects their runs/comments into the verifier prompt;
   do not create another recovery card merely because a source stayed blocked.
2. Ordinary project bug diagnosis, code changes, incident repair, runtime
   remediation, and automation repair are normal repair work. Call
   supervisor_delegate with work_kind="repair", shell_key="code" or
   "operations", and the configured ordinary repair executor. These tasks may
   continue to use lower-cost workers. Hermes self-maintenance is different:
   only when the target is Hermes itself--its controller, adapters, role shells,
   routing, supervisor configuration, or Hermes runtime contract--call
   supervisor_delegate with work_kind="hermes_repair" and
   shell_key="hermes-repair". That route is pinned to the dedicated
   gpt-5.6-sol/high Maintainer and may analyze exact source_task_ids before
   changing Hermes code or configuration. Never route generic project coding to
   hermes-repair merely because the operator is speaking to Hermes. Never
   inspect files, logs, processes, repositories, or cron storage yourself. The
   tool enforces both boundaries even if you omit or contradict the executor id.
   Tool, skill, plugin, or MCP inventory/installation/assignment requests are
   not controller work: delegate them to shell_key="tool-management". That
   worker must keep assignments role-scoped and hand source/service/secret
   repair back to code or operations instead of widening its own authority.
3. For current runtime, heartbeat, role, task, or adapter facts, use the
   matching supervisor tool. Never guess from conversation memory.
   Controller-owned state changes are also your job: use supervisor_automation
   or supervisor_adapter directly instead of merely describing a missing rule.
   Use supervisor_project for project creation, project/card inspection,
   independent root cards inside an existing project, follow-up cards,
   parallel splits, verification cards, recovery cards, old card lookup, and
   project pause/close/reopen. An adapter may execute or propose a card, but only
   this controller tool may commit the project/card graph.
   Before issuing any new code Role Shell card, create the approval request,
   show both Project ID and pa_* approval ID, and ask the operator to approve
   or reject it. Never call approve_project_card in the same turn that created
   the request. For a new code Project, ask whether to use an existing repo,
   initialize local Git, create a private/public GitHub repo, or use no repo.
   GitHub creation, card-branch checkpoint/push, and approval decisions are
   explicit controller actions; do not simulate them in prose.
   If the operator says to stop an ordinary running Kanban card, call
   supervisor_project with action=pause_card. This must terminate the worker
   and hold the same card for a later explicit resume; do not answer that no
   cancellation tool exists. A later plain continue/resume instruction uses
   resume_card. A requested correction followed by continue uses steer_card so
   the controller stops first, records the instruction as card context, and
   resumes the same card. Never substitute gateway/service shutdown because it
   can affect unrelated work.
   If the operator materially changes scope while a Project card is running,
   call request_direction_change instead of trying to inject a new prompt into
   the worker. The controller must stop and archive the current run, preserve a
   Git checkpoint when applicable, and return a pa_* successor proposal. Show
   the Project ID, source Card ID, checkpoint state, and approval ID, then wait
   for a later explicit approve/reject instruction. Never approve the successor
   in the same turn. Minor clarifications that do not change scope or acceptance
   criteria may stay on the same card through its normal comment/retry path.
4. Never use shell, filesystem search, git, logs, web search, MCP, raw Kanban
   tools, or code edits to discover or mutate supervisor state. If a supervisor
   tool fails, report the bridge failure; do not work around it. Never claim a
   supervisor tool is unavailable unless the runtime returned that exact error.
5. Use supervisor_adapter for adapter lists, ownership, provider/model/reasoning
   details, temporary/permanent/one-shot overrides, recent tasks, inspection,
   and reruns. A status request should normally require one tool call.
6. For automation or heartbeat status, read every row in scheduled.jobs and
   report the scheduled.counts totals. required_cron is only a protected
   baseline used to detect deletion; it is never the active automation list.
   Surface failed_enabled_cron, which contains unacknowledged actionable
   failures. A failure acknowledgement is a state mutation: call
   supervisor_automation with action=acknowledge_failures only when the user
   explicitly commands Hermes to acknowledge, hide, or stop repeating that
   exact failure. A question about why a failure appears, a claim that its
   artifact succeeded, or a request to inspect whether heartbeat is correct is
   not authorization to acknowledge anything. After status tools, answer the
   user's actual question with a direct conclusion; return a raw operator_text
   screen only when the user asked to show the status screen itself.
7. For an adapter-status question, call supervisor_adapter exactly once with
   action="list" and view="compact". Return its operator_text verbatim, with no
   preface, conclusion, duplicated executor roster, extra supervisor_roles or
   supervisor_status call, check mark, or X glyph. This default is an iPhone
   one-line-per-role tree followed by controller-fallback scope and short
   role/tool explanations. Internal worker ids, repeated healthy states,
   provider fields, reasoning legends, candidate identities, and candidate
   role counts are detail-only. Never label a reusable candidate merely
   "대기"; detailed status must distinguish 추가 배정 가능, 운영자 비활성,
   and 헬스 실패. Use view="full" when the operator explicitly asks for details,
   worker ids, raw history, health diagnostics, provider fields, or candidates.
8. Explain your routing judgment briefly, then report the card id or audited
   adapter change. Do not impersonate the executor that will do the work.
9. Your controller cwd is a private empty control workspace, never a project
   workspace. Never pass it as workspace_path. Use workspace_kind="scratch"
   unless the user supplied an exact project directory or a prior audited task
   already records that directory. Put a repo name such as example-project in the card
   body when its exact path is not established; let the worker resolve it.
"""


_NON_ACTIONABLE_MESSAGE = re.compile(
    r"^(?:안녕(?:하세요)?|반가워|고마워|감사(?:합니다)?|thanks?|hello|hi)[.!?\s]*$",
    re.IGNORECASE,
)
_REPAIR_REQUEST = re.compile(
    r"(?:수선|수정|고쳐|고치|복구|장애|버그|오류|문제\s*해결|"
    r"작동(?:이|을)?\s*안|안\s*(?:돼|되|되는|됨)|"
    r"\b(?:repair|fix|debug|bug|incident|restore|recover|remediat(?:e|ion)|hotfix)\b)",
    re.IGNORECASE,
)
_HERMES_SELF_MAINTENANCE_TARGET = re.compile(
    r"(?:"
    r"헤르메스(?:를|의|자체)|"
    r"헤르메스\s*(?:컨트롤러|어댑터|셸|쉘|라우터|확장|설정|런타임|"
    r"수퍼바이저|슈퍼바이저|감독|제어부)|"
    r"\bHermes(?:\s+itself|'s|\s+(?:controller|adapter|role\s*shell|shell|"
    r"router|extension|config(?:uration)?|runtime|supervisor|control\s*plane))"
    r")",
    re.IGNORECASE,
)
_ADAPTER_REQUEST = re.compile(
    # Korean mobile typos are common in Telegram.  Keep this bounded to the
    # adapter noun instead of fuzzy-matching the whole utterance: the live
    # incident was "어갭커 현황", which previously fell through to the broad
    # supervisor_status screen and mixed services, cron and receipts into an
    # adapter-only answer.
    r"(?:(?:어|아)[댑뎁답갭][터커]|어댑터|아댑터|실행기|카드|칸반|작업|"
    r"adapter|executor|kanban|task)",
    re.IGNORECASE,
)
_PROJECT_CARD_REQUEST = re.compile(
    r"(?:프로젝트|project|"
    r"(?:pa_[a-f0-9]+|승인\s*요청).{0,30}(?:승인|거절|approve|reject)|"
    r"(?:승인|거절|approve|reject).{0,30}(?:pa_[a-f0-9]+|승인\s*요청)|"
    r"(?:프로젝트|카드|project|card).{0,30}(?:깃허브|레포|저장소|커밋|푸시|github|repo|commit|push)|"
    r"(?:후속|연속|다음)\s*(?:카드|작업)|"
    r"(?:카드|작업)(?:을|를|에|의)?\s*(?:관계|계보|묶음|분해|병렬|검증|복구|이어|계속|종결|종료|재개)|"
    r"(?:카드|작업)\s*(?:매니저|관리자)|"
    r"(?:card|task)\s*(?:chain|thread|follow[- ]?up|split|verify|recover|continue)|"
    r"(?:close|reopen)\s+project)",
    re.IGNORECASE,
)
_AUTOMATION_REQUEST = re.compile(
    r"(?:자동화|하트비트|허트비트|크론|스케줄|automation|heartbeat|cron|schedule)",
    re.IGNORECASE,
)
_FAILURE_ACKNOWLEDGEMENT_REQUEST = re.compile(
    r"(?:"
    r"(?:실패|경고|오류).{0,20}(?:확인\s*처리|숨겨|감춰|제외해|알리지\s*마|반복하지\s*마)"
    r"|(?:확인\s*처리|숨겨|감춰|제외해|알리지\s*마|반복하지\s*마).{0,20}(?:실패|경고|오류)"
    r"|\backnowledge(?:ment)?\b"
    r")",
    re.IGNORECASE,
)
_FAILURE_ACKNOWLEDGEMENT_CLEAR_REQUEST = re.compile(
    r"(?:"
    r"(?:실패|경고|오류).{0,20}(?:확인\s*기록|acknowledgement).{0,12}(?:지워|삭제|초기화|취소|해제|clear)"
    r"|(?:확인\s*기록|acknowledgement).{0,12}(?:지워|삭제|초기화|취소|해제|clear)"
    r")",
    re.IGNORECASE,
)
_INTERPRETIVE_STATUS_REQUEST = re.compile(
    r"(?:왜|뭐가\s*문제|무슨\s*문제|원인|비정상|이상|설명|분석|판정|"
    r"검토|정상(?:이야|인가|인지|한지|해| 맞)|제대로|실제로|정각|누락|지연|"
    r"됐는데|나갔(?:는데|잖)|맞(?:아|나|는지|지))",
    re.IGNORECASE,
)
_ROLE_REQUEST = re.compile(
    r"(?:역할|셸|쉘|role|shell)",
    re.IGNORECASE,
)
_TOOL_MANAGEMENT_MUTATION = re.compile(
    r"(?=.*(?:MCP|스킬|skill|툴|도구|플러그인|plugin))"
    r"(?=.*(?:설치|등록|추가|장착|붙(?:여|이|임)|배정|교체|변경|갱신|"
    r"업데이트|제거|삭제|해제|관리해|install|register|add|attach|assign|"
    r"switch|update|remove|delete|uninstall|manage))",
    re.IGNORECASE,
)


def supervisor_control_tool_required(user_message: str) -> bool:
    """Require a control tool for every substantive operator turn.

    Trusted async completion payloads are already validated controller input and
    may be relayed without another read. Greetings and thanks are the only other
    no-tool path; everything operational fails closed.
    """
    text = str(user_message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("[kanban]") or lowered.startswith("cronjob response:"):
        return False
    return _NON_ACTIONABLE_MESSAGE.fullmatch(text) is None


def supervisor_repair_delegation_required(user_message: str) -> bool:
    """Return whether the operator asked Hermes to diagnose or repair a fault."""
    text = str(user_message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("[kanban]") or lowered.startswith("cronjob response:"):
        return False
    return _REPAIR_REQUEST.search(text) is not None


def supervisor_hermes_repair_delegation_required(user_message: str) -> bool:
    """Return whether the repair target is Hermes itself, not project code."""
    text = str(user_message or "").strip()
    return bool(
        text
        and supervisor_repair_delegation_required(text)
        and _HERMES_SELF_MAINTENANCE_TARGET.search(text)
    )


def supervisor_tool_management_delegation_required(user_message: str) -> bool:
    """Return whether the operator requested a tool-lifecycle mutation."""
    text = str(user_message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("[kanban]") or lowered.startswith("cronjob response:"):
        return False
    # Incident and bug language keeps the stronger Codex repair policy. The
    # repair worker can hand a completed configuration task back to Multitool;
    # the reverse must not silently weaken repair authority or evidence gates.
    if supervisor_repair_delegation_required(text):
        return False
    return _TOOL_MANAGEMENT_MUTATION.search(text) is not None


def supervisor_failure_acknowledgement_requested(user_message: str) -> bool:
    """Return True only for an explicit command to suppress one failed run."""
    text = str(user_message or "").strip()
    return bool(text and _FAILURE_ACKNOWLEDGEMENT_REQUEST.search(text))


def supervisor_automation_mutation_authorized(
    user_message: str,
    action: str,
) -> bool:
    """Gate acknowledgement mutations on the operator's current utterance."""
    normalized = str(action or "").strip().lower()
    if normalized == "acknowledge_failures":
        return supervisor_failure_acknowledgement_requested(user_message)
    if normalized == "clear_acknowledgements":
        text = str(user_message or "").strip()
        return bool(text and _FAILURE_ACKNOWLEDGEMENT_CLEAR_REQUEST.search(text))
    return True


def supervisor_operator_screen_allowed(
    user_message: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    """Return whether deterministic operator text may replace the real answer.

    Compact screens are useful for direct display requests.  They must never
    overwrite a model's interpretation of the same evidence or the result of a
    controller mutation.
    """
    text = str(user_message or "").strip()
    name = str(tool_name or "").strip()
    args = arguments if isinstance(arguments, dict) else {}

    if name == "supervisor_adapter":
        return (
            str(args.get("action") or "list").strip().lower() == "list"
            and str(args.get("view") or "compact").strip().lower() == "compact"
            and _ADAPTER_REQUEST.search(text) is not None
        )
    if supervisor_repair_delegation_required(text):
        return False
    if _INTERPRETIVE_STATUS_REQUEST.search(text):
        return False
    if name == "supervisor_status":
        return True
    if name == "supervisor_automation":
        return str(args.get("action") or "list_failures").strip().lower() == "list_failures"
    if name == "supervisor_roles":
        return _ROLE_REQUEST.search(text) is not None
    return False


def supervisor_control_plane_active(agent: Any) -> bool:
    """Return whether this agent is the real five-tool controller.

    Helper agents created inside a supervisor-root process (for example the
    in-place compression agent) inherit the global supervisor config but do
    not expose the control toolset.  They must not be forced through the
    controller contract merely because the root config is enabled.
    """
    if not bool(getattr(agent, "_supervisor_mode", False)):
        return False
    valid_names = set(getattr(agent, "valid_tool_names", set()) or set())
    return SUPERVISOR_CONTROL_TOOL_NAMES.issubset(valid_names)


def supervisor_recovery_tool_name(user_message: str) -> str:
    """Choose a specific control tool only after a controller violation.

    Normal routing remains model-judged with ``tool_choice=required``.  This
    deterministic choice is the bounded same-model recovery path used when a
    controller ignored that contract and returned prose without a tool call.
    """
    text = str(user_message or "").strip()
    if _PROJECT_CARD_REQUEST.search(text):
        return "supervisor_project"
    if supervisor_repair_delegation_required(text):
        return "supervisor_delegate"
    if supervisor_tool_management_delegation_required(text):
        return "supervisor_delegate"
    if supervisor_failure_acknowledgement_requested(text):
        return "supervisor_automation"
    if _AUTOMATION_REQUEST.search(text):
        return "supervisor_status"
    if _ROLE_REQUEST.search(text):
        return "supervisor_roles"
    if _ADAPTER_REQUEST.search(text):
        return "supervisor_adapter"
    return "supervisor_status"


def supervisor_required_tool_name(user_message: str) -> str | None:
    """Return the one tool a clear controller intent must use.

    Ambiguous domain requests remain model-routed.  Clear repair, automation,
    role, and adapter/status requests are deterministic UI boundaries: calling
    a different supervisor tool is still a contract violation even though it
    technically touched the control plane.
    """
    text = str(user_message or "").strip()
    if _PROJECT_CARD_REQUEST.search(text):
        return "supervisor_project"
    if supervisor_repair_delegation_required(text):
        return "supervisor_delegate"
    if supervisor_tool_management_delegation_required(text):
        return "supervisor_delegate"
    if supervisor_failure_acknowledgement_requested(text):
        return "supervisor_automation"
    if _AUTOMATION_REQUEST.search(text):
        return "supervisor_status"
    if _ROLE_REQUEST.search(text):
        return "supervisor_roles"
    if _ADAPTER_REQUEST.search(text):
        return "supervisor_adapter"
    return None


def supervisor_tool_choice_payload(
    api_mode: str,
    tool_name: str | None = None,
) -> Any:
    """Return the provider-native tool-choice payload for a control turn."""
    mode = str(api_mode or "chat_completions").strip().lower()
    name = str(tool_name or "").strip()
    if name:
        if mode == "anthropic_messages":
            return {"type": "tool", "name": name}
        if mode == "codex_responses":
            return {"type": "function", "name": name}
        return {"type": "function", "function": {"name": name}}
    if mode == "anthropic_messages":
        return {"type": "any"}
    return "required"


def supervisor_operator_text_from_tool_result(content: Any) -> str | None:
    """Extract a deterministic operator screen from provider tool wrappers.

    Chat-completions transports store the tool payload as a JSON object, while
    Codex responses stores the same payload inside an ``inputText`` content
    array.  Treat both as transport envelopes; the model must never be asked to
    paraphrase the raw registry payload merely because its provider wrapped it.
    """

    def _decode(value: Any, depth: int = 0) -> str | None:
        if depth > 6:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return _decode(json.loads(text), depth + 1)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
        if isinstance(value, dict):
            operator_text = str(value.get("operator_text") or "").strip()
            if operator_text:
                return operator_text
            for key in ("text", "content"):
                nested = _decode(value.get(key), depth + 1)
                if nested:
                    return nested
            return None
        if isinstance(value, list):
            for item in value:
                nested = _decode(item, depth + 1)
                if nested:
                    return nested
        return None

    return _decode(content)


def normalize_supervisor_repair_delegation(
    user_message: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Force repair metadata onto an eligible supervisor delegation call."""
    normalized = dict(arguments)
    if not supervisor_repair_delegation_required(user_message):
        return normalized, False
    if str(tool_name or "").strip() != "supervisor_delegate":
        return normalized, False
    hermes_repair = supervisor_hermes_repair_delegation_required(user_message)
    if hermes_repair:
        normalized["shell_key"] = "hermes-repair"
        normalized["work_kind"] = "hermes_repair"
    else:
        normalized["work_kind"] = "repair"
    # Branch names only have meaning for an explicit worktree workspace.  A
    # controller model may still populate the optional field while delegating
    # the normal scratch repair card; passing that combination through makes
    # the otherwise valid repair fail before a card is created.
    if str(normalized.get("workspace_kind") or "scratch").strip() != "worktree":
        normalized.pop("branch_name", None)
    eligible = (
        str(normalized.get("shell_key") or "").strip() == "hermes-repair"
        if hermes_repair
        else str(normalized.get("shell_key") or "").strip()
        in {"code", "operations"}
    )
    return normalized, eligible


def normalize_supervisor_tool_management_delegation(
    user_message: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Pin tool-lifecycle mutations to the Multitool role boundary."""
    normalized = dict(arguments)
    if not supervisor_tool_management_delegation_required(user_message):
        return normalized, False
    if str(tool_name or "").strip() != "supervisor_delegate":
        return normalized, False
    normalized["shell_key"] = "tool-management"
    normalized["work_kind"] = "tooling"
    if str(normalized.get("workspace_kind") or "scratch").strip() != "worktree":
        normalized.pop("branch_name", None)
    return normalized, True


def activate_codex_control_failback(agent: Any) -> bool:
    """Jump directly to the configured Codex controller safety net.

    Same-provider free-model candidates are useful for ordinary provider
    outages, but a supervisor contract violation needs one predictable final
    safety net.  Walking each intermediate adapter created misleading
    user-visible fallback notices even though those models were never called.
    """
    if str(getattr(agent, "provider", "") or "").strip().lower() == "openai-codex":
        return False
    chain = list(getattr(agent, "_fallback_chain", None) or [])
    start = max(0, int(getattr(agent, "_fallback_index", 0) or 0))
    codex_index = next(
        (
            index
            for index in range(start, len(chain))
            if str(chain[index].get("provider") or "").strip().lower()
            == "openai-codex"
        ),
        None,
    )
    if codex_index is None:
        return False
    agent._fallback_index = codex_index
    if not agent._try_activate_fallback(emit_status=False):
        return False
    return (
        str(getattr(agent, "provider", "") or "").strip().lower()
        == "openai-codex"
    )
