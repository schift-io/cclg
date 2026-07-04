from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cclg.cli import build_citation, rollback_patch
from cclg.mcp_server import handle_message
from cclg.models import MemoryNode, MemoryPatch
from cclg.pack import compile_pack
from cclg.patches import active_nodes, apply_patch, classify_patch, conflict_nodes, detect_patch_candidates
from cclg.schema import validate_node
from cclg.session import end_session, fork_session, merge_session, promote_session_node, start_session, write_overlay_node
from cclg.store import CCLGStore


def fresh() -> CCLGStore:
    store = CCLGStore(Path(tempfile.mkdtemp()))
    store.init()
    return store


class ScopePrecedenceTests(unittest.TestCase):
    def test_project_overrides_global_for_same_key(self) -> None:
        store = fresh()
        g = MemoryNode.create(content="global value", source="t", scope={"agent": "global"})
        g.key = "k.x"
        p = MemoryNode.create(content="project value", source="t", scope={"project": "repo"})
        p.key = "k.x"
        store.write_node(g)
        store.write_node(p)
        keyed = [n.content for n in active_nodes(store) if n.key == "k.x"]
        self.assertEqual(keyed, ["project value"])

    def test_keyless_nodes_are_never_collapsed(self) -> None:
        store = fresh()
        store.write_node(MemoryNode.create(content="fact one", source="t"))
        store.write_node(MemoryNode.create(content="fact two", source="t"))
        self.assertEqual(len(active_nodes(store)), 2)


class PatchSemanticsTests(unittest.TestCase):
    def test_expand_retires_target(self) -> None:
        store = fresh()
        base = MemoryNode.create(content="Support Hermes.", source="t", node_type="project_decision")
        store.write_node(base)
        apply_patch(store, MemoryPatch.create(operation="expand", target_ids=[base.id], reason="x", new_content="Support Hermes and Codex."))
        self.assertEqual(len(active_nodes(store)), 1)
        self.assertEqual(store.read_node(base.id).status, "superseded")

    def test_rollback_restores_exact_prior_status(self) -> None:
        store = fresh()
        node = MemoryNode.create(content="old", source="t")
        store.write_node(node)
        apply_patch(store, MemoryPatch.create(operation="deprecate", target_ids=[node.id], reason="dep"))
        sup = MemoryPatch.create(operation="supersede", target_ids=[node.id], reason="sup", new_content="new")
        apply_patch(store, sup)
        rollback_patch(store, sup.id, reason="undo")
        restored = store.read_node(node.id)
        self.assertEqual(restored.status, "deprecated")
        self.assertNotIn(sup.new_node_ids[0], restored.relations.get("superseded_by", []))


class DetectionTests(unittest.TestCase):
    def test_classification_triggers(self) -> None:
        self.assertEqual(classify_patch("아니 정정할게"), "supersede")
        self.assertEqual(classify_patch("이제 폐기해"), "deprecate")
        self.assertEqual(classify_patch("Codex도 포함해야 해"), "expand")
        self.assertIsNone(classify_patch("좋아 그대로 진행"))

    def test_detect_targets_relevant_node(self) -> None:
        nodes = [
            MemoryNode.create(content="ACMC supports Hermes only.", source="t", node_type="project_decision"),
            MemoryNode.create(content="Dense retrieval is required.", source="t"),
        ]
        cands = detect_patch_candidates("Hermes also Codex 포함해야 해", nodes)
        self.assertTrue(cands)
        self.assertEqual(cands[0]["operation"], "expand")
        self.assertEqual(cands[0]["target_id"], nodes[0].id)


