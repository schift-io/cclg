# Research Notes

Implementation choices were checked against current public references and common
repo-map/memory-server patterns.

## Hook Surface

OpenAI Codex and Claude Code both expose deterministic lifecycle scripts around
events such as prompt submission, tool use, and compact. CCLG consumes that
surface through:

```bash
cclg-hook user-prompt --include-codegraph --code-root "$PWD"
```

The hook returns `hookSpecificOutput.additionalContext` so active CCLG memory and
code graph context can be injected before model work.

## MCP Tool Surface

MCP tools are exposed through `tools/list` and called through `tools/call`.
`cclg-mcp` implements:

- `cclg.search`
- `cclg.pack`
- `cclg.add`
- `cclg.patch`
- `cclg.raw`
- `cclg.code_index`
- `cclg.code_search`
- `cclg.bench`

## Code Graph Surface

Aider's repo map is the strongest practical baseline: summarize a repo with
important classes/functions, signatures, and relationships so the agent picks
the right context before reading full files. CCLG starts with a dependency-free
version of that idea:

- files
- symbols
- imports
- define/import edges
- git churn

Tree-sitter/LSP/SCIP can replace the regex/AST extractor later without changing
the CodeGraphPack contract.

## Benchmark References

Memory:

- LoCoMo: <https://snap-research.github.io/locomo/>
- LongMemEval: <https://github.com/xiaowu0162/longmemeval>
- FAMA: <https://arxiv.org/abs/2604.20006>

Tool/MCP:

- MCP-Bench: <https://github.com/Accenture/mcp-bench>
- MCPBench: <https://github.com/modelscope/mcpbench>
- LiveMCPBench: <https://icip-cas.github.io/LiveMCPBench/>
- MCP-Universe: <https://github.com/SalesforceAIResearch/MCP-Universe>

Code graph:

- Aider repo map: <https://aider.chat/docs/repomap.html>
- Aider Tree-sitter repo map write-up: <https://aider.chat/2023/10/22/repomap.html>
- Codebase-Memory Tree-sitter/MCP code graph paper:
  <https://arxiv.org/html/2603.27277v1>

## GitHub Examples Checked

Memory/MCP examples found through GitHub search:

- `memory-graph/memory-graph`: <https://github.com/memory-graph/memory-graph>
- `CodeAbra/iai-personal-memory-engine`:
  <https://github.com/CodeAbra/iai-personal-memory-engine>
- `IzumiSy/mcp-duckdb-memory-server`:
  <https://github.com/IzumiSy/mcp-duckdb-memory-server>
- `iAchilles/memento`: <https://github.com/iAchilles/memento>
- `danielmarbach/mnemonic`: <https://github.com/danielmarbach/mnemonic>

Repo-map/codegraph examples:

- `pdavis68/RepoMapper`: <https://github.com/pdavis68/RepoMapper>
- Aider repository map docs and tree-sitter repo-map write-up.
