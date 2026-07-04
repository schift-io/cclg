# Tools and Hooks

CCLG has two local consumption surfaces:

1. `cclg-hook`: lifecycle hook adapter.
2. `cclg-mcp`: MCP stdio tool server.

No hosted API is required.

## Hook Adapter

Command:

```bash
cclg-hook user-prompt --include-codegraph --code-root "$PWD"
```

Input is JSON on stdin. The adapter accepts common fields such as `prompt`,
`user_prompt`, `message`, `session_id`, and raw hook payloads.

Output:

```json
{
  "schema_version": "cclg.hook_output.v0.1",
  "continue": true,
  "hookSpecificOutput": {
    "additionalContext": "schema_version = \"cclg.active_memory_pack.v0.1\"\n..."
  },
  "cclg": {
    "session_id": "session_...",
    "active_nodes": 5,
    "suppressed_nodes": 0
  }
}
```

The `additionalContext` field is the prompt-injection surface. It contains a
compact TOML ActiveMemoryPack and, when enabled, a CodeGraphPack. Suppressed
memory is labelled and must not be treated as active.

Post-tool and compact events are stored as session events and raw evidence:

```bash
cclg-hook post-tool
cclg-hook pre-compact
cclg-hook post-compact
```

## MCP Server

Command:

```bash
cclg-mcp
```

Tools exposed:

- `cclg.search`
- `cclg.grep`
- `cclg.bm25`
- `cclg.pack`
- `cclg.add`
- `cclg.patch`
- `cclg.raw`
- `cclg.code_index`
- `cclg.code_search`
- `cclg.audit`
- `cclg.recall`
- `cclg.cite`
- `cclg.conflicts`
- `cclg.resolve`
- `cclg.bench`
- `memory.search`
- `memory.grep`
- `memory.bm25`
- `memory.pack`
- `memory.patch`
- `memory.audit`
- `memory.recall`
- `memory.cite`
- `memory.conflicts`
- `memory.resolve`

`recall`/`cite` return source-grounded provenance (raw spans + quote) for active
memory. `conflicts` lists `conflict_pending` nodes; `resolve` reconciles them
with a `resolve_conflict` patch.

Use `grep` for exact wording, IDs, dates, filenames, code, and error strings.
Use `search` or `bm25` for lexical active-memory retrieval. Dense retrieval is
not required for local MVP.

The server supports standard Content-Length framed JSON-RPC over stdio and a
`--line-delimited` mode for smoke tests.

## Config Snippets

Safe examples are installed by `scripts/apply-local.sh`:

```text
~/.codex/hooks.cclg-example.json
~/.codex/mcp.cclg-example.json
~/.claude/settings.cclg-example.json
```

They are not auto-merged into existing user config because existing hook files
may contain unrelated user workflow.
