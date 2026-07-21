# AI Operations Manual — HERMES-TEAM

This file is an executable contract for an AI operating or extending this
repository. Read it before changing Role Shells, adapters, tools, memory, or
heartbeat configuration.

## 0. Non-negotiable invariants

1. The Hermes root is a control plane, not a domain worker.
2. A model/provider identity never widens a Role Shell.
3. Effective capability is the intersection of Shell allowed capability,
   executor capability, and optional Binding cap.
4. Timeline context and NeuralLink recall are required on every bundled Shell.
5. A stored Code Map slice is required for `code`; it is not required for
   non-repository roles.
6. A bound run cannot close without a validated Receipt and
   `verify_all.invalid_count=0`.
7. New adapters start as health-gated candidates. Test once, then temporary,
   then permanent.
8. Secrets are environment/runtime state and never belong in source, JSON
   templates, receipts, or logs.
9. Optional market memory narrows research; it never grants capabilities.
10. Heartbeat has exactly three canonical layers: configuration,
    service_schedule, artifacts.

## 0.1 Maintainer release boundary

For this public project, the DGX Spark checkout is the maintainer's canonical
release source. A Mac checkout is a disposable working mirror: it may be used
for inspection or temporary preparation, but it must not be treated as release
truth or pushed ahead of Spark.

The public distribution repository is also strictly separate from the live
Hermes runtime repository. A release operation may initialize Git metadata,
validate, commit, and push only from the public distribution checkout. It must
not copy release files into the live checkout, mutate live configuration,
restart services, or use the live runtime worktree as a staging area.

Before release, record both paths in the operator's local evidence, prove that
the live checkout is unchanged, and publish only from the Spark distribution
checkout.

## 1. Orient before acting

Run:

```bash
pwd
git status --short
hermes supervisor adapter list --json
hermes supervisor shell list --active --json
hermes supervisor executor list --json
hermes supervisor binding list --json
hermes supervisor heartbeat --json
```

For source changes, load the Timeline goal context, perform NeuralLink recall,
and query a stored Code Map slice before editing. Read repository instructions
and the exact source/tests directly; the slice is impact evidence, not edit
permission.

## 2. Dispatch algorithm

Given a card:

1. Select one Role Shell from the requested outcome, not from a favorite model.
2. Reject scope that the Shell forbids.
3. Resolve enabled Bindings whose effective capabilities contain all required
   capabilities.
4. Apply the narrowest active Override: task before shell before global.
5. Reject unhealthy, disabled, over-capacity, or capability-incomplete routes.
6. Stamp task/run/shell/executor/binding/goal provenance.
7. Execute in the assigned workspace/profile.
8. Record action/output nodes and links.
9. Verify the Timeline chain.
10. Submit the complete result plus structured Receipt.

Never silently fall back to an executor that cannot satisfy the Shell.

## 3. Receipt shape

```json
{
  "run_id": 123,
  "task_id": "t_example",
  "role_shell_id": "code_v3",
  "executor_id": "executor_opencode_free",
  "binding_id": "binding_code_opencode_free",
  "outputs": [
    {"kind": "changed_files", "value": ["path/to/file.py"]},
    {"kind": "tests", "value": {"command": "...", "status": "passed"}}
  ],
  "timeline": {
    "goal_id": "hermes-task:t_example:run:123",
    "context_loaded": true,
    "neural_recall": {
      "performed": true,
      "query": "task-specific query",
      "candidate_count": 0,
      "context_chars": 0
    },
    "slice_ids": ["slice-id-required-for-code"],
    "node_ids": ["action-node", "output-node"],
    "verify_all": {"invalid_count": 0}
  }
}
```

Zero NeuralLink candidates are valid. Omitting the recall record is not.

## 4. Add or revise a Role Shell

Do not mutate an immutable version. Create a new version:

```bash
hermes supervisor shell add-version \
  --key example-role \
  --name 'Example Role' \
  --description 'Narrow responsibility' \
  --contract '{"allowed_adapters":["hermes_profile","command"],"instructions":"...","root_may_execute":false}' \
  --required-capability kanban \
  --required-capability hermes-timeline-code-map \
  --allowed-capability kanban \
  --allowed-capability hermes-timeline-code-map \
  --evidence-policy '{"timeline_required":true,"neural_recall_required":true,"code_slice_required":false,"verify_all_invalid_count":0,"outputs_required":true}'
```

Then explicitly rebind compatible executors. Never add a capability merely to
make an adapter pass.

## 5. Add a command adapter

Preferred path:

1. Copy `distribution/adapters/generic-command.example.json`.
2. Set a stable executor ID and exact executable.
3. Ensure `engine_argv` consumes `{prompt_file}` or `{prompt_text}`.
4. Declare only capabilities the complete adapter path actually provides.
5. Set a deterministic, read-only health probe.
6. Register it:

```bash
python3 scripts/register_external_adapter.py /path/to/reviewed-adapter.json
```

The trusted external bridge, not the external model, owns Timeline records,
Receipt provenance, and terminal Kanban transition.

### Codex

Use `distribution/adapters/codex-cli.json`. The adapter uses the existing local
Codex login and does not copy credentials.

### Grok

