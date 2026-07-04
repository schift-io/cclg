# Benchmarks

CCLG is not evaluated like a model benchmark. The target is an agent memory
runtime:

- memory compiler
- retrieval fabric
- patch engine
- prompt injection
- adapter behavior
- source verification

## Priority

1. OAuth/API-capable agentic benchmarks.
2. Embedding-independent retrieval and mutation benchmarks.
3. Existing memory/tool/web/OS benchmarks.
4. Dense embedding benchmarks as optional ablations.

## Tier 0: Unit and Integration

Deterministic tests:

- schema validation
- source required
- patch application
- effective view exact match
- suppressed memory exclusion
- search ranking
- pack budget handling
- MCP tool listing and tool calls
- hook `additionalContext` injection
- code graph indexing/search

## Tier 1: CCLG-MutationBench

Tests whether correction, contradiction, preference drift, and deletion are
reflected in effective memory.

Example:

```text
Session 1: "Start as a Hermes extension."
Session 2: "It must support Claude Code and Codex too."

Expected:
old node = superseded or narrowed
new node = active
effective view = cross-agent support requirement
```

Metrics:

- `mutation_accuracy`
- `obsolete_memory_penalty`
- `supersession_edge_accuracy`
- `effective_view_accuracy`

## Tier 2: CCLG-RetrievalBench

Embedding-independent retrieval first:

- grep only
- BM25/token sparse
- graph adjacency
- temporal filter
- hybrid sparse
- dense optional

Question types:

- exact phrase
- date/time
- current state
- contradiction validity
- commitment lookup

Metrics:

- `source_recall@k`
- `active_node_recall@k`
- `superseded_false_positive_rate`
- `token_budget_per_answer`

## Tier 3: CCLG-HookBench

Validates adapters:

- prompt submission creates an ActiveMemoryPack
- only relevant memory is injected
- superseded memory is excluded
- token budget is respected
- tool results can be ingested
- compact preserves source references

## Tier 4: CCLG-OAuthBench

This is the product-grade benchmark.

It uses real or mock OAuth/API tools and checks whether a long-running agent can
maintain state through memory:

- GitHub issue/project state
- Gmail thread follow-up state
- Notion task/spec state
- Linear/Jira ticket state
- Google Drive or docs source references

The local MVP does not implement this yet; the repo documents it as the first
serious product evaluation track.

## Implemented Runner

The local runner is:

```bash
cclg bench run --suite all --repo-root "$PWD"
```

Implemented suites:

- `mutation`: verifies refine/supersede behavior and obsolete-memory suppression.
- `retrieval`: verifies embedding-independent sparse recall.
- `pack`: verifies ActiveMemoryPack excludes suppressed memory.
- `hook`: verifies `additionalContext` includes active memory.
- `mcp`: verifies `initialize`, `tools/list`, and `tools/call`.
- `codegraph`: verifies repo indexing, symbol extraction, and code search.
