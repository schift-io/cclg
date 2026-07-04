from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from cclg.cli import main as cli_main
from cclg.container import ContainerError, load_container, pack_container, pack_for_export, pack_from_store
from cclg.format import (
    CCLG_CONTAINER_ID,
    CCLG_CONTAINER_MAGIC,
    CCLG_CONTAINER_VERSION,
    CCLG_FORMAT_ID,
    MEMORY_EDGE_SCHEMA,
    MEMORY_NODE_SCHEMA,
    MEMORY_PATCH_SCHEMA,
    SESSION_SCHEMA,
)
from cclg.models import MemoryNode, MemoryPatch
from cclg.patches import RETIRING_PATCH_OPERATIONS, apply_patch, effective_view
from cclg.session import start_session, write_overlay_node
from cclg.store import CCLGStore


def _populated_store(root: Path) -> CCLGStore:
    """A store with one active node, one superseded node, one patch, one edge,
    and one session — enough surface to exercise every container section."""
    store = CCLGStore(root)
    store.init()
    original = MemoryNode.create(
        content="ACMC should start as a Hermes-only extension.",
        source="test:session-1",
        node_type="project_decision",
    )
    store.write_node(original)
    patch = MemoryPatch.create(
        operation="supersede",
        target_ids=[original.id],
        reason="User expanded agent support.",
        new_content="ACMC must support Claude Code, Codex, and Hermes.",
        source="test:session-2",
    )
    apply_patch(store, patch)
    start_session(store, session_id="session_container_test", agent="codex", workspace="local", project="cclg")
    return store


class ContainerRoundTripTests(unittest.TestCase):
    def test_round_trip_is_lossless_and_effective_view_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _populated_store(Path(tmp))

            original_nodes = list(store.iter_nodes())
            original_patches = list(store.iter_patches())
            original_edges = list(store.iter_edges())

            text = pack_from_store(store)
            bundle = load_container(text)

            self.assertEqual(bundle.counts(), {"nodes": 2, "patches": 1, "edges": 1, "sessions": 1})
            self.assertEqual(len(bundle.nodes), len(original_nodes))
            self.assertEqual(len(bundle.patches), len(original_patches))
            self.assertEqual(len(bundle.edges), len(original_edges))

            # Every record round-trips byte-for-byte identical (as dicts) via the
            # existing to_dict()/from_dict() contract — no lossy re-encoding.
            self.assertEqual(
                {node.id: node.to_dict() for node in original_nodes},
                {node["id"]: node for node in bundle.nodes},
            )
            self.assertEqual(
                {patch.id: patch.to_dict() for patch in original_patches},
                {patch["id"]: patch for patch in bundle.patches},
            )
            self.assertEqual(
                {edge.id: edge.to_dict() for edge in original_edges},
                {edge["id"]: edge for edge in bundle.edges},
            )

            original_view_ids = {node.id for node in effective_view(original_nodes)}
            loaded_view_ids = {node.id for node in effective_view(bundle.memory_nodes())}
            self.assertEqual(original_view_ids, loaded_view_ids)

    def test_pack_container_accepts_dataclasses_or_plain_dicts(self) -> None:
        node = MemoryNode.create(content="dict or dataclass, either way.", source="test:mixed")
        as_dataclass = pack_container([node])
        as_dict = pack_container([node.to_dict()])
        self.assertEqual(load_container(as_dataclass).nodes, load_container(as_dict).nodes)

    def test_record_line_preserves_to_dict_field_order_not_alphabetical(self) -> None:
        """docs/CCLG_CONTAINER.md §2.3: records reuse to_dict()'s own field
        order (== format/cclg.format.v0.1.toml's canonical_order table) — the
        container must not re-sort record keys alphabetically."""
        node = MemoryNode.create(content="order matters", source="test:order")
        text = pack_container([node])

        lines = [line for line in text.split("\n") if line]
        record_line = lines[lines.index("@nodes") + 1]
        # object_pairs_hook=identity preserves on-disk key order (a plain
        # json.loads() into a dict would silently lose any ordering bug).
        ordered_pairs = json.JSONDecoder(object_pairs_hook=lambda pairs: pairs).decode(record_line)
        keys_in_container = [key for key, _value in ordered_pairs]

        self.assertEqual(keys_in_container, list(node.to_dict().keys()))
        self.assertNotEqual(keys_in_container, sorted(keys_in_container))


