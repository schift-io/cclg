from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from cclg.bench import run_benchmarks
from cclg.cli import audit_report, doctor_report, main as cli_main, validate_paths
from cclg.codegraph import build_code_graph, search_code_graph
from cclg.format import CODE_GRAPH_SCHEMA, render_active_pack_toml
from cclg.hooks import user_prompt_context
from cclg.mcp_server import handle_message, iter_messages
from cclg.models import MemoryNode
from cclg.pack import compile_pack
from cclg.store import CCLGStore


class CCLGSurfaceTests(unittest.TestCase):
    def test_code_graph_indexes_symbols(self) -> None:
        graph = build_code_graph(Path(__file__).resolve().parents[1])
        self.assertEqual(graph.schema_version, CODE_GRAPH_SCHEMA)
        hits = search_code_graph(graph, "user_prompt_context MemoryNode", limit=20)
        names = {hit["item"].get("name") for hit in hits if hit["kind"] == "symbol"}

        self.assertIn("user_prompt_context", names)
        self.assertIn("MemoryNode", names)

    def test_hook_returns_additional_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.write_node(MemoryNode.create(content="Hooks inject CCLG memory.", source="test:hook"))

            output = user_prompt_context(
                store,
                {"prompt": "hook CCLG", "session_id": "test"},
                code_root=Path(tmp),
                max_chars=2000,
                include_codegraph=False,
            )

            # Hosts (Claude Code / Codex plugin_hooks) reject unknown top-level
            # keys in hook stdout — the payload must stay schema-only.
            self.assertEqual(set(output.keys()), {"hookSpecificOutput"})
            self.assertIn("additionalContext", output["hookSpecificOutput"])
            self.assertIn("Hooks inject CCLG memory", output["hookSpecificOutput"]["additionalContext"])

    def test_pack_renders_compact_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.write_node(MemoryNode.create(content="Compact TOML keeps prompt context short.", source="test:toml"))
            text = render_active_pack_toml(compile_pack(store, "compact").to_dict())

            self.assertIn('schema_version = "cclg.active_memory_pack.v0.1"', text)
            self.assertIn("[[memory]]", text)
            self.assertIn('source = "test:toml"', text)

    def test_mcp_lists_and_calls_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, store)
            tool_names = [tool["name"] for tool in response["result"]["tools"]]

            self.assertIn("cclg.pack", tool_names)
            self.assertIn("cclg.code_search", tool_names)
            self.assertIn("memory.grep", tool_names)

            grep = handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "memory.audit", "arguments": {}},
                },
                store,
            )

            self.assertFalse(grep["result"]["isError"])
            self.assertTrue(grep["result"]["structuredContent"]["ok"])

    def test_benchmark_runner_passes_core_suites(self) -> None:
        report = run_benchmarks(suite="all", repo_root=str(Path(__file__).resolve().parents[1]))

        self.assertTrue(report["ok"], report)

    def test_validate_reads_seed_jsonl_records(self) -> None:
        seed = Path(__file__).resolve().parents[1] / "examples" / "acmc_seed.jsonl"
        report = validate_paths([seed])

        self.assertTrue(report["ok"], report)
        self.assertGreater(report["checked"], 0)

    def test_validate_accepts_whole_demo_store(self) -> None:
        demo_store = Path(__file__).resolve().parents[1] / "docs" / "explainer" / "demo-store"
        report = validate_paths([demo_store])

        self.assertTrue(report["ok"], report)
        self.assertGreaterEqual(report["checked"], 8)

    def test_validate_reports_malformed_json_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text("{not json}\n", encoding="utf-8")

            report = validate_paths([path])

            self.assertFalse(report["ok"])
            self.assertEqual(report["checked"], 1)
            self.assertIn("invalid JSON", report["problems"][0])

    def test_cli_expected_errors_do_not_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = cli_main(["--root", tmp, "add", "--content", "", "--source", "test"])

            self.assertEqual(code, 1)
            self.assertIn("error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_mcp_malformed_json_returns_parse_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            messages = list(iter_messages(io.BytesIO(b"{bad json}\n"), line_delimited=True))
            response = handle_message(messages[0], store)

            self.assertEqual(response["error"]["code"], -32700)
            self.assertIn("invalid JSON", response["error"]["message"])

    def test_doctor_and_audit_report_dangling_relations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            node = MemoryNode.create(content="Dangling relation should be reported.", source="test:dangling")
            node.relations["supersedes"] = ["mem_missing"]
            store.write_node(node)

            doctor = doctor_report(store)
            audit = audit_report(store)

            self.assertFalse(doctor["ok"])
            self.assertIn("missing node mem_missing", "\n".join(doctor["problems"]))
            self.assertFalse(audit["ok"])
            self.assertIn("missing node mem_missing", "\n".join(audit["findings"]["doctor"]))

    def test_doctor_reports_malformed_session_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            (Path(tmp) / "sessions" / "bad.json").write_text("{not json}\n", encoding="utf-8")

            doctor = doctor_report(store)
            audit = audit_report(store)

            self.assertFalse(doctor["ok"])
            self.assertIn("invalid JSON", "\n".join(doctor["problems"]))
            self.assertFalse(audit["ok"])
            self.assertIn("invalid JSON", "\n".join(audit["findings"]["doctor"]))


if __name__ == "__main__":
    unittest.main()
