from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bench import run_benchmarks
from .codegraph import build_code_graph, load_code_graph, render_code_pack, save_code_graph, search_code_graph
from .format import CODE_GRAPH_SCHEMA, MEMORY_NODE_SCHEMA, MEMORY_PATCH_SCHEMA, STORE_SCHEMA, render_active_pack_toml, source_label
from .models import MemoryNode, MemoryPatch, now_iso
from .pack import compile_pack
from .patches import active_nodes, apply_patch, conflict_nodes, detect_patch_candidates, suppressed_nodes
from .retrieval import search_memory, search_nodes
from .schema import validate_edge, validate_node, validate_patch, validate_session
from .session import end_session, fork_session, load_session, merge_session, promote_session_node, start_session, write_overlay_node
from .store import CCLGStore, atomic_write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cclg", description="Canonical Chat Ledger Graph CLI")
    parser.add_argument("--root", help="CCLG store root. Defaults to $CCLG_HOME or ~/.cclg")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize local CCLG store")

    add = sub.add_parser("add", help="Add a source-grounded memory node")
    add.add_argument("--type", default="memory", dest="node_type")
    add.add_argument("--content", required=True)
    add.add_argument("--source", required=True)
    add.add_argument("--quote")
    add.add_argument("--key")
    add.add_argument("--confidence", type=float, default=1.0)
    add.add_argument("--priority", default="normal")
    add.add_argument("--scope", action="append", default=[], help="Scope as key=value. Repeatable.")
    add.add_argument("--tag", action="append", default=[])

    import_jsonl = sub.add_parser("import-jsonl", help="Import memory nodes from JSONL")
    import_jsonl.add_argument("path")

    ingest = sub.add_parser("ingest", help="Ingest raw transcript/evidence or JSONL nodes")
    ingest.add_argument("path", help="File path or '-' for stdin raw text")
    ingest.add_argument("--name", help="Raw evidence name when storing text")
    ingest.add_argument("--jsonl", action="store_true", help="Treat input as memory-node JSONL")

    validate = sub.add_parser("validate", help="Validate CCLG record files against cclg.format.v0.1")
    validate.add_argument("path", nargs="+")
    validate.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="Search active memory")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--mode", choices=["auto", "grep", "bm25", "dense", "graph", "temporal"], default="bm25")
    search.add_argument("--json", action="store_true")

    grep = sub.add_parser("grep", help="Exact search active memory and raw evidence")
    grep.add_argument("query")
    grep.add_argument("--limit", type=int, default=20)
    grep.add_argument("--json", action="store_true")

    bm25 = sub.add_parser("bm25", help="Lexical ranked active-memory search")
    bm25.add_argument("query")
    bm25.add_argument("--limit", type=int, default=10)
    bm25.add_argument("--json", action="store_true")

    pack = sub.add_parser("pack", help="Compile an ActiveMemoryPack")
    pack.add_argument("--query", default="")
    pack.add_argument("--max-nodes", type=int, default=12)
    pack.add_argument("--max-chars", type=int, default=6000)
    pack.add_argument("--format", choices=["json", "markdown", "toml"], default="json")
    pack.add_argument("--session-id")

    pack_file = sub.add_parser("pack-file", help="Pack the store ledger into a portable .cclg container (docs/CCLG_CONTAINER.md)")
    pack_file.add_argument("out", help="Output .cclg path")
    pack_file.add_argument("--session", action="append", default=[], dest="sessions", help="Limit @sessions to this session id. Repeatable. Omit to include all sessions.")
    pack_file.add_argument("--store", help="Store root to pack from (defaults to --root / $CCLG_HOME)")

    export = sub.add_parser("export", help="Export a portable .cclg payload for a producer target")
    export_sub = export.add_subparsers(dest="export_target", required=True)
    export_schift = export_sub.add_parser("schift", help="Export a .cclg payload for Schift ingestion (no Schift auth fields)")
    export_schift.add_argument("--out", required=True, help="Output .cclg path")
    export_schift.add_argument("--session", action="append", default=[], dest="sessions", help="Limit to this session's nodes (by id). Repeatable. Omit to not filter by session.")
    export_schift.add_argument("--node", action="append", default=[], dest="nodes", help="Limit to this node id. Repeatable. Omit to not filter by node.")
    export_schift.add_argument("--store", help="Store root to export from (defaults to --root / $CCLG_HOME)")

    open_cmd = sub.add_parser("open", help="Open and validate a .cclg container (read-only: validate + print header/counts)")
    open_cmd.add_argument("path", help="Path to a .cclg container file")
    open_cmd.add_argument("--json", action="store_true")

    patch = sub.add_parser("patch", help="Apply a memory patch")
    patch.add_argument("operation", choices=["create", "update", "supersede", "refine", "expand", "narrow", "merge", "split", "expire", "deprecate", "forget", "resolve_conflict", "rollback"])
    patch.add_argument("--target", action="append", default=[], dest="targets")
    patch.add_argument("--content")
    patch.add_argument("--reason", required=True)
    patch.add_argument("--source", default="manual")

    forget = sub.add_parser("forget", help="Forget one memory id or the first active query match")
    forget.add_argument("target")
    forget.add_argument("--reason", default="User requested forget.")

    status = sub.add_parser("status", help="Show store status")
    status.add_argument("--json", action="store_true")

    raw = sub.add_parser("raw", help="Append a raw source file to the local ledger")
    raw.add_argument("path")
    raw.add_argument("--name")

    session = sub.add_parser("session", help="Manage local session state and overlays")
    session_sub = session.add_subparsers(dest="session_action", required=True)
    session_start = session_sub.add_parser("start", help="Start a session")
    session_start.add_argument("--id")
    session_start.add_argument("--agent", default="codex")
    session_start.add_argument("--workspace", default="local")
    session_start.add_argument("--project", default="default")
    session_start.add_argument("--parent")
    session_start.add_argument("--branch", default="main")
    session_resume = session_sub.add_parser("resume", help="Show a session")
    session_resume.add_argument("id")
    session_end = session_sub.add_parser("end", help="End a session")
    session_end.add_argument("id")
    session_end.add_argument("--policy", choices=["keep", "promote", "discard"], default="keep", help="Overlay disposition on end")
    session_fork = session_sub.add_parser("fork", help="Fork a session into an independent overlay branch")
    session_fork.add_argument("id")
    session_fork.add_argument("--branch", default="fork")
    session_fork.add_argument("--new-id", dest="new_id")
    session_merge = session_sub.add_parser("merge", help="Promote a session's overlay into long-term memory")
    session_merge.add_argument("id")
    session_promote = session_sub.add_parser("promote", help="Promote one session overlay node to long-term memory")
    session_promote.add_argument("id")
    session_promote.add_argument("node_id")
    session_overlay = session_sub.add_parser("overlay", help="Write a session-only overlay memory")
    session_overlay.add_argument("id")
    session_overlay.add_argument("--content", required=True)
    session_overlay.add_argument("--source", default="cli:session-overlay")
    session_overlay.add_argument("--type", default="memory", dest="node_type")

    index = sub.add_parser("index", help="Build and persist embedding-independent retrieval indexes (bm25/graph/temporal)")
    index.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="Validate store invariants")
    doctor.add_argument("--json", action="store_true")

    audit = sub.add_parser("audit", help="Run memory audit report")
    audit.add_argument("--json", action="store_true")

    conflicts = sub.add_parser("conflicts", help="List unresolved conflict_pending memory")
    conflicts.add_argument("--json", action="store_true")

    detect = sub.add_parser("detect", help="Detect patch candidates from a raw user turn")
    detect.add_argument("text")
    detect.add_argument("--limit", type=int, default=3)
    detect.add_argument("--json", action="store_true")

    cite = sub.add_parser("cite", help="Show source provenance for a memory id")
    cite.add_argument("memory_id")
    cite.add_argument("--json", action="store_true")

    log = sub.add_parser("log", help="Show patch log")
    log.add_argument("--json", action="store_true")

    diff = sub.add_parser("diff", help="Show one patch and related nodes")
    diff.add_argument("patch_id")
    diff.add_argument("--json", action="store_true")

    rollback = sub.add_parser("rollback", help="Rollback a patch by reactivating targets and discarding new nodes")
    rollback.add_argument("patch_id")
    rollback.add_argument("--reason", default="Rollback requested.")

    dense = sub.add_parser("dense", help="Manage the optional on-device dense retrieval model")
    dense_sub = dense.add_subparsers(dest="dense_action", required=True)
    dense_enable = dense_sub.add_parser("enable", help="Enable dense retrieval with a provider/model")
    dense_enable.add_argument("--provider", default="auto", choices=["auto", "local", "ollama", "openai", "schift", "google", "cloudflare"], help="auto detects from env credentials")
    dense_enable.add_argument("--model", help="Embedding model id (defaults to the provider's default)")
    dense_enable.add_argument("--device", help="Local backend device hint, e.g. cpu/cuda/mps")
    dense_enable.add_argument("--base-url", dest="base_url", help="OpenAI-compatible base URL (openai/schift/llama.cpp/LM Studio/vLLM)")
    dense_enable.add_argument("--host", help="Ollama host, default http://localhost:11434")
    dense_enable.add_argument("--account-id", dest="account_id", help="Cloudflare account id (or CLOUDFLARE_ACCOUNT_ID)")
    dense_enable.add_argument("--download", action="store_true", help="Pre-download/verify a local model now")
    dense_sub.add_parser("disable", help="Disable dense retrieval")
    dense_sub.add_parser("status", help="Show dense retrieval configuration and detected providers")

    mcp = sub.add_parser("mcp", help="MCP helper")
    mcp.add_argument("action", choices=["serve"])

    apply_codex = sub.add_parser("apply-codex", help="Print Codex-local setup guidance")
    apply_codex.add_argument("--write-skill", action="store_true")

    code_index = sub.add_parser("code-index", help="Build and store a code graph for a repository")
    code_index.add_argument("repo", nargs="?", default=".")
    code_index.add_argument("--name")
    code_index.add_argument("--json", action="store_true")

    code_search = sub.add_parser("code-search", help="Search a repository code graph")
    code_search.add_argument("query")
    code_search.add_argument("--repo", default=".")
    code_search.add_argument("--graph")
    code_search.add_argument("--limit", type=int, default=20)
    code_search.add_argument("--format", choices=["json", "markdown"], default="markdown")

    bench = sub.add_parser("bench", help="Run CCLG benchmark suites")
    bench.add_argument("action", choices=["run"])
    bench.add_argument("--suite", default="all", choices=["all", "mutation", "detection", "retrieval", "pack", "hook", "mcp", "codegraph"])
    bench.add_argument("--repo-root")
    bench.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (FileNotFoundError, KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = CCLGStore(args.root)

    if args.command == "init":
        store.init()
        print(f"initialized {store.root}")
        return 0

    if args.command == "add":
        node = MemoryNode.create(
            content=args.content,
            source=args.source,
            node_type=args.node_type,
            quote=args.quote,
            scope=parse_scope(args.scope),
            tags=args.tag,
        )
        node.key = args.key
        node.confidence = args.confidence
        node.priority = args.priority
        store.write_node(node)
        print(json.dumps(node.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "import-jsonl":
        count = import_jsonl(store, Path(args.path))
        print(f"imported {count} nodes into {store.root}")
        return 0

    if args.command == "ingest":
        if args.path == "-":
            text = sys.stdin.read()
            stored = store.append_raw(args.name or "stdin.txt", text)
            print(stored)
            return 0
        path = Path(args.path).expanduser()
        if args.jsonl or path.suffix == ".jsonl":
            count = import_jsonl(store, path)
            print(f"imported {count} nodes into {store.root}")
            return 0
        stored = store.append_raw(args.name or path.name, path.read_text(encoding="utf-8"))
        print(stored)
        return 0

    if args.command == "validate":
        report = validate_paths([Path(path).expanduser() for path in args.path])
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"ok: {report['ok']}")
            for problem in report["problems"]:
                print(f"- {problem}")
        return 0 if report["ok"] else 1

    if args.command == "search":
        hits = search_memory(args.query, active_nodes(store), mode=args.mode, limit=args.limit, dense=load_dense_provider(store))
        print_search_hits(hits, json_output=args.json)
        return 0

    if args.command == "bm25":
        hits = search_nodes(args.query, active_nodes(store), limit=args.limit)
        print_search_hits(hits, json_output=args.json)
        return 0

    if args.command == "grep":
        results = grep_store(store, args.query, limit=args.limit)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for result in results:
                print(f"{result['kind']}\t{result['ref']}\t{result['text']}")
        return 0

    if args.command == "pack":
        pack = compile_pack(store, args.query, max_nodes=args.max_nodes, max_chars=args.max_chars, session_id=args.session_id)
        if args.format == "json":
            print(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2))
        elif args.format == "toml":
            print(render_active_pack_toml(pack.to_dict()))
        else:
            print(pack_markdown(pack.to_dict()))
        return 0

    if args.command == "pack-file":
        from .container import pack_from_store

        pack_store = CCLGStore(args.store) if args.store else store
        text = pack_from_store(pack_store, session_ids=args.sessions or None)
        out_path = Path(args.out).expanduser()
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path}")
        return 0

    if args.command == "export":
        if args.export_target == "schift":
            from .container import pack_for_export

            export_store = CCLGStore(args.store) if args.store else store
            text = pack_for_export(export_store, session_ids=args.sessions or None, node_ids=args.nodes or None)
            out_path = Path(args.out).expanduser()
            out_path.write_text(text, encoding="utf-8")
            print(f"wrote {out_path}")
            return 0
        raise AssertionError(args.export_target)

    if args.command == "open":
        from .container import load_container

        path = Path(args.path).expanduser()
        bundle = load_container(path.read_text(encoding="utf-8"))
        payload = {
            "header": bundle.header,
            "counts": bundle.counts(),
            "unknown_sections": {name: len(records) for name, records in bundle.unknown_sections.items()},
            "warnings": bundle.warnings,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"container: {bundle.header.get('container')}")
            print(f"format_id: {bundle.header.get('format_id')}")
            print(f"generated_at: {bundle.header.get('generated_at')}")
            for name, count in payload["counts"].items():
                print(f"{name}: {count}")
            if payload["unknown_sections"]:
                print("unknown_sections:")
                for name, count in payload["unknown_sections"].items():
                    print(f"- @{name}: {count}")
            for warning in bundle.warnings:
                print(f"warning: {warning}")
        return 0

    if args.command == "patch":
        patch = MemoryPatch.create(
            operation=args.operation,
            target_ids=args.targets,
            reason=args.reason,
            new_content=args.content,
            source=args.source,
        )
        written = apply_patch(store, patch)
        print(json.dumps({"patch": patch.to_dict(), "written_node_ids": [node.id for node in written]}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "forget":
        node_id = args.target
        if not node_id.startswith("mem_"):
            hits = search_nodes(args.target, active_nodes(store), limit=1)
            if not hits:
                print(f"no active memory matched: {args.target}", file=sys.stderr)
                return 1
            node_id = hits[0].node.id
        patch = MemoryPatch.create(operation="forget", target_ids=[node_id], reason=args.reason, source="cli:forget")
        written = apply_patch(store, patch)
        print(json.dumps({"patch": patch.to_dict(), "written_node_ids": [node.id for node in written]}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "status":
        payload = {
            "root": str(store.root),
            "active_nodes": len(active_nodes(store)),
            "suppressed_nodes": len(suppressed_nodes(store)),
            "patches": len(list(store.iter_patches())),
            "edges": len(list(store.iter_edges())),
            "sessions": len(list(store.sessions_dir.glob("*.json"))) if store.sessions_dir.exists() else 0,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"root: {payload['root']}")
            print(f"active_nodes: {payload['active_nodes']}")
            print(f"suppressed_nodes: {payload['suppressed_nodes']}")
            print(f"patches: {payload['patches']}")
            print(f"edges: {payload['edges']}")
            print(f"sessions: {payload['sessions']}")
        return 0

    if args.command == "raw":
        path = Path(args.path)
        stored = store.append_raw(args.name or path.name, path.read_text(encoding="utf-8"))
        print(stored)
        return 0

    if args.command == "session":
        if args.session_action == "start":
            session = start_session(
                store,
                session_id=args.id,
                agent=args.agent,
                workspace=args.workspace,
                project=args.project,
                parent_session_id=args.parent,
                branch_name=args.branch,
            )
            print(json.dumps(session, ensure_ascii=False, indent=2))
            return 0
        if args.session_action == "resume":
            print(json.dumps(load_session(store, args.id), ensure_ascii=False, indent=2))
            return 0
        if args.session_action == "end":
            print(json.dumps(end_session(store, args.id, policy=args.policy), ensure_ascii=False, indent=2))
            return 0
        if args.session_action == "fork":
            print(json.dumps(fork_session(store, args.id, branch_name=args.branch, new_session_id=args.new_id), ensure_ascii=False, indent=2))
            return 0
        if args.session_action == "merge":
            print(json.dumps(merge_session(store, args.id), ensure_ascii=False, indent=2))
            return 0
        if args.session_action == "promote":
            node = promote_session_node(store, session_id=args.id, node_id=args.node_id)
            print(json.dumps(node.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.session_action == "overlay":
            node = write_overlay_node(store, session_id=args.id, content=args.content, source=args.source, node_type=args.node_type)
            print(json.dumps(node.to_dict(), ensure_ascii=False, indent=2))
            return 0

    if args.command == "index":
        from .indexing import build_index

        summary = build_index(store)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"indexed {summary['active_nodes']} active nodes")
            print(f"terms: {summary['terms']}")
            print(f"graph_nodes: {summary['graph_nodes']}")
            print(f"temporal_days: {summary['temporal_days']}")
            print(f"dense: {summary['dense']} (embedded {summary['dense_embedded']})")
            print(f"path: {summary['path']}")
        return 0

    if args.command == "doctor":
        report = doctor_report(store)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"ok: {report['ok']}")
            for problem in report["problems"]:
                print(f"- {problem}")
        return 0 if report["ok"] else 1

    if args.command == "audit":
        report = audit_report(store)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"ok: {report['ok']}")
            for key, values in report["findings"].items():
                for value in values:
                    print(f"- {key}: {value}")
        return 0 if report["ok"] else 1

    if args.command == "conflicts":
        nodes = [node.to_dict() for node in conflict_nodes(store)]
        if args.json:
            print(json.dumps(nodes, ensure_ascii=False, indent=2))
        else:
            print(f"conflicts: {len(nodes)}")
            for node in nodes:
                print(f"- {node['id']}\t{node.get('key')}\t{node['content']}")
        return 0 if not nodes else 1

    if args.command == "detect":
        candidates = detect_patch_candidates(args.text, active_nodes(store), limit=args.limit)
        if args.json:
            print(json.dumps(candidates, ensure_ascii=False, indent=2))
        else:
            if not candidates:
                print("no patch candidates detected")
            for cand in candidates:
                print(f"{cand['operation']}\t{cand['target_id']}\t{cand['trigger']}\t{cand['reason']}")
        return 0

    if args.command == "cite":
        node = store.read_node(args.memory_id)
        citation = build_citation(store, node)
        print(json.dumps(citation, ensure_ascii=False, indent=2))
        return 0

    if args.command == "log":
        patches = [patch.to_dict() for patch in store.iter_patches()]
        if args.json:
            print(json.dumps(patches, ensure_ascii=False, indent=2))
        else:
            for patch in patches:
                print(f"{patch['id']}\t{patch['operation']}\t{','.join(patch['target_ids'])}\t{patch['reason']}")
        return 0

    if args.command == "diff":
        payload = patch_diff(store, args.patch_id)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"patch: {payload['patch']['id']} {payload['patch']['operation']}")
            for node in payload["targets"]:
                print(f"target\t{node['id']}\t{node['status']}\t{node['content']}")
            for node in payload["new_nodes"]:
                print(f"new\t{node['id']}\t{node['status']}\t{node['content']}")
        return 0

    if args.command == "rollback":
        payload = rollback_patch(store, args.patch_id, reason=args.reason)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "dense":
        return run_dense(store, args)

    if args.command == "mcp":
        print("Run: cclg-mcp")
        return 0

    if args.command == "apply-codex":
        if args.write_skill:
            for path in write_codex_skills():
                print(f"wrote {path}")
        else:
            print("Run ./scripts/apply-local.sh from the CCLG repo to install the CLI symlink and Codex skill.")
        return 0

    if args.command == "code-index":
        return run_code_index(store, repo_arg=args.repo, name=args.name, json_output=args.json)

    if args.command == "code-search":
        if args.graph:
            graph = load_code_graph(Path(args.graph).expanduser())
        else:
            graph = build_code_graph(Path(args.repo).expanduser().resolve())
        if args.format == "json":
            print(json.dumps(search_code_graph(graph, args.query, limit=args.limit), ensure_ascii=False, indent=2))
        else:
            print(render_code_pack(graph, args.query, limit=args.limit))
        return 0

    if args.command == "bench":
        report = run_benchmarks(suite=args.suite, repo_root=args.repo_root)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"ok: {report['ok']}")
            for result in report["results"]:
                status = "PASS" if result["ok"] else "FAIL"
                print(f"{status}\t{result['suite']}\t{json.dumps({k: v for k, v in result.items() if k not in {'suite', 'ok'}}, ensure_ascii=False)}")
        return 0 if report["ok"] else 1

    raise AssertionError(args.command)


def import_jsonl(store: CCLGStore, path: Path) -> int:
    store.init()
    count = 0
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if "id" not in value:
            node = MemoryNode.create(
                content=value["content"],
                source=value["provenance"]["source"] if isinstance(value.get("provenance"), dict) else value["source"],
                node_type=value.get("type", "memory"),
                quote=(value.get("provenance") or {}).get("quote") if isinstance(value.get("provenance"), dict) else None,
                tags=value.get("tags", []),
                scope=value.get("scope", {}),
            )
        else:
            node = MemoryNode.from_dict(value)
        store.write_node(node)
        count += 1
    store.append_audit({"event": "jsonl_imported", "path": str(path), "count": count})
    return count


def run_dense(store: CCLGStore, args) -> int:
    from .dense import provider_status, resolve_provider

    store.init()
    config_path = store.root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    dense = dict(config.get("dense") or {})
    if args.dense_action == "status":
        print(json.dumps(provider_status(config), ensure_ascii=False, indent=2))
        return 0
    if args.dense_action == "disable":
        dense["enabled"] = False
    else:  # enable
        from .dense import DEFAULT_MODELS, detect_provider

        resolved = detect_provider() if args.provider == "auto" else args.provider
        model = args.model or DEFAULT_MODELS.get(resolved)
        dense.update({"enabled": True, "provider": args.provider, "model": model, "device": args.device})
        for key, value in (("base_url", args.base_url), ("host", args.host), ("account_id", args.account_id)):
            if value:
                dense[key] = value
        if args.download:  # only meaningful for the local backend
            provider = resolve_provider(config | {"dense": dense})
            if hasattr(provider, "_ensure_model"):
                provider._ensure_model()
    config["dense"] = dense
    atomic_write_text(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    store.append_audit({"event": "dense_config_changed", "enabled": dense.get("enabled"), "provider": dense.get("provider"), "model": dense.get("model")})
    print(json.dumps({"dense": dense}, ensure_ascii=False, indent=2))
    return 0


def load_dense_provider(store: CCLGStore):
    from .dense import CachedBackend, resolve_provider

    config_path = store.root / "config.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    backend = resolve_provider(config)
    if backend is None:
        return None
    return CachedBackend(backend, store.root / "indexes" / "dense" / "cache.json")


def parse_scope(values: list[str]) -> dict[str, str]:
    scope: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"scope must be key=value: {value}")
        key, raw = value.split("=", 1)
        scope[key.strip()] = raw.strip()
    return scope


def print_search_hits(hits, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps([{"score": hit.score, "reasons": hit.reasons, "node": hit.node.to_dict()} for hit in hits], ensure_ascii=False, indent=2))
    else:
        for hit in hits:
            print(f"{hit.node.id}\t{hit.score:.2f}\t{','.join(hit.reasons)}\t{hit.node.content}")


def grep_store(store: CCLGStore, query: str, *, limit: int) -> list[dict]:
    needle = query.casefold()
    results: list[dict] = []
    for node in active_nodes(store):
        if needle in node.content.casefold() or needle in node.id.casefold() or any(needle in tag.casefold() for tag in node.tags):
            results.append({"kind": "memory", "ref": node.id, "text": node.content})
            if len(results) >= limit:
                return results
    store.init()
    for path in sorted(store.raw_dir.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle in line.casefold():
                results.append({"kind": "raw", "ref": f"{path.relative_to(store.root)}:{line_no}", "text": line[:300]})
                if len(results) >= limit:
                    return results
    return results


def validate_paths(paths: list[Path]) -> dict:
    problems: list[str] = []
    checked = 0
    for path in paths:
        candidates = sorted([*path.rglob("*.json"), *path.rglob("*.jsonl")]) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.exists() or candidate.suffix not in {".json", ".jsonl"}:
                continue
            if is_evidence_path(candidate):
                continue
            if candidate.suffix == ".jsonl":
                for line_no, line in enumerate(candidate.read_text(encoding="utf-8").splitlines(), start=1):
                    if not line.strip():
                        continue
                    ref = f"{candidate}:{line_no}"
                    checked += 1
                    value, parse_problem = parse_json_record(line, ref=ref)
                    if parse_problem:
                        problems.append(parse_problem)
                        continue
                    problems.extend(validate_record(value, ref=ref))
            else:
                checked += 1
                value, parse_problem = parse_json_record(candidate.read_text(encoding="utf-8"), ref=str(candidate))
                if parse_problem:
                    problems.append(parse_problem)
                    continue
                problems.extend(validate_record(value, ref=str(candidate)))
    return {"ok": not problems, "checked": checked, "problems": problems}


def validate_record(value: dict, *, ref: str) -> list[str]:
    schema_version = value.get("schema_version", "")
    if schema_version == MEMORY_NODE_SCHEMA:
        return [f"{ref}: {problem}" for problem in validate_node(value)]
    if schema_version == MEMORY_PATCH_SCHEMA:
        return [f"{ref}: {problem}" for problem in validate_patch(value)]
    if schema_version.endswith("edge.v0.1"):
        return [f"{ref}: {problem}" for problem in validate_edge(value)]
    if schema_version.endswith("session.v0.1"):
        return [f"{ref}: {problem}" for problem in validate_session(value)]
    if schema_version == STORE_SCHEMA:
        return [] if value.get("root") else [f"{ref}: missing root"]
    if schema_version == CODE_GRAPH_SCHEMA:
        missing = [field for field in ["root", "generated_at", "git", "files", "symbols", "edges"] if field not in value]
        return [f"{ref}: missing {field}" for field in missing]
    return [f"{ref}: unsupported or missing schema_version {schema_version!r}"]


def is_evidence_path(path: Path) -> bool:
    return "raw" in path.parts or "audit" in path.parts


def parse_json_record(text: str, *, ref: str) -> tuple[dict, str | None]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"{ref}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
    if not isinstance(value, dict):
        return {}, f"{ref}: JSON record must be an object"
    return value, None


def run_code_index(store: CCLGStore, *, repo_arg: str, name: str | None, json_output: bool) -> int:
    repo = Path(repo_arg).expanduser().resolve()
    graph = build_code_graph(repo)
    graph_name = name or repo.name or "repo"
    path = store.active_dir / "codegraphs" / f"{graph_name}.json"
    save_code_graph(graph, path)
    payload = {"path": str(path), "root": graph.root, "files": len(graph.files), "symbols": len(graph.symbols), "edges": len(graph.edges), "git": graph.git}
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"wrote {path}")
        print(f"files: {payload['files']}")
        print(f"symbols: {payload['symbols']}")
        print(f"edges: {payload['edges']}")
    return 0


def pack_markdown(pack: dict) -> str:
    lines = [
        "# ActiveMemoryPack",
        "",
        f"query: {pack['query']}",
        f"generated_at: {pack['generated_at']}",
        "",
        "## Active Memory",
    ]
    for node in pack["memory_nodes"]:
        lines.append(f"- `{node['id']}` {node['content']} (source: {source_label(node.get('source') or node.get('provenance'))})")
    if pack["suppressed_nodes"]:
        lines.extend(["", "## Suppressed Memory"])
        for node in pack["suppressed_nodes"]:
            lines.append(f"- `{node['id']}` {node['status']}: {node['content']}")
    return "\n".join(lines) + "\n"


def doctor_report(store: CCLGStore) -> dict:
    problems: list[str] = []
    store.init()
    for required_dir in [store.raw_dir, store.nodes_dir, store.patches_dir, store.edges_dir, store.sessions_dir, store.active_dir, store.audit_dir]:
        if not required_dir.exists():
            problems.append(f"filesystem: missing directory {required_dir}")
    if not (store.root / "config.json").exists():
        problems.append("config: missing config.json")
    try:  # MCP server must import and expose the required tools.
        from .mcp_server import tool_definitions

        names = {tool["name"] for tool in tool_definitions()}
        for required_tool in ["memory.search", "memory.grep", "memory.pack", "memory.patch", "memory.recall", "memory.cite", "memory.conflicts", "memory.resolve", "memory.audit"]:
            if required_tool not in names:
                problems.append(f"mcp: missing required tool {required_tool}")
    except Exception as exc:  # noqa: BLE001 - surfaced as a doctor problem
        problems.append(f"mcp: server import failed: {exc}")
    nodes = list(store.iter_nodes())
    ids = {node.id for node in nodes}
    patches = list(store.iter_patches())
    patch_ids = {patch.id for patch in patches}
    for node in nodes:
        problems.extend(validate_node(node.to_dict(), known_ids=ids))
        if node.status == "active":
            for old_id in node.supersedes:
                if old_id not in ids:
                    continue
                old = store.read_node(old_id)
                if old.status == "active":
                    problems.append(f"{node.id}: superseded target {old_id} is still active")
    for patch in patches:
        problems.extend(validate_patch(patch.to_dict(), known_ids=ids))
    for edge in store.iter_edges():
        problems.extend(validate_edge(edge.to_dict(), known_ids=ids, known_patch_ids=patch_ids))
    for path in sorted(store.sessions_dir.glob("*.json")) if store.sessions_dir.exists() else []:
        _, parse_problem = parse_json_record(path.read_text(encoding="utf-8"), ref=str(path))
        if parse_problem:
            problems.append(parse_problem)
            continue
        problems.extend(validate_session(load_session(store, path.stem)))
    return {"ok": not problems, "problems": problems, "node_count": len(nodes), "patch_count": len(patches), "edge_count": len(list(store.iter_edges()))}


def audit_report(store: CCLGStore) -> dict:
    doctor = doctor_report(store)
    nodes = list(store.iter_nodes())
    ids = {node.id for node in nodes}
    findings: dict[str, list[str]] = {"doctor": doctor["problems"], "uncited": [], "stale_active": [], "conflicts": []}
    for node in nodes:
        if not node.source.get("label") and not node.source.get("raw_spans"):
            findings["uncited"].append(node.id)
        if node.status == "active":
            for old_id in node.supersedes:
                if old_id not in ids:
                    continue
                old = store.read_node(old_id)
                if old.status == "active":
                    findings["stale_active"].append(f"{node.id} supersedes active {old_id}")
        if node.status == "conflict_pending":
            findings["conflicts"].append(node.id)
    return {"ok": not any(findings.values()), "findings": findings}


def build_citation(store: CCLGStore, node: MemoryNode) -> dict:
    source = node.source or {}
    spans = []
    for span in source.get("raw_spans", []) if isinstance(source, dict) else []:
        source_id = span.get("source_id") if isinstance(span, dict) else None
        text = None
        if source_id:
            candidate = store.root / source_id
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8", errors="ignore")[:500]
        spans.append({"span": span, "recovered_text": text})
    return {
        "schema_version": "cclg.citation.v0.1",
        "memory_id": node.id,
        "status": node.status,
        "label": source_label(source),
        "quote": source.get("quote") if isinstance(source, dict) else None,
        "raw_spans": spans,
    }


def read_patch(store: CCLGStore, patch_id: str) -> MemoryPatch:
    for patch in store.iter_patches():
        if patch.id == patch_id:
            return patch
    raise ValueError(f"patch not found: {patch_id}")


def patch_diff(store: CCLGStore, patch_id: str) -> dict:
    patch = read_patch(store, patch_id)
    targets = [store.read_node(node_id).to_dict() for node_id in patch.target_ids if (store.nodes_dir / f"{node_id}.json").exists()]
    new_nodes = [store.read_node(node_id).to_dict() for node_id in patch.new_node_ids if (store.nodes_dir / f"{node_id}.json").exists()]
    return {"patch": patch.to_dict(), "targets": targets, "new_nodes": new_nodes}


def rollback_patch(store: CCLGStore, patch_id: str, *, reason: str) -> dict:
    patch = read_patch(store, patch_id)
    changed = []
    for node_id in patch.target_ids:
        path = store.nodes_dir / f"{node_id}.json"
        if path.exists():
            node = store.read_node(node_id)
            # Restore the exact pre-patch status, not a blanket "active".
            node.status = patch.prior_states.get(node_id, "active")
            # Drop superseded_by links this patch introduced.
            superseded_by = node.relations.get("superseded_by")
            if superseded_by:
                node.relations["superseded_by"] = [nid for nid in superseded_by if nid not in patch.new_node_ids]
            node.updated_at = now_iso()
            store.write_node(node)
            changed.append(node.id)
    for node_id in patch.new_node_ids:
        path = store.nodes_dir / f"{node_id}.json"
        if path.exists():
            node = store.read_node(node_id)
            node.status = "discarded"
            node.updated_at = now_iso()
            store.write_node(node)
            changed.append(node.id)
    rollback = MemoryPatch.create(operation="rollback", target_ids=changed or patch.target_ids, reason=reason, new_content="rollback marker", source="cli:rollback")
    rollback.new_node_ids = []
    rollback.applied_at = now_iso()
    store.write_patch(rollback)
    return {"rolled_back": patch_id, "changed_node_ids": changed, "rollback_patch": rollback.to_dict()}


def write_codex_skills() -> list[Path]:
    return [
        write_skill(
            "cclg-memory",
            """# CCLG Memory

Use this skill when the task may depend on local long-term CCLG memory.

1. Run `cclg pack --query "<current task>" --format markdown`.
2. Treat active memory as hints, not proof.
3. Verify drift-prone facts against current files, runtime, or source artifacts.
4. Do not inject suppressed memory as active context.
5. Use `cclg search "<term>"` when you need source-grounded memory details.

CCLG is local-first. Raw transcript and memory stay under `~/.cclg` unless the
user explicitly enables sync or API mode.
""",
        ),
        write_skill(
            "cclg-codegraph",
            """# CCLG Code Graph

Use this skill for codebase tasks that need repo-aware context selection.

1. Run `cclg code-search "<symbol, file, or task>" --repo "$PWD"`.
2. Use the CodeGraphPack to pick files and symbols to inspect.
3. Verify final claims by reading real source files.
4. Run `cclg code-index "$PWD"` after meaningful repo structure changes.

The code graph uses git-tracked and untracked non-ignored source files, symbol
definitions, imports, define/import edges, and git churn.
""",
        ),
        write_skill(
            "cclg-hooks",
            """# CCLG Hooks and Tools

Use this skill when wiring or validating CCLG as a tool/hook surface.

Commands:

- `cclg-hook user-prompt --include-codegraph --code-root "$PWD"`
- `cclg-hook post-tool`
- `cclg-hook pre-compact`
- `cclg-mcp`

Config examples:

- `~/.codex/hooks.cclg-example.json`
- `~/.codex/mcp.cclg-example.json`
- `~/.claude/settings.cclg-example.json`
""",
        ),
        write_skill(
            "cclg-bench",
            """# CCLG Bench

Use this skill before claiming CCLG memory/tool/codegraph behavior is working.

Run:

`cclg bench run --suite all --repo-root "$PWD"`

Suites:

- mutation
- retrieval
- pack
- hook
- mcp
- codegraph
""",
        ),
    ]


def write_skill(name: str, body: str) -> Path:
    skill_dir = Path.home() / ".codex" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
