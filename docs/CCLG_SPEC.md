# CCLG Spec

CCLG means Canonical Chat Ledger Graph.

It is not a summary file and not just a vector DB. It is a versioned,
source-grounded graph of memory nodes, patches, edges, session overlays, and
effective runtime packs.

Canonical schema:

```text
format/cclg.format.v0.1.toml
```

## Records

| Record | Schema | Purpose |
| --- | --- | --- |
| MemoryNode | `cclg.memory_node.v0.1` | Source-grounded memory unit |
| MemoryPatch | `cclg.memory_patch.v0.1` | First-class memory mutation |
| MemoryEdge | `cclg.edge.v0.1` | Relation created by patches |
| SessionState | `cclg.session.v0.1` | Branch/overlay state for a running agent session |
| ActiveMemoryPack | `cclg.active_memory_pack.v0.1` | Compact context injected before model work |
| CodeGraph | `cclg.code_graph.v0.1` | File/symbol/import/churn context for repo tasks |

## Required Invariants

- Long-term nodes require `source.label` or `source.raw_spans`.
- Raw transcript and source artifacts are evidence; summaries are not evidence.
- Superseded, expired, deprecated, forgotten, and discarded nodes do not appear
  in the default effective view.
- `active_session` nodes appear only when building a pack for that session id.
- Patches are append-only and record `target_ids`, `new_node_ids`, `reason`,
  `source`, `resolution_policy`, and `applied_at`.
- Relation edges must point to existing nodes and the patch that created them.
- Hook/MCP consumers read `ActiveMemoryPack`, not the raw graph directly.

## Mutation Semantics

```text
old MemoryNode --(Patch refine)--> new MemoryNode
new MemoryNode --(Edge refines)--> old MemoryNode
old status = superseded
new status = active
```

Allowed patch operations:

```text
create update supersede refine expand narrow merge split expire deprecate forget
resolve_conflict rollback
```

Allowed edge types:

```text
supersedes refines expands narrows contradicts depends_on derived_from
temporary_override source_of blocks resolves
```

## Effective View

The effective view is the materialized list of currently valid memory nodes for
the requested scope/session.

Default included:

```text
status = active
```

Session pack additionally includes:

```text
status = active_session AND scope.session = requested session id
```

Default excluded:

```text
pending superseded deprecated expired forgotten conflict_pending
pending_promotion promoted discarded active_session from other sessions
```
