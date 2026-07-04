from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .codegraph import build_code_graph, search_code_graph
from .hooks import user_prompt_context
from .models import MemoryNode, MemoryPatch
from .pack import compile_pack
from .patches import active_nodes, apply_patch, classify_patch, detect_patch_candidates
from .retrieval import search_nodes
from .store import CCLGStore


def run_benchmarks(*, suite: str = "all", repo_root: str | None = None) -> dict[str, Any]:
    suites = {
        "mutation": bench_mutation,
        "detection": bench_detection,
        "retrieval": bench_retrieval,
        "pack": bench_pack,
        "hook": bench_hook,
        "mcp": bench_mcp,
        "codegraph": lambda: bench_codegraph(repo_root),
    }
    selected = suites.keys() if suite == "all" else [suite]
    results = []
    for name in selected:
        if name not in suites:
            results.append({"suite": name, "ok": False, "error": f"unknown suite {name}"})
            continue
        try:
            results.append({"suite": name, "ok": True, **suites[name]()})
        except AssertionError as exc:
            results.append({"suite": name, "ok": False, "error": str(exc)})
    scored = [result.get("score", 0.0) if result["ok"] else 0.0 for result in results]
    return {
        "ok": all(result["ok"] for result in results),
        "score": round(sum(scored) / max(1, len(scored)), 4),
        "score_percent": round((sum(scored) / max(1, len(scored))) * 100, 2),
        "results": results,
    }


def bench_mutation() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        store = CCLGStore(Path(tmp))
        old = MemoryNode.create(content="ACMC is Hermes-only.", source="bench:session1", node_type="project_decision")
        store.write_node(old)
        patch = MemoryPatch.create(
            operation="refine",
            target_ids=[old.id],
            reason="Cross-agent support clarified.",
            new_content="ACMC supports Claude Code, Codex, Hermes, and ReACT agents.",
            source="bench:session2",
        )
        apply_patch(store, patch)
        active = active_nodes(store)
        assert len(active) == 1, "expected exactly one active node"
        assert "Codex" in active[0].content, "new active node should contain expanded target"
        assert store.read_node(old.id).status == "superseded", "old node should be superseded"

        # expand must not leave the original active as a stale duplicate (PRD §7.2)
        base = MemoryNode.create(content="Support Hermes.", source="bench:e1", node_type="project_decision")
        store.write_node(base)
        apply_patch(store, MemoryPatch.create(operation="expand", target_ids=[base.id], reason="add codex", new_content="Support Hermes and Codex."))
        expand_active = [n for n in active_nodes(store) if "Support Hermes" in n.content]
        assert len(expand_active) == 1, "expand left a duplicate active node"
        assert store.read_node(base.id).status == "superseded", "expand should retire its target"

        # rollback must restore the exact prior status, not a blanket active (PRD §7.2)
        node = MemoryNode.create(content="rollback me", source="bench:r1")
        store.write_node(node)
        apply_patch(store, MemoryPatch.create(operation="deprecate", target_ids=[node.id], reason="dep"))
        sup = MemoryPatch.create(operation="supersede", target_ids=[node.id], reason="sup", new_content="replacement")
        apply_patch(store, sup)
        from .cli import rollback_patch

        rollback_patch(store, sup.id, reason="undo")
        assert store.read_node(node.id).status == "deprecated", "rollback did not restore exact prior status"

        # scope precedence: project memory overrides global memory for the same key (PRD §7.1)
        g = MemoryNode.create(content="global rule", source="bench:g", scope={"agent": "global"})
        g.key = "bench.scope"
        p = MemoryNode.create(content="project rule", source="bench:p", scope={"project": "repo"})
        p.key = "bench.scope"
        store.write_node(g)
        store.write_node(p)
        keyed = [n.content for n in active_nodes(store) if n.key == "bench.scope"]
        assert keyed == ["project rule"], f"scope precedence failed: {keyed}"

        return {"score": 1.0, "effective_view_exact_match": True, "obsolete_memory_suppressed": True, "expand_no_duplicate": True, "rollback_exact": True, "scope_precedence": True}


def bench_detection() -> dict[str, Any]:
    nodes = [
        MemoryNode.create(content="ACMC supports Hermes only.", source="bench:d1", node_type="project_decision"),
        MemoryNode.create(content="Dense retrieval is required.", source="bench:d2", node_type="constraint"),
    ]
    assert classify_patch("아니 그게 아니라 정정할게") == "supersede", "correction trigger not classified"
    assert classify_patch("이제 폐기해") == "deprecate", "temporal/deprecate not classified"
    assert classify_patch("Codex도 지원해야 해") == "expand", "expansion not classified"
    assert classify_patch("그대로 진행해") is None, "non-trigger turn should not classify"
    cands = detect_patch_candidates("Hermes also Codex 포함해야 해", nodes)
    assert cands and cands[0]["operation"] == "expand", "detection did not produce expand candidate"
    assert cands[0]["target_id"] == nodes[0].id, "detection targeted wrong node"
    return {"score": 1.0, "classification_accuracy": 1.0, "candidate_recall": 1.0}


