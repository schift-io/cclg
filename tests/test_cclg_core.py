from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cclg.format import ACTIVE_MEMORY_PACK_SCHEMA, MEMORY_NODE_SCHEMA, MEMORY_PATCH_SCHEMA
from cclg.models import MemoryNode, MemoryPatch
from cclg.pack import compile_pack
from cclg.patches import active_nodes, apply_patch, suppressed_nodes
from cclg.retrieval import search_nodes
from cclg.session import start_session, write_overlay_node
from cclg.store import CCLGStore


class CCLGCoreTests(unittest.TestCase):
    def test_patch_refine_suppresses_old_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            original = MemoryNode.create(
                content="ACMC should start as a Hermes-only extension.",
                source="test:session-1",
                node_type="project_decision",
            )
            self.assertEqual(original.schema_version, MEMORY_NODE_SCHEMA)
            store.write_node(original)

            patch = MemoryPatch.create(
                operation="refine",
                target_ids=[original.id],
                reason="User expanded agent support.",
                new_content="ACMC must support Claude Code, Codex, Hermes, and server-side ReACT agents.",
                source="test:session-2",
            )
            self.assertEqual(patch.schema_version, MEMORY_PATCH_SCHEMA)
            apply_patch(store, patch)

            active = active_nodes(store)
            suppressed = suppressed_nodes(store)

            self.assertEqual(len(active), 1)
            self.assertIn("Claude Code", active[0].content)
            self.assertEqual(suppressed[0].id, original.id)
            self.assertEqual(suppressed[0].status, "superseded")
            edge = next(iter(store.iter_edges()))
            self.assertEqual(edge.from_id, active[0].id)
            self.assertEqual(edge.to_id, original.id)
            self.assertEqual(edge.type, "refines")
            self.assertEqual(edge.source_patch_id, patch.id)

    def test_search_prefers_active_sparse_match(self) -> None:
        local = MemoryNode.create(
            content="CCLG is local-first and keeps raw transcript on the user's machine.",
            source="test:local",
            tags=["local-first"],
        )
        other = MemoryNode.create(
            content="Dense embeddings are optional ablations.",
            source="test:bench",
            tags=["benchmark"],
        )

        hits = search_nodes("local-first transcript", [other, local])

        self.assertEqual(hits[0].node.id, local.id)
        self.assertGreater(hits[0].score, 0)

    def test_pack_excludes_suppressed_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            node = MemoryNode.create(content="Old Hermes-only memory.", source="test:old")
            store.write_node(node)
            apply_patch(
                store,
                MemoryPatch.create(
                    operation="supersede",
                    target_ids=[node.id],
                    reason="Cross-agent requirement.",
                    new_content="CCLG supports Codex and Claude Code too.",
                ),
            )

            pack = compile_pack(store, "Codex memory")
            self.assertEqual(pack.schema_version, ACTIVE_MEMORY_PACK_SCHEMA)

            active_contents = [item["content"] for item in pack.memory_nodes]
            self.assertEqual(active_contents, ["CCLG supports Codex and Claude Code too."])
            self.assertEqual(pack.suppressed_nodes[0]["status"], "superseded")

    def test_session_overlay_is_session_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            session = start_session(store, session_id="sess_one", project="CCLG")
            write_overlay_node(store, session_id=session["id"], content="Only this session can see overlay memory.")

            global_pack = compile_pack(store, "overlay")
            session_pack = compile_pack(store, "overlay", session_id="sess_one")
            other_session_pack = compile_pack(store, "overlay", session_id="sess_two")

            self.assertEqual(global_pack.memory_nodes, [])
            self.assertEqual([node["content"] for node in session_pack.memory_nodes], ["Only this session can see overlay memory."])
            self.assertEqual(other_session_pack.memory_nodes, [])

    def test_init_does_not_rewrite_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            config_path = Path(tmp) / "config.json"
            first = config_path.read_text(encoding="utf-8")

            store.init()

            self.assertEqual(config_path.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
