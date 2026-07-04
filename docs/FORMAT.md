# CCLG Format

Canonical artifact:

```text
format/cclg.format.v0.1.toml
```

CCLG format은 DCLG/dclg.xml, 즉 DocLang 계열에서 원칙을 차용했다.

- version은 format의 일부다.
- `0.x` minor bump는 breaking으로 본다.
- 자유 prose보다 controlled vocabulary를 우선한다.
- field order는 소비자가 읽는 계약이다.
- validation gate가 format에 포함된다.
- prompt로 들어가는 pack은 token 낭비를 줄이기 위해 TOML을 기본 compact
  form으로 둔다.

참조 기준:

- DocLang: <https://doclang.ai/>
- Docling supported formats: <https://docling-project.github.io/docling/usage/supported_formats/>
- Docling repository: <https://github.com/docling-project/docling>

## Version

```toml
[format]
id = "cclg.format.v0.1"
version = "0.1"
namespace = "https://github.com/schift-io/cclg/ns/v0"
storage_encoding = "json"
prompt_encoding = "toml"
```

Record schema versions:

```toml
[versions]
store = "cclg.store.v0.1"
memory_node = "cclg.memory_node.v0.1"
memory_patch = "cclg.memory_patch.v0.1"
memory_edge = "cclg.edge.v0.1"
active_memory_pack = "cclg.active_memory_pack.v0.1"
code_graph = "cclg.code_graph.v0.1"
session = "cclg.session.v0.1"
hook_output = "cclg.hook_output.v0.1"
```

## Runtime Records

| Record | Stored as | Path |
| --- | --- | --- |
| `memory_node` | JSON | `nodes/{id}.json` |
| `memory_patch` | JSON | `patches/{id}.json` |
| `memory_edge` | JSON | `edges/{id}.json` |
| `session` | JSON | `sessions/{id}.json` |
| `active_memory_pack` | TOML/JSON runtime output | stdout/runtime |
| `code_graph` | JSON | `active/codegraphs/{repo}.json` |
| `hook_output` | JSON stdout | hook stdout |

## Required Fields

`MemoryNode`:

```text
schema_version, id, type, scope, key, content, status, confidence, priority,
created_at, updated_at, effective_from, effective_until, source, relations,
retrieval, metadata
```

`MemoryPatch`:

```text
schema_version, id, operation, target_ids, new_node_ids, reason, source,
confidence, resolution_policy, created_at, applied_at
```

`MemoryEdge`:

```text
schema_version, id, from, to, type, created_at, source_patch_id
```

`SessionState`:

```text
schema_version, id, agent, workspace, project, started_at, ended_at, status,
parent_session_id, branch_name, loaded_memory_ids, session_overlay_ids,
pending_patch_ids, active_task, policy, events, created_at, updated_at
```

## Compact Prompt Form

`cclg pack --format toml` emits:

```toml
schema_version = "cclg.active_memory_pack.v0.1"
query = "CCLG format"
generated_at = "2026-06-30T09:24:55+00:00"

[[memory]]
id = "mem_..."
type = "project_decision"
status = "active"
source = "patch:patch_..."
content = "CCLG preserves raw evidence and injects only active memory."

[[suppressed]]
id = "mem_..."
status = "superseded"
content = "Old memory that must not be used as current truth."

[budget]
max_nodes = 12
max_chars = 6000
used_chars = 68
```

Rule: `suppressed`는 현재 truth가 아니다. consumer는 이를 active memory로
재주입하면 안 된다.

## Validation

```bash
cclg validate docs/explainer/demo-store/nodes docs/explainer/demo-store/patches
cclg doctor --json
```

`doctor` checks:

- schema version match;
- source-grounded long-term nodes;
- relation targets;
- patch targets and new nodes;
- edge endpoints and source patch;
- active node supersession conflicts;
- session record shape.