class SessionLifecycleTests(unittest.TestCase):
    def test_end_promote_moves_overlay_to_longterm(self) -> None:
        store = fresh()
        start_session(store, session_id="s1", project="repo")
        node = write_overlay_node(store, session_id="s1", content="overlay fact")
        end_session(store, "s1", policy="promote")
        promoted = store.read_node(node.id)
        self.assertEqual(promoted.status, "active")
        self.assertIsNone(promoted.scope.get("session"))
        self.assertIn(promoted.content, [n.content for n in active_nodes(store)])

    def test_end_discard_removes_overlay(self) -> None:
        store = fresh()
        start_session(store, session_id="s1")
        node = write_overlay_node(store, session_id="s1", content="overlay fact")
        end_session(store, "s1", policy="discard")
        self.assertEqual(store.read_node(node.id).status, "discarded")

    def test_promote_single_node(self) -> None:
        store = fresh()
        start_session(store, session_id="s1")
        node = write_overlay_node(store, session_id="s1", content="overlay fact")
        promote_session_node(store, session_id="s1", node_id=node.id)
        self.assertEqual(store.read_node(node.id).status, "active")

    def test_fork_creates_independent_branch(self) -> None:
        store = fresh()
        start_session(store, session_id="parent", project="repo")
        child = fork_session(store, "parent", branch_name="alt")
        self.assertEqual(child["parent_session_id"], "parent")
        self.assertEqual(child["branch_name"], "alt")
        self.assertNotEqual(child["id"], "parent")

    def test_merge_flags_conflict_on_shared_key(self) -> None:
        store = fresh()
        existing = MemoryNode.create(content="global rule", source="t", scope={"agent": "global"})
        existing.key = "k.shared"
        store.write_node(existing)
        start_session(store, session_id="s1", project="repo")
        overlay = write_overlay_node(store, session_id="s1", content="overlay rule")
        overlay.key = "k.shared"
        store.write_node(overlay)
        result = merge_session(store, "s1")
        self.assertIn(overlay.id, result["conflict_node_ids"])
        self.assertTrue(conflict_nodes(store))


class RetrievalModeTests(unittest.TestCase):
    def _nodes(self):
        a = MemoryNode.create(content="CCLG supports Codex and Claude Code adapters.", source="t", tags=["codex"])
        b = MemoryNode.create(content="CCLG is local-first.", source="t", tags=["local"])
        return a, b

    def test_grep_is_exact(self) -> None:
        from cclg.retrieval import grep_search

        a, b = self._nodes()
        self.assertEqual(grep_search("Codex", [a, b])[0].node.id, a.id)
        self.assertEqual(grep_search("absent", [a, b]), [])

    def test_router_prefers_grep_for_exact_signals(self) -> None:
        from cclg.retrieval import route_query

        self.assertEqual(route_query("mem_abc123")[0], "grep")
        self.assertEqual(route_query("2026-06-30")[0], "grep")
        self.assertEqual(route_query("project decision rationale")[0], "bm25")

    def test_graph_follows_relations(self) -> None:
        from cclg.retrieval import graph_search

        a, b = self._nodes()
        a.relations["refines"] = [b.id]
        ids = {hit.node.id for hit in graph_search("Codex", [a, b])}
        self.assertIn(b.id, ids)

    def test_dense_off_by_default_and_graceful(self) -> None:
        from cclg.retrieval import get_dense_provider, search_memory

        a, b = self._nodes()
        self.assertIsNone(get_dense_provider(None))
        self.assertIsNone(get_dense_provider({"dense": {"enabled": False}}))
        hits = search_memory("Codex", [a, b], mode="dense", dense=None)
        self.assertEqual(hits[0].node.id, a.id)

    def test_dense_auto_selects_local_without_credentials(self) -> None:
        import os
        from unittest import mock

        from cclg.dense import LocalBackend, PROVIDER_ENV, detect_provider
        from cclg.retrieval import get_dense_provider

        cred_vars = [name for names in PROVIDER_ENV.values() for name in names] + ["OLLAMA_HOST"]
        cleared = {name: "" for name in cred_vars}
        with mock.patch.dict(os.environ, cleared, clear=False):
            for name in cred_vars:
                os.environ.pop(name, None)
            provider = get_dense_provider({"dense": {"enabled": True, "provider": "auto"}})
            self.assertIsInstance(provider, LocalBackend)
            self.assertEqual(detect_provider(), "local")

    def test_dense_provider_registry_and_unknown(self) -> None:
        from cclg.dense import OllamaBackend, OpenAIBackend, resolve_provider

        self.assertIsInstance(resolve_provider({"dense": {"enabled": True, "provider": "ollama"}}), OllamaBackend)
        self.assertIsInstance(resolve_provider({"dense": {"enabled": True, "provider": "openai"}}), OpenAIBackend)
        with self.assertRaises(ValueError):
            resolve_provider({"dense": {"enabled": True, "provider": "nope"}})

    def test_dense_detects_provider_from_env(self) -> None:
        import os
        from unittest import mock

        from cclg.dense import detect_provider

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            self.assertEqual(detect_provider(), "openai")


