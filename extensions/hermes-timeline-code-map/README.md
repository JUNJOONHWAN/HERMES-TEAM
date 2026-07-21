# Hermes Timeline Code Map + NeuralLink

This bundled extension is HERMES-TEAM's shared evidence graph. It keeps
append-only work history, repository indexes and stored task slices, output and
reasoning links, typed Roadmap events, and a small associative recall index in
one SQLite database.

## Components

- **Timeline** records context, actions, outputs, decisions, and links. Every
  node participates in a hash chain and `verify_all` must report zero invalid
  nodes before a Role Shell run can close.
- **Code Map** indexes repository files/symbols/relationships and returns a
  bounded query slice. The `code` Role Shell requires a stored `slice_id`;
  non-code roles do not pretend that a repository slice is relevant.
- **NeuralLink** incrementally derives lexical, metadata, entity, temporal, and
  graph-neighbor features from Timeline nodes. It does not run an embedding
  server. Recall returns candidates for the answer model to rerank.
- **Roadmap** stores typed, versioned events and rebuildable projections for
  goals and tasks.

## Install

From the distribution root:

```bash
python3 -m pip install -e 'extensions/hermes-timeline-code-map[mcp]'
```

The public setup script installs this automatically unless
`--skip-timeline-install` is supplied.

The default database is:

```text
~/.hermes/timeline_code_map/graph.db
```

Set `TIMELINE_CODE_MAP_DB_PATH` only when an operator deliberately chooses a
different database. No user-specific path is compiled into this extension.

## CLI

```bash
hermes-timeline-cli context GOAL_ID
hermes-timeline-cli index-code /path/to/repo --goal-id GOAL_ID
hermes-timeline-cli query-slice /path/to/repo 'adapter routing' --goal-id GOAL_ID
hermes-timeline-cli recall-neural 'adapter routing failure' --candidate-mode
hermes-timeline-cli verify
hermes-timeline-cli neural-status
```

The distribution also ships `scripts/hermes_timeline_cli.py`, a stable Python
bridge used by external command adapters.

## MCP server

```json
{
  "command": "/path/to/python",
  "args": ["-m", "hermes_timeline_code_map.mcp_server"],
  "env": {
    "TIMELINE_CODE_MAP_DB_PATH": "/operator-selected/hermes/timeline_code_map/graph.db"
  }
}
```

The root Hermes controller does not receive this MCP. The setup process copies
the catalog entry into each isolated worker profile, and OpenCode receives a
role-scoped MCP bundle.

## NeuralLink runtime behavior

The `timeline-neural-link` Hermes plugin runs a bounded, read-only recall before
an LLM turn. If the graph or index is empty, it injects no context; this is a
normal zero-candidate result, not a failure. Role Shell receipts additionally
record whether recall was performed, its query, candidate count, and context
size. This makes “recall happened and found nothing” distinguishable from
“recall was skipped.”

NeuralLink deliberately changes the memory failure mode rather than claiming to
solve semantic memory completely. It removes an embedding-service dependency,
but abstract similarity still depends on generated aliases, metadata, graph
links, and final model reranking.

## Tests

```bash
python3 -m unittest discover -s extensions/hermes-timeline-code-map/tests
```