def bench_retrieval() -> dict[str, Any]:
    from .retrieval import get_dense_provider, grep_search, graph_search, route_query, search_memory

    local = MemoryNode.create(content="CCLG is local-first and stores raw transcript locally.", source="bench:local", tags=["local-first"])
    adapter = MemoryNode.create(content="CCLG supports Claude Code, Codex, Hermes, and ReACT adapters.", source="bench:adapter", tags=["codex"])
    nodes = [local, adapter]
    hits = search_nodes("Codex adapter", nodes)
    assert hits and hits[0].node.id == adapter.id, "retrieval should rank adapter memory first"

    # grep is exact
    assert grep_search("ReACT", nodes)[0].node.id == adapter.id, "grep exact match failed"
    assert not grep_search("nonexistent-phrase", nodes), "grep should not match absent text"

    # graph follows relations from a seed hit
    adapter.relations["depends_on"] = [local.id]
    graph_hits = {hit.node.id for hit in graph_search("Codex adapter", nodes)}
    assert local.id in graph_hits, "graph search should pull in related node via depends_on"

    # router picks exact path for id/date/quoted queries
    assert route_query('"exact words"')[0] == "grep", "router should prefer grep for quoted text"
    assert route_query("project decision")[0] == "bm25", "router should prefer bm25 for conceptual query"

    # dense is optional and disabled by default; auto/dense degrade gracefully
    assert get_dense_provider(None) is None, "dense must be off by default"
    auto = search_memory("Codex adapter", nodes, mode="auto")
    assert auto and auto[0].node.id == adapter.id, "auto fusion should still rank adapter first"
    degraded = search_memory("Codex adapter", nodes, mode="dense", dense=None)
    assert degraded and degraded[0].node.id == adapter.id, "dense mode must fall back when disabled"

    return {"score": 1.0, "active_node_recall_at_1": 1.0, "grep_exact": True, "graph_expand": True, "dense_required": False}


def bench_pack() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        store = CCLGStore(Path(tmp))
        node = MemoryNode.create(content="Superseded memory should not be injected.", source="bench:old")
        store.write_node(node)
        apply_patch(
            store,
            MemoryPatch.create(
                operation="supersede",
                target_ids=[node.id],
                reason="Current memory changed.",
                new_content="Only active memory should be injected.",
            ),
        )
        pack = compile_pack(store, "active memory")
        contents = [entry["content"] for entry in pack.memory_nodes]
        assert contents == ["Only active memory should be injected."], "pack leaked suppressed memory"
        return {"score": 1.0, "suppressed_false_positive_rate": 0.0}


def bench_hook() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        store = CCLGStore(Path(tmp))
        store.write_node(MemoryNode.create(content="Hook should inject active memory through additionalContext.", source="bench:hook"))
        output = user_prompt_context(store, {"prompt": "hook memory", "session_id": "bench"}, code_root=Path(tmp), max_chars=2000, include_codegraph=False)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "additionalContext" in output["hookSpecificOutput"], "hook missing additionalContext"
        assert "Hook should inject" in context, "hook did not include active memory"
        return {"score": 1.0, "additional_context": True}


def bench_mcp() -> dict[str, Any]:
    from .mcp_server import handle_message

    with tempfile.TemporaryDirectory() as tmp:
        store = CCLGStore(Path(tmp))
        init = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, store)
        assert init and init["result"]["capabilities"]["tools"] == {}, "initialize failed"
        listed = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, store)
        names = [tool["name"] for tool in listed["result"]["tools"]]
        assert "cclg.pack" in names and "cclg.code_search" in names, "tool list incomplete"
        for required in ["memory.recall", "memory.cite", "memory.conflicts", "memory.resolve"]:
            assert required in names, f"missing required MCP tool {required}"
        called = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cclg.add", "arguments": {"content": "MCP adds memory.", "source": "bench:mcp"}}}, store)
        assert called and not called["result"].get("isError"), "tool call failed"
        node_id = called["result"]["structuredContent"]["id"]
        cite = handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "memory.cite", "arguments": {"memory_id": node_id}}}, store)
        assert cite and cite["result"]["structuredContent"]["memory_id"] == node_id, "cite failed"
        recall = handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "memory.recall", "arguments": {"query": "MCP memory"}}}, store)
        assert recall and not recall["result"].get("isError"), "recall failed"
        return {"score": 1.0, "tools_listed": len(names), "tool_call": True, "recall": True, "cite": True}


def bench_codegraph(repo_root: str | None) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve() if repo_root else Path(__file__).resolve().parents[2]
    graph = build_code_graph(root)
    hits = search_code_graph(graph, "compile_pack MemoryPatch", limit=10)
    assert graph.files, "code graph has no files"
    assert graph.symbols, "code graph has no symbols"
    assert hits, "code graph search returned no hits"
    coverage_score = min(1.0, (len(graph.files) > 0) / 3 + (len(graph.symbols) > 0) / 3 + (len(graph.edges) > 0) / 3)
    return {"score": round(coverage_score, 4), "files": len(graph.files), "symbols": len(graph.symbols), "edges": len(graph.edges), "hits": len(hits)}


def main() -> None:
    print(json.dumps(run_benchmarks(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