class ContainerIntegrityTests(unittest.TestCase):
    @staticmethod
    def _good_text() -> str:
        node = MemoryNode.create(content="hello integrity check", source="test:integrity")
        return pack_container([node])

    def test_rejects_checksum_tamper(self) -> None:
        text = self._good_text()
        self.assertIn("hello integrity check", text)
        tampered = text.replace("hello integrity check", "hellx integrity check", 1)

        with self.assertRaises(ContainerError) as ctx:
            load_container(tampered)
        self.assertIn("checksum", str(ctx.exception))

    def test_rejects_bad_magic(self) -> None:
        text = self._good_text()
        tampered = text.replace(f"{CCLG_CONTAINER_MAGIC}\t{CCLG_CONTAINER_VERSION}", f"NOPE\t{CCLG_CONTAINER_VERSION}", 1)

        with self.assertRaises(ContainerError) as ctx:
            load_container(tampered)
        self.assertIn("magic", str(ctx.exception))

    def test_rejects_unsupported_container_version(self) -> None:
        text = self._good_text()
        tampered = text.replace(f"{CCLG_CONTAINER_MAGIC}\t{CCLG_CONTAINER_VERSION}", f"{CCLG_CONTAINER_MAGIC}\t9.9", 1)

        with self.assertRaises(ContainerError) as ctx:
            load_container(tampered)
        self.assertIn("version", str(ctx.exception))

    def test_rejects_count_mismatch(self) -> None:
        text = self._good_text()
        lines = text.split("\n")
        header = json.loads(lines[1])
        header["counts"]["nodes"] += 1
        lines[1] = json.dumps(header, ensure_ascii=False)

        with self.assertRaises(ContainerError) as ctx:
            load_container("\n".join(lines))
        self.assertIn("count mismatch", str(ctx.exception))

    def test_rejects_missing_counts_key_for_section_present_in_body(self) -> None:
        """docs/CCLG_CONTAINER.md §5: a section's marker is always written
        (even empty), so header.counts/header.sections must always carry an
        entry for it. A dropped key must not be treated as "nothing to
        compare" — that would let a hand-edited header silently bypass the
        one integrity check the (unchecksummed) header has."""
        text = self._good_text()
        lines = text.split("\n")
        header = json.loads(lines[1])
        del header["counts"]["sessions"]
        lines[1] = json.dumps(header, ensure_ascii=False)

        with self.assertRaises(ContainerError) as ctx:
            load_container("\n".join(lines))
        self.assertIn("count mismatch", str(ctx.exception))
        self.assertIn("sessions", str(ctx.exception))

    def test_rejects_missing_sections_entry_for_section_present_in_body(self) -> None:
        text = self._good_text()
        lines = text.split("\n")
        header = json.loads(lines[1])
        header["sections"] = [entry for entry in header["sections"] if entry["name"] != "edges"]
        lines[1] = json.dumps(header, ensure_ascii=False)

        with self.assertRaises(ContainerError) as ctx:
            load_container("\n".join(lines))
        self.assertIn("count mismatch", str(ctx.exception))
        self.assertIn("edges", str(ctx.exception))


class ContainerForwardCompatTests(unittest.TestCase):
    def test_unknown_section_is_skipped_with_warning_and_known_sections_still_load(self) -> None:
        node = MemoryNode.create(content="known section content", source="test:forward-compat")
        text = pack_container([node])

        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        header = json.loads(lines[1])
        body_lines = lines[2:]
        body_lines = [*body_lines, "@future_experimental_section", json.dumps({"note": "added by a newer producer"}, sort_keys=True)]
        header["content_sha256"] = hashlib.sha256("\n".join(body_lines).encode("utf-8")).hexdigest()
        forward_text = "\n".join([lines[0], json.dumps(header, ensure_ascii=False), *body_lines]) + "\n"

        bundle = load_container(forward_text)

        self.assertEqual(len(bundle.nodes), 1)
        self.assertEqual(bundle.nodes[0]["content"], "known section content")
        self.assertIn("future_experimental_section", bundle.unknown_sections)
        self.assertEqual(len(bundle.unknown_sections["future_experimental_section"]), 1)
        self.assertTrue(any("future_experimental_section" in warning for warning in bundle.warnings))

    def test_format_id_mismatch_warns_but_still_loads(self) -> None:
        """docs/CCLG_CONTAINER.md §4: header.format_id is an informational
        cross-check — a mismatch is a warning, not a hard failure, since
        per-record schema_version is the authoritative check."""
        node = MemoryNode.create(content="format id cross-check", source="test:format-id")
        text = pack_container([node])

        lines = text.split("\n")
        header = json.loads(lines[1])
        self.assertNotEqual(header["format_id"], "cclg.format.v9.9")
        header["format_id"] = "cclg.format.v9.9"
        lines[1] = json.dumps(header, ensure_ascii=False)
        tampered = "\n".join(lines)

        bundle = load_container(tampered)

        self.assertEqual(len(bundle.nodes), 1)
        self.assertEqual(bundle.nodes[0]["content"], "format id cross-check")
        self.assertTrue(any("format_id" in warning for warning in bundle.warnings))


