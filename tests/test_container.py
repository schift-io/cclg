from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from cclg.cli import main as cli_main
from cclg.container import ContainerError, load_container, pack_container, pack_from_store
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
from cclg.patches import apply_patch, effective_view
from cclg.session import start_session
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


if __name__ == "__main__":
    unittest.main()