The bundled `controller_grok` uses provider `xai`, base URL
`https://api.x.ai/v1`, and key reference `XAI_API_KEY`. It remains disabled
until live catalog and tool-call checks pass. There is no assumed universal
Grok CLI command; if an operator has one, register it through the generic JSON
contract after verifying its real argv.

### OpenRouter

`controller_openrouter_gemma4` is optional and disabled. OpenRouter is not the
public default.

### Local models

`controller_vllm_gemma4` is a template for an OpenAI-compatible local endpoint.
Change model/base URL through the controller adapter controls, then health-test;
do not claim health from a listening PID alone.

## 6. Adapter promotion and rollback

Inspect:

```bash
hermes supervisor adapter list --json
hermes supervisor adapter history --limit 100
```

Test one task:

```bash
hermes supervisor adapter switch TASK_ID EXECUTOR_ID --once --reason 'probe'
```

Temporary shell route:

```bash
hermes supervisor adapter switch code EXECUTOR_ID \
  --temporary-seconds 1800 --reason 'bounded evaluation'
```

Permanent route only after evidence:

```bash
hermes supervisor adapter assign code EXECUTOR_ID \
  --primary --priority 120 --note 'approved after receipt review'
```

Rollback a live Override:

```bash
hermes supervisor adapter clear OVERRIDE_ID --reason 'rollback'
```

Disabling an executor affects future claims. Inspect active runs separately.

## 7. Tool, MCP, skill, and plugin lifecycle

1. Inventory the target profile and central tool catalog.
2. Identify provenance, license, version, executable, auth, and data writes.
3. Select one owning Role Shell/profile.
4. Back up the exact target config.
5. Install only in that profile; do not restore MCP to the root.
6. Start a new worker session because live contexts are immutable.
7. Run discovery and a read-only probe.
8. Verify the Shell's effective capability intersection.
9. Record before/after assignment and rollback path in the Receipt.

Export a role-specific MCP bundle:

```bash
python3 scripts/build_supervisor_mcp_bundle.py \
  --source ~/.hermes/profiles/PROFILE/config.yaml \
  --server hermes-timeline-code-map \
  --server OPTIONAL_ROLE_MCP \
  --claude-output /tmp/claude-mcp.json \
  --opencode-output /tmp/opencode-mcp.json \
  --manifest-output /tmp/mcp-manifest.json
```

## 8. Timeline, Code Map, and NeuralLink operations

```bash
hermes-timeline-cli context GOAL_ID
hermes-timeline-cli index-code /repo --goal-id GOAL_ID
hermes-timeline-cli query-slice /repo 'task terms' --goal-id GOAL_ID
hermes-timeline-cli recall-neural 'task terms and aliases' --candidate-mode
hermes-timeline-cli neural-status
hermes-timeline-cli verify
```

Interpretation:

- `pending_nodes > 0`: index maintenance is incomplete, not necessarily graph corruption.
- `invalid_count > 0`: hard completion failure.
- zero recall items: normal no-match result.
- zero code indexes: configuration is valid but no repository has been indexed.

## 9. Market research and optional memory

Base public frame:

1. Prefer official exchange/regulator/issuer and documented API sources.
2. Use Yahoo Finance or Naver Finance for discovery/cross-check where permitted.
3. Preserve URL, retrieval timestamp, market timezone, value units, and source state.
4. Never infer absent values or perform trade/account writes.

Optional memory:

```bash
python3 scripts/market_memory.py \
  --db "$HERMES_SUPERVISOR_ROOT/knowledge/market_memory.jsonl" \
  search 'task query'
```

If the file is absent or returns zero items, continue with current-source
research and record `no optional memory`. Add entries only with explicit write
authorization. Cite every used memory entry ID and revalidate time-sensitive
claims.

## 10. Heartbeat configuration

Canonical JSON:

```text
layers.configuration
layers.service_schedule
layers.artifacts
```

Artifact example in `config.yaml`:

```yaml
supervisor:
  artifact_health:
    enabled: true
    checks:
      - name: daily-report
        type: path
        path: outputs/report.json
        kind: file
        required: true
        min_bytes: 100
        max_age_seconds: 86400
```

Supported artifact checks are path-only: existence, file/directory kind, size,
age, optional SHA-256. Do not add arbitrary shell commands to Heartbeat.

## 11. Completion checklist

- requested scope is fully handled;
- active Role Shell and effective capabilities are shown;
- Timeline context and NeuralLink recall were performed;
- code work has a stored post-change slice;
- changed files and tests are exact;
- private paths/secrets are absent;
- health-gated adapters are not falsely enabled;
- three Heartbeat layers are interpretable;
- Receipt validates;
- Timeline `invalid_count=0`.

For a repository-wide public-core release gate, run:

```bash
scripts/run_tests.sh -j 8 \
  --exclude-manifest distribution/validation/test_exclusions.json
```

Do not add a new exclusion to make a HERMES-TEAM regression green. The manifest
is reserved for optional dependency suites and independently reproducible
upstream-baseline defects, with exact scope and reason. A stale entry is a hard
failure. After an upstream merge, run the preflight and this full gate as
described in `docs/HERMES_TEAM_UPGRADE_KO.md`.
