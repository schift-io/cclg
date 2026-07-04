# APM + CCLG Multi-Agent Session Map

This is the working constitution-level map for one multi-agent coding session.

Core split:

- APM owns package and runtime wiring: agents, tools, prompts, hooks, and deploy profile.
- CCLG owns the canonical memory format: raw evidence, nodes, patches, edges, sessions, and runtime packs.
- Schift Memory owns the product runtime around CCLG: auth, redaction, upload queue, hosted search, bucket routing, MCP/A2A endpoint.

```text
                       one user task / one session

+----------------+     +-----------------------------------------------+
| User Session   | --> | APM Agent Mesh / Schift Profile                |
| session_id     |     | defines agents, tools, prompts, hook wiring    |
+----------------+     |                                               |
                       |  +------------+    +------------------------+  |
                       |  | Root Agent | -> | Researcher / Executor  |  |
                       |  | orchestrates    | Verifier / Critic      |  |
                       |  +------------+    +------------------------+  |
                       +-----------------------+-----------------------+
                                               |
                                               v
+-----------------------------------------------------------------------+
| Hooks                                                                 |
| session_start | user_prompt_submit | post_tool_use | subagent_stop    |
| pre_compact   | post_compact       | stop/session_end                 |
+-----------------------+-------------------------------+---------------+
                        |                               |
                        | builds prompt context          | writes evidence
                        v                               v
             +--------------------+          +--------------------------+
             | ActiveMemoryPack   |          | CCLG Store               |
             | CodeGraphPack      |          | raw/                     |
             | injected context   |          | sessions/                |
             +----------+---------+          | nodes/ MemoryNode        |
                        |                    | patches/ MemoryPatch     |
                        |                    | edges/ MemoryEdge        |
                        |                    | active/codegraphs/        |
                        |                    | audit/                    |
                        |                    | indexes/                  |
                        |                    +------------+-------------+
                        |                                 |
                        v                                 v
              agents receive only              +------------------------+
              active scoped packs              | Schift Memory Runtime  |
                                               | auth / redaction       |
                                               | sync queue             |
                                               | hosted search/fetch    |
                                               | MCP / A2A endpoint     |
                                               +------------------------+
```

## Hook Semantics

| Hook | CCLG write | CCLG read | Result |
| --- | --- | --- | --- |
| `session_start` | `sessions/{id}.json`, audit event | optional active scope | Starts the session boundary. |
| `user_prompt_submit` | raw prompt, session event, audit event | effective `MemoryNode` view, optional `CodeGraph` | Emits `ActiveMemoryPack` in hook `additionalContext`. |
| `post_tool_use` | raw tool result, session event, audit event | none by default | Preserves provenance without auto-promoting it. |
| `subagent_stop` | raw handoff, session event, artifact refs | optional session pack | Root agent can cite subagent output without treating it as truth. |
| `pre_compact` / `post_compact` | compact input/output, session event | effective session view | Creates patch candidates or session summary evidence. |
| `stop` / `session_end` | terminal session event, optional overlay policy | pending overlays | Promotes, discards, or leaves session-scoped memory pending. |

## CCLG Pieces Used

- `CCLGStore`: filesystem store rooted at `CCLG_HOME` or `~/.cclg`.
- `SessionState`: branch/overlay state for the running session.
- `MemoryNode`: source-grounded long-term memory.
- `MemoryPatch`: append-only mutation record for refine, supersede, forget, rollback, and related changes.
- `MemoryEdge`: relation created by patches.
- `ActiveMemoryPack`: compact context injected into agents.
- `CodeGraph`: repo file/symbol/import/churn context for coding tasks.
- `Audit`: append-only operational trace for memory writes and session events.

`AgentHandoff` is proposed as a Schift/APM envelope. In current CCLG terms it should be stored as raw evidence plus a session event and artifact refs, not as an active `MemoryNode` unless a later patch explicitly promotes it.

## Storage Tree

```text
~/.cclg/
  config.json
  raw/
  sessions/
  nodes/
  patches/
  edges/
  active/
    codegraphs/
  audit/
    memory_audit.jsonl
  indexes/
```

## Boundary Rules

- APM does not define a competing memory schema.
- CCLG does not require Schift auth, buckets, or network.
- Schift Memory does not mutate `~/.cclg` except through explicit import commands.
- Hook/MCP consumers read `ActiveMemoryPack`, not raw graph state.
- Suppressed, forgotten, expired, deprecated, or superseded memory is not injected as active context.