class ContainerLedgerOnlyTests(unittest.TestCase):
    def test_superseded_node_preserved_raw_and_effective_view_is_never_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _populated_store(Path(tmp))
            superseded = next(node for node in store.iter_nodes() if node.status == "superseded")

            text = pack_from_store(store)

            # Only the four ledger sections ever appear — no precomputed
            # effective-view / active-memory-pack section.
            markers = [line for line in text.split("\n") if line.startswith("@")]
            self.assertEqual(markers, ["@nodes", "@patches", "@edges", "@sessions"])
            self.assertNotIn("active_memory_pack", text)
            self.assertNotIn("effective_view", text)

            bundle = load_container(text)
            loaded_superseded = next(node for node in bundle.nodes if node["id"] == superseded.id)
            self.assertEqual(loaded_superseded["status"], "superseded")
            self.assertEqual(loaded_superseded["content"], superseded.content)

            # Present verbatim in the ledger, but computed-out of the effective view.
            self.assertIn(superseded.id, {node["id"] for node in bundle.nodes})
            self.assertNotIn(superseded.id, {node.id for node in effective_view(bundle.memory_nodes())})


class ContainerLoadSemanticsEffectiveViewTests(unittest.TestCase):
    """docs/CCLG_CONTAINER.md §3.1.1: a conforming reader MUST exclude nodes
    targeted by a retiring patch from the effective view regardless of the
    node's *baked* status. These reproduce the P3-reported cross-impl gap: a
    schema-valid container whose `@nodes` section never had a patch's effect
    baked into `status` (still `active`) MUST still retire that node once
    loaded, via `ContainerBundle.effective_view()`."""

    @staticmethod
    def _unbaked_supersede_container() -> tuple[str, str, str]:
        """Build a schema-valid `.cclg` text with a supersede patch whose target
        node's status was never flipped away from `active` -- i.e. it was
        never run through `apply_patch` before packing. Returns
        (text, old_node_id, new_node_id)."""
        old = MemoryNode.create(content="ACMC is Hermes-only.", source="test:load-semantics")
        new = MemoryNode.create(content="ACMC supports Claude Code, Codex, and Hermes.", source="test:load-semantics")
        patch = MemoryPatch.create(
            operation="supersede",
            target_ids=[old.id],
            reason="user expanded agent support",
            new_content=new.content,
        )
        text = pack_container([old, new], [patch])
        return text, old.id, new.id

    def test_status_only_view_wrongly_keeps_unbaked_target(self) -> None:
        """Establishes the bug being fixed: the pre-existing `patches=None`
        status-only projection alone cannot see this inconsistency."""
        text, old_id, new_id = self._unbaked_supersede_container()
        bundle = load_container(text)

        loaded_old = next(record for record in bundle.nodes if record["id"] == old_id)
        self.assertEqual(loaded_old["status"], "active")

        status_only_ids = {node.id for node in effective_view(bundle.memory_nodes())}
        self.assertEqual(status_only_ids, {old_id, new_id})

    def test_bundle_effective_view_excludes_unbaked_retired_target(self) -> None:
        """The fix: `ContainerBundle.effective_view()` wires the bundle's own
        `@patches` through, so the stale node is excluded even though its
        `status` field still reads `active`."""
        text, old_id, new_id = self._unbaked_supersede_container()
        bundle = load_container(text)

        canonical_ids = {node.id for node in bundle.effective_view()}
        self.assertEqual(canonical_ids, {new_id})

    def test_bundle_effective_view_excludes_unbaked_forget_target(self) -> None:
        old = MemoryNode.create(content="a secret note", source="test:load-semantics")
        patch = MemoryPatch.create(operation="forget", target_ids=[old.id], reason="user asked to forget")
        text = pack_container([old], [patch])
        bundle = load_container(text)

        self.assertEqual({node["status"] for node in bundle.nodes}, {"active"})
        self.assertEqual(bundle.effective_view(), [])

    def test_effective_view_patches_none_default_is_unchanged(self) -> None:
        """Regression: existing call sites (agent-hub's cclg_grounding.py /
        pack.py, active_nodes() in patches.py) never pass patches=, and must
        see byte-for-byte the same result as before this change."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _populated_store(Path(tmp))
            bundle = load_container(pack_from_store(store))

            baseline_ids = [node.id for node in effective_view(bundle.memory_nodes())]
            explicit_none_ids = [node.id for node in effective_view(bundle.memory_nodes(), patches=None)]
            self.assertEqual(baseline_ids, explicit_none_ids)
            # Same store this class already trusts (ContainerRoundTripTests):
            # one active node survives, the superseded one does not.
            self.assertEqual(len(baseline_ids), 1)

    def test_retiring_patch_operations_excludes_create_and_rollback(self) -> None:
        self.assertNotIn("create", RETIRING_PATCH_OPERATIONS)
        self.assertNotIn("rollback", RETIRING_PATCH_OPERATIONS)
        self.assertEqual(
            RETIRING_PATCH_OPERATIONS,
            {
                "update",
                "supersede",
                "refine",
                "expand",
                "narrow",
                "merge",
                "split",
                "resolve_conflict",
                "expire",
                "forget",
                "deprecate",
            },
        )


class ContainerAuthFreeGuardTests(unittest.TestCase):
    def test_pack_container_rejects_forbidden_metadata_field(self) -> None:
        node = MemoryNode.create(content="should never carry auth", source="test:auth").to_dict()
        node["metadata"]["org_id"] = "org_should_not_be_here"

        with self.assertRaises(ContainerError) as ctx:
            pack_container([node])
        self.assertIn("org_id", str(ctx.exception))

    def test_load_container_rejects_forbidden_field_even_if_checksum_is_valid(self) -> None:
        node = MemoryNode.create(content="should never carry auth", source="test:auth").to_dict()
        node["metadata"]["api_key"] = "sk-should-not-be-here"
        body_lines = ["@nodes", json.dumps(node, sort_keys=True, ensure_ascii=False), "@patches", "@edges", "@sessions"]
        content_sha256 = hashlib.sha256("\n".join(body_lines).encode("utf-8")).hexdigest()
        header = {
            "container": CCLG_CONTAINER_ID,
            "format_id": CCLG_FORMAT_ID,
            "versions": {
                "memory_node": MEMORY_NODE_SCHEMA,
                "memory_patch": MEMORY_PATCH_SCHEMA,
                "edge": MEMORY_EDGE_SCHEMA,
                "session": SESSION_SCHEMA,
            },
            "sections": [{"name": "nodes", "count": 1}, {"name": "patches", "count": 0}, {"name": "edges", "count": 0}, {"name": "sessions", "count": 0}],
            "counts": {"nodes": 1, "patches": 0, "edges": 0, "sessions": 0},
            "generated_at": "2026-07-04T00:00:00+00:00",
            "content_sha256": content_sha256,
        }
        text = "\n".join([f"{CCLG_CONTAINER_MAGIC}\t{CCLG_CONTAINER_VERSION}", json.dumps(header, ensure_ascii=False), *body_lines]) + "\n"

        with self.assertRaises(ContainerError) as ctx:
            load_container(text)
        self.assertIn("api_key", str(ctx.exception))


def _session_scoped_store(root: Path) -> tuple[CCLGStore, dict[str, MemoryNode]]:
    """A store with one session-independent node plus two sessions, each with
    its own overlay node — enough surface to tell `--session`/`--node`
    filtering apart from a whole-ledger export."""
    store = CCLGStore(root)
    store.init()
    global_node = MemoryNode.create(content="Global fact not tied to any session.", source="test:global")
    store.write_node(global_node)

    start_session(store, session_id="session_a", agent="codex", workspace="local", project="cclg")
    node_a = write_overlay_node(store, session_id="session_a", content="Session A scratch note.", source="test:session-a")

    start_session(store, session_id="session_b", agent="codex", workspace="local", project="cclg")
    node_b = write_overlay_node(store, session_id="session_b", content="Session B scratch note.", source="test:session-b")

    return store, {"global": global_node, "node_a": node_a, "node_b": node_b}


class PackForExportTests(unittest.TestCase):
    def test_no_filters_matches_pack_from_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _populated_store(Path(tmp))
            self.assertEqual(load_container(pack_for_export(store)).counts(), load_container(pack_from_store(store)).counts())

    def test_session_filter_selects_only_that_sessions_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, nodes = _session_scoped_store(Path(tmp))

            bundle = load_container(pack_for_export(store, session_ids=["session_a"]))

            self.assertEqual({node["id"] for node in bundle.nodes}, {nodes["node_a"].id})
            self.assertEqual([session["id"] for session in bundle.sessions], ["session_a"])

    def test_node_filter_selects_exactly_that_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, nodes = _session_scoped_store(Path(tmp))

            bundle = load_container(pack_for_export(store, node_ids=[nodes["global"].id]))

            self.assertEqual({node["id"] for node in bundle.nodes}, {nodes["global"].id})
            self.assertEqual(bundle.sessions, [])

    def test_session_and_node_filters_union(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, nodes = _session_scoped_store(Path(tmp))

            bundle = load_container(pack_for_export(store, session_ids=["session_a"], node_ids=[nodes["global"].id]))

            self.assertEqual({node["id"] for node in bundle.nodes}, {nodes["global"].id, nodes["node_a"].id})

    def test_unknown_session_id_selects_nothing_but_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _nodes = _session_scoped_store(Path(tmp))

            bundle = load_container(pack_for_export(store, session_ids=["session_does_not_exist"]))

            self.assertEqual(bundle.counts(), {"nodes": 0, "patches": 0, "edges": 0, "sessions": 0})

    def test_rejects_forbidden_metadata_field_same_as_pack_container(self) -> None:
        """The export path must not open a bypass around the auth-free guard:
        it reuses `pack_container` directly, so a tampered on-disk node still
        raises here exactly as it does for `pack_container`/`pack_from_store`."""
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            node = MemoryNode.create(content="should never carry auth", source="test:auth")
            tampered = node.to_dict()
            tampered["metadata"]["org_id"] = "org_should_not_be_here"
            store._write_json(store.nodes_dir / f"{node.id}.json", tampered)

            with self.assertRaises(ContainerError) as ctx:
                pack_for_export(store)
            self.assertIn("org_id", str(ctx.exception))


class ContainerCliTests(unittest.TestCase):
    def test_pack_file_and_open_round_trip_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            _populated_store(root)
            out_path = Path(tmp) / "export.cclg"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli_main(["--root", str(root), "pack-file", str(out_path)])
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertTrue(out_path.exists())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli_main(["open", str(out_path), "--json"])
            self.assertEqual(code, 0, stdout.getvalue())

            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["header"]["container"], CCLG_CONTAINER_ID)
            self.assertEqual(payload["counts"], {"nodes": 2, "patches": 1, "edges": 1, "sessions": 1})
            self.assertEqual(payload["unknown_sections"], {})


class ExportSchiftCliTests(unittest.TestCase):
    def test_export_schift_no_filters_round_trips_full_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            _populated_store(root)
            out_path = Path(tmp) / "export.cclg"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli_main(["--root", str(root), "export", "schift", "--out", str(out_path)])
            self.assertEqual(code, 0, stdout.getvalue())
            self.assertTrue(out_path.exists())

            bundle = load_container(out_path.read_text(encoding="utf-8"))
            self.assertEqual(bundle.header["container"], CCLG_CONTAINER_ID)
            self.assertEqual(bundle.counts(), {"nodes": 2, "patches": 1, "edges": 1, "sessions": 1})

    def test_export_schift_session_filter_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            _store, nodes = _session_scoped_store(root)
            out_path = Path(tmp) / "session_a.cclg"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli_main(["--root", str(root), "export", "schift", "--out", str(out_path), "--session", "session_a"])
            self.assertEqual(code, 0, stdout.getvalue())

            bundle = load_container(out_path.read_text(encoding="utf-8"))
            self.assertEqual({node["id"] for node in bundle.nodes}, {nodes["node_a"].id})
            self.assertEqual([session["id"] for session in bundle.sessions], ["session_a"])

    def test_export_schift_node_filter_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            _store, nodes = _session_scoped_store(root)
            out_path = Path(tmp) / "node.cclg"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli_main(["--root", str(root), "export", "schift", "--out", str(out_path), "--node", nodes["global"].id])
            self.assertEqual(code, 0, stdout.getvalue())

            bundle = load_container(out_path.read_text(encoding="utf-8"))
            self.assertEqual({node["id"] for node in bundle.nodes}, {nodes["global"].id})
            self.assertEqual(bundle.sessions, [])


if __name__ == "__main__":
    unittest.main()
