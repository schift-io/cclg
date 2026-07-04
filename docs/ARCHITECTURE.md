# Architecture

CCLG is the memory core of the ACMC idea from the source discussion:
Agentic Context Memory Compiler.

The local MVP keeps the cloud/API pieces out of the critical path. The first
job is to prove that memory can be source-grounded, mutable, searchable, and
injectable on this machine.

## Runtime Shape

```text
raw transcript / tool output
  -> memory node candidates
  -> CCLG nodes
  -> patches + edges
  -> effective view + session overlay
  -> ActiveMemoryPack
  -> agent prompt/context injection
```

## Components

- `cclg.store`: filesystem-backed local ledger under `~/.cclg`.
- `cclg.models`: CCLG node, patch, edge, source, session, and pack data models.
- `cclg.patches`: deterministic patch application.
- `cclg.session`: session state and session-scoped overlay memory.
- `cclg.schema`: schema validation used by `validate`, `doctor`, and `audit`.
- `cclg.retrieval`: embedding-independent token/phrase/BM25-style search.
- `cclg.pack`: ActiveMemoryPack compiler for prompt injection.
- `cclg.mcp_server`: local stdio MCP tools and memory.* aliases.
- `cclg.hooks`: Codex/Claude-style lifecycle hook adapter.
- `cclg.cli`: user-facing local CLI.

## Retrieval

- `cclg.retrieval`: grep / BM25 / graph / temporal modes, an auto router, and RRF
  fusion. `cclg index` persists embedding-independent indexes under `indexes/`.
- Dense retrieval is an optional, off-by-default provider with pluggable backends:
  - `local` — sentence-transformers on-device (needs `cclg[dense]`); good Korean
    default `ibm-granite/granite-embedding-97m-multilingual-r2`.
  - `ollama` — local daemon / any lightweight runtime speaking the Ollama API.
  - `openai`, `schift`, `google`, `cloudflare` — hosted embedding APIs (stdlib
    HTTP, no extra deps).
  - any OpenAI-compatible runtime (llama.cpp, LM Studio, vLLM) via the `openai`
    backend with a custom `--base-url`.
  Enable with `cclg dense enable --provider auto|local|ollama|openai|schift|google|cloudflare`.
  `auto` selects a backend from credentials present in the environment
  (`OPENAI_API_KEY`, `SCHIFT_API_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`,
  `CLOUDFLARE_API_TOKEN`, `OLLAMA_HOST`), else falls back to `local`. API keys are
  read from the environment only — never written to `config.json`. API backends
  make paid networked calls, so dense stays opt-in.
- Document embeddings are cached per node under `indexes/dense/cache.json`, keyed
  by `provider:model:node_id` and invalidated by a content hash. `cclg index`
  warms the cache; at query time only the query is embedded, so repeat searches
  cost one embedding call regardless of corpus size.
- The memory graph is also exported to `indexes/graph/memory.nt` (N-Triples) so an
  external SPARQL store such as Oxigraph can back graph queries without becoming a
  hard dependency. The in-process adjacency graph stays the default.

## Non-Goals for the Local MVP

- No hosted API.
- No dense embedding requirement (dense is opt-in, off by default).
- No required external graph database (in-process graph default; Oxigraph optional via N-Triples export).
- No automatic private data sync.
- No agent-specific hard dependency.

## Future API Boundary

The same format can later back:

- MCP tools: `memory.search`, `memory.pack`, `memory.patch`, `memory.audit`.
- hosted API: `/v1/memory/pack`, `/v1/memory/search`, `/v1/memory/patch`.
- adapters for Claude Code, Codex, Hermes, LangGraph, and server ReACT agents.

The format must stay the same when it moves from local-only to hosted mode.