class IndexTests(unittest.TestCase):
    def test_build_index_persists_artifacts(self) -> None:
        from cclg.indexing import build_index

        store = fresh()
        store.write_node(MemoryNode.create(content="indexed fact about codex", source="t", tags=["codex"]))
        summary = build_index(store)
        self.assertGreaterEqual(summary["terms"], 1)
        self.assertTrue((store.root / "indexes" / "bm25" / "postings.json").exists())
        self.assertTrue((store.root / "indexes" / "graph" / "adjacency.json").exists())
        self.assertTrue((store.root / "indexes" / "temporal" / "buckets.json").exists())
        self.assertEqual(summary["dense"], "disabled")
        self.assertTrue((store.root / "indexes" / "graph" / "memory.nt").exists())

    def test_ntriples_export_emits_relations(self) -> None:
        from cclg.indexing import export_ntriples
        from cclg.models import MemoryPatch
        from cclg.patches import apply_patch

        store = fresh()
        node = MemoryNode.create(content="seed", source="t")
        store.write_node(node)
        apply_patch(store, MemoryPatch.create(operation="refine", target_ids=[node.id], reason="r", new_content="seed v2"))
        nt = export_ntriples(store)
        self.assertIn("/rel/supersedes>", nt)
        self.assertTrue(all(line.endswith(" .") for line in nt.splitlines()))


class CodeGraphGitTests(unittest.TestCase):
    def test_files_carry_git_recency_and_authors(self) -> None:
        from cclg.codegraph import build_code_graph

        graph = build_code_graph(Path(__file__).resolve().parents[1])
        sample = next(f for f in graph.files if f["path"].endswith("cli.py"))
        self.assertIn("git_last_modified", sample)
        self.assertIn("git_authors", sample)
        self.assertGreaterEqual(sample["git_authors"], 1)

    def test_co_change_edges_present(self) -> None:
        from cclg.codegraph import build_code_graph

        graph = build_code_graph(Path(__file__).resolve().parents[1])
        self.assertTrue(any(edge.kind == "co_change" for edge in graph.edges))


class DenseCacheTests(unittest.TestCase):
    def _backend(self):
        from cclg.dense import EmbeddingBackend

        class StubBackend(EmbeddingBackend):
            name = "stub"

            def __init__(self):
                super().__init__("stub-model")
                self.calls = 0
                self.embedded = 0

            def embed(self, texts):
                self.calls += 1
                self.embedded += len(texts)
                # deterministic toy vector: [len, #codex tokens]
                return [[float(len(t)), float(t.lower().count("codex"))] for t in texts]

        return StubBackend()

    def test_cache_skips_unchanged_nodes(self) -> None:
        from cclg.dense import CachedBackend

        store = fresh()
        a = MemoryNode.create(content="codex adapter memory", source="t")
        b = MemoryNode.create(content="local first memory", source="t")
        store.write_node(a)
        store.write_node(b)
        backend = self._backend()
        cache_path = store.root / "indexes" / "dense" / "cache.json"
        cached = CachedBackend(backend, cache_path)

        self.assertEqual(cached.warm([a, b]), 2)  # both embedded first time
        self.assertEqual(cached.warm([a, b]), 0)  # nothing re-embedded
        self.assertTrue(cache_path.exists())

        # changing a node's dense text invalidates only that entry
        a.content = "codex adapter memory updated"
        a.retrieval["dense_text"] = a.content
        store.write_node(a)
        self.assertEqual(cached.warm([a, b]), 1)

    def test_cached_search_ranks_and_reuses(self) -> None:
        from cclg.dense import CachedBackend

        store = fresh()
        a = MemoryNode.create(content="codex codex codex", source="t")
        b = MemoryNode.create(content="local first", source="t")
        store.write_node(a)
        store.write_node(b)
        backend = self._backend()
        cached = CachedBackend(backend, store.root / "indexes" / "dense" / "cache.json")
        hits = cached.search("codex", [a, b], limit=2)
        self.assertEqual(hits[0].node.id, a.id)
        # docs cached from first search; second search only embeds the query
        calls_before = backend.calls
        cached.search("codex", [a, b], limit=2)
        self.assertEqual(backend.calls, calls_before + 1)


