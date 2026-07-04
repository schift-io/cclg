# Patch Semantics

Mutable memory is the point of CCLG. Without mutation, long-term memory becomes
stale and actively harmful.

## Operations

- `supersede`: replace old memory with a new node.
- `refine`: make old memory more precise and suppress the old node.
- `expand`: add broader scope while keeping the old node active.
- `narrow`: constrain old memory and suppress the old node.
- `expire`: mark old memory no longer valid.
- `deprecate`: keep auditable but discourage use.
- `forget`: remove old memory from the effective view.
- `create`, `update`, `merge`, `split`, `resolve_conflict`, `rollback`: local
  MVP-supported operations for schema parity and CLI/MCP flow.

## Patch Example

```json
{
  "schema_version": "cclg.memory_patch.v0.1",
  "id": "patch_...",
  "operation": "refine",
  "target_ids": ["mem_hermes_only_001"],
  "new_node_ids": ["mem_cross_agent_001"],
  "new_content": "ACMC must support Claude Code, Codex, Hermes, and server-side ReACT agents.",
  "reason": "User expanded target agents.",
  "source": {
    "label": "manual",
    "session_ids": [],
    "turn_ids": [],
    "raw_spans": [],
    "tool_result_ids": [],
    "artifact_ids": []
  },
  "confidence": 1.0,
  "resolution_policy": {
    "rule": "manual",
    "auto_applied": true,
    "requires_review": false
  },
  "applied_at": "2026-06-30T00:00:00+00:00"
}
```

Patch application also writes a `MemoryEdge` when the operation creates a new
node from an old node:

```json
{
  "schema_version": "cclg.edge.v0.1",
  "id": "edge_...",
  "from": "mem_cross_agent_001",
  "to": "mem_hermes_only_001",
  "type": "refines",
  "source_patch_id": "patch_..."
}
```

## Effective View Rule

```text
active_nodes = all nodes where status == "active"
session_active_nodes = active_nodes + active_session nodes for that session id
```

Suppressed nodes remain auditable but are not injected into prompts.

## Local MVP Behavior

The current implementation writes patches under `~/.cclg/patches`, edges under
`~/.cclg/edges`, and node state under `~/.cclg/nodes`. This is intentionally
simple and reviewable before adding SQLite, sync, or API layers.
