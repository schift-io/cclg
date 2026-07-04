# Benchmark Scorecard

CCLG needs two score layers:

1. local deterministic product gates that run in CI and during install smoke;
2. external comparison suites that show where the memory/runtime stands against
   existing agent systems.

## Local Product Gates

Run:

```bash
cclg bench run --suite all --repo-root "$PWD" --json
```

Current implemented suites:

| Suite | What it proves | Score signal |
| --- | --- | --- |
| `mutation` | stale memory is suppressed after patch | effective view exact match |
| `retrieval` | sparse search finds active memory without embeddings | active node recall@1 |
| `pack` | ActiveMemoryPack excludes suppressed nodes | false positive rate |
| `hook` | hook emits `additionalContext` | injection present |
| `mcp` | MCP initialize/list/call works | tool discovery + call |
| `codegraph` | repo files/symbols/edges are indexed | graph coverage |

The local runner is a release gate, not a marketing benchmark. It proves local
mutation, retrieval, pack, hook, MCP, and code graph behavior on deterministic
fixtures.

## External Benchmark Tracks

These tracks were selected from current public benchmark work rather than from
generic RAG leaderboards. CCLG needs to prove three separate things: memory
freshness, tool/runtime usability, and code-context selection.

### Memory

Use these to benchmark CCLG's memory runtime:

- LoCoMo: very long-term conversational memory with QA, event summarization, and
  multimodal dialogue generation tasks.
- LongMemEval / LongMemEval-V2: long-term chat and agent-memory evaluation,
  including larger, environment-specific histories.
- FAMA: Forgetting-Aware Memory Accuracy. This is especially important for CCLG
  because stale-memory suppression is central to the product.

Target metrics:

- `active_node_recall@k`
- `source_recall@k`
- `FAMA`
- `obsolete_memory_false_positive_rate`
- `tokens_per_retrieval`
- `update_or_forget_success`

### Tool Use

Use MCP-Bench, MCPBench, LiveMCPBench, or MCP-Universe style tasks for:

- tool discovery;
- tool selection;
- multi-step tool call correctness;
- state persistence across turns;
- OAuth/API-style task continuity;
- large toolset navigation.

Target metrics:

- `tool_discovery_success`
- `tool_call_success`
- `state_continuity_success`
- `cost_per_solved_task`
- `tokens_before_first_action`
- `wrong_tool_rate`

### Code Graph

Use repo-map/code retrieval tasks inspired by Aider's repo map and Tree-sitter
code graph work:

- find the right file for a change request;
- find a symbol definition;
- identify import/dependency edges;
- suggest impact radius from graph + git churn;
- compare graph-selected context against plain grep context.

Target metrics:

- `file_recall@k`
- `symbol_recall@k`
- `edge_recall@k`
- `context_tokens_per_correct_file`
- `change_success_with_graph_context`
- `wrong_file_context_rate`

## Reference Links

- LoCoMo: <https://snap-research.github.io/locomo/>
- LoCoMo paper: <https://arxiv.org/abs/2402.17753>
- LongMemEval: <https://github.com/xiaowu0162/longmemeval>
- LongMemEval-V2: <https://arxiv.org/html/2605.12493v1>
- FAMA / forgetting-aware memory: <https://arxiv.org/abs/2604.20006>
- MCP-Bench: <https://github.com/Accenture/mcp-bench>
- MCPBench: <https://github.com/modelscope/mcpbench>
- LiveMCPBench: <https://icip-cas.github.io/LiveMCPBench/>
- MCP-Universe: <https://github.com/SalesforceAIResearch/MCP-Universe>
- Aider repo map: <https://aider.chat/docs/repomap.html>
- Aider Tree-sitter repo map: <https://aider.chat/2023/10/22/repomap.html>
- Codebase-Memory Tree-sitter/MCP code graph paper:
  <https://arxiv.org/html/2603.27277v1>

## Release Bar

Before public release:

- Local deterministic gates complete.
- MCP smoke completes.
- Hook smoke completes.
- CodeGraphPack smoke on CCLG itself completes.
- Public install smoke from a clean checkout completes.

Before claiming benchmark superiority:

- Run LoCoMo or LongMemEval through a CCLG adapter.
- Run MCP-Bench or a smaller compatible MCP task set.
- Run a code retrieval benchmark against at least 3 repos.
- Publish raw harness configs and not only aggregate scores.
