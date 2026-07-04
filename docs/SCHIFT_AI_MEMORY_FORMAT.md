# Schift AI Memory Format Integration

CCLG should be the canonical memory format used by Schift AI Memory. Schift AI
Memory should provide the product wrapper: install, auth, redaction, queue,
upload, hosted search, and Schift bucket routing.

## Ownership

```text
CCLG repo
  owns: memory schema, patches, provenance, active packs, code graph packs,
        local validation, import/export contract

Schift AI Memory repo
  owns: npm installer, OAuth/API key flow, Schift bucket/collection routing,
        upload queue, redaction policy, hosted MCP search/fetch
```

Do not duplicate CCLG node, patch, or pack semantics in Schift AI Memory. If the
Schift package needs memory data, it should consume a CCLG record or CCLG export.

## Target Shape

Schift AI Memory should wrap CCLG data with Schift transport metadata:

```text
CCLG record or pack
        |
        v
+--------------------------------------+
| Schift AI Memory envelope            |
|                                      |
| schift:                              |
|   org_id / user_id                   |
|   bucket / collection                |
|   upload policy / redaction policy   |
|   queue and upload status            |
|                                      |
| cclg:                                |
|   schema_version                     |
|   node_ids / patch_ids / session_id  |
|   active pack or session summary     |
|   source provenance                  |
+--------------------------------------+
        |
        v
Schift bucket document
```

Example envelope:

```json
{
  "schema_version": "schift.ai_memory_envelope.v0.1",
  "kind": "cclg_session_summary",
  "schift": {
    "org_id": "org_...",
    "user_id": "usr_...",
    "bucket": "default",
    "collection": "__schift_ai_daily_log",
    "upload_policy": "summary_metadata_only",
    "redaction": "default"
  },
  "cclg": {
    "schema_version": "cclg.active_memory_pack.v0.1",
    "session_id": "session_...",
    "node_ids": ["mem_..."],
    "patch_ids": ["patch_..."],
    "source_labels": ["manual:quickstart"],
    "summary": "What the coding agent should retain from this session."
  }
}
```

## Current To Target

Current Schift AI Memory event:

```text
AiMemoryEvent
  source / harness / event_kind
  org_id / user_id / bucket / collection
  job title / intent / status
  summary / metadata
```

Target Schift AI Memory event:

```text
AiMemoryEvent
  source / harness / event_kind
  org_id / user_id / bucket / collection
  job metadata as transport metadata only
  cclg payload:
    CCLG session summary, ActiveMemoryPack, MemoryNode refs, or raw evidence refs
```

The existing job/event fields can remain as Schift upload metadata. They should
not become the memory model.

## Implementation Phases

1. Stabilize the CCLG export contract.

```text
cclg export schift --session <id>
cclg export schift --pack-query "<task>"
cclg export schift --node <mem_id>
```

The export should emit a CCLG-shaped payload with provenance and no Schift auth
fields.

2. Add a Schift envelope in Schift AI Memory.

```text
read CCLG export
  -> apply redaction/upload policy
  -> add org/user/bucket/collection envelope
  -> enqueue or upload to Schift
```

3. Preserve local-first boundaries.

```text
CCLG must not require SCHIFT_API_KEY.
CCLG must not write ~/.schift.
Schift AI Memory must not mutate ~/.cclg except through explicit import commands.
```

4. Make hosted retrieval return CCLG-shaped results.

```text
Schift MCP search/fetch
  -> returns Schift document metadata
  -> includes embedded cclg.schema_version and cclg source refs when present
```

5. Add optional import back into CCLG.

```text
Schift search result
  -> cclg raw import with source provenance
  -> optional manual promotion to MemoryNode
```

Search results must not auto-promote into active memory.

## Non-Goals

- Do not make CCLG depend on Schift auth, buckets, or API keys.
- Do not let Schift AI Memory define a competing long-term memory schema.
- Do not upload raw transcript by default.
- Do not auto-sync every local CCLG node into Schift.
- Do not auto-promote Schift search results into active CCLG memory.

## Acceptance Criteria

- Schift AI Memory can upload a CCLG session summary without inventing a new
  memory shape.
- Uploaded Schift documents include CCLG schema/version/provenance metadata.
- CCLG remains fully usable with no Schift account or network.
- Schift search/fetch can return enough CCLG metadata for later local citation or
  raw-evidence import.
- Tests cover the mapping from CCLG export payload to Schift envelope.