class StoreLessTests(unittest.TestCase):
    def test_effective_view_filters_and_precedence(self) -> None:
        from cclg.patches import effective_view

        a = MemoryNode.create(content="active fact", source="t")
        b = MemoryNode.create(content="old fact", source="t")
        b.status = "superseded"
        g = MemoryNode.create(content="global", source="t", scope={"agent": "global"})
        g.key = "k"
        p = MemoryNode.create(content="project", source="t", scope={"project": "r"})
        p.key = "k"
        view = effective_view([a, b, g, p])
        contents = {n.content for n in view}
        self.assertIn("active fact", contents)
        self.assertNotIn("old fact", contents)
        self.assertIn("project", contents)
        self.assertNotIn("global", contents)  # project beats global on same key

    def test_compile_pack_from_nodes_excludes_suppressed(self) -> None:
        from cclg.pack import compile_pack_from_nodes

        a = MemoryNode.create(content="codex adapter is active", source="t")
        b = MemoryNode.create(content="stale hermes-only note", source="t")
        b.status = "superseded"
        pack = compile_pack_from_nodes([a, b], "codex", max_chars=500)
        active = [n["content"] for n in pack.memory_nodes]
        self.assertEqual(active, ["codex adapter is active"])
        self.assertTrue(any(s["id"] == b.id for s in pack.suppressed_nodes))


class CitationAndTypeTests(unittest.TestCase):
    def test_security_policy_type_is_valid(self) -> None:
        node = MemoryNode.create(content="no secrets in logs", source="policy", node_type="security_policy")
        self.assertEqual(validate_node(node.to_dict()), [])

    def test_cite_recovers_provenance(self) -> None:
        store = fresh()
        node = MemoryNode.create(content="cited fact", source="chatgpt-share:abc", quote="the exact words")
        store.write_node(node)
        citation = build_citation(store, node)
        self.assertEqual(citation["memory_id"], node.id)
        self.assertEqual(citation["quote"], "the exact words")


class MCPToolTests(unittest.TestCase):
    def test_required_tools_present_and_callable(self) -> None:
        store = fresh()
        listed = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, store)
        names = {tool["name"] for tool in listed["result"]["tools"]}
        for required in ["memory.recall", "memory.cite", "memory.conflicts", "memory.resolve"]:
            self.assertIn(required, names)
        added = handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cclg.add", "arguments": {"content": "memory fact", "source": "t"}}},
            store,
        )
        node_id = added["result"]["structuredContent"]["id"]
        cite = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory.cite", "arguments": {"memory_id": node_id}}}, store)
        self.assertFalse(cite["result"]["isError"])
        self.assertEqual(cite["result"]["structuredContent"]["memory_id"], node_id)

    def test_pack_budget_includes_suppressed(self) -> None:
        store = fresh()
        node = MemoryNode.create(content="x" * 400, source="t")
        store.write_node(node)
        apply_patch(store, MemoryPatch.create(operation="supersede", target_ids=[node.id], reason="r", new_content="y" * 400))
        pack = compile_pack(store, "x", max_chars=300)
        self.assertLessEqual(pack.budget["used_chars"], 300 + 400)  # one entry may exceed, never unbounded


if __name__ == "__main__":
    unittest.main()
