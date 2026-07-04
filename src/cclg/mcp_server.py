from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .codegraph import build_code_graph, render_code_pack, save_code_graph
from .models import MemoryNode, MemoryPatch
from .pack import compile_pack
from .patches import active_nodes, apply_patch, conflict_nodes
from .retrieval import search_nodes
from .schema import validate_edge, validate_node, validate_patch, validate_session
from .session import load_session
from .store import CCLGStore


ToolHandler = Callable[[dict[str, Any], CCLGStore], dict[str, Any]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cclg-mcp", description="CCLG MCP stdio server")
    parser.add_argument("--root", help="CCLG store root")
    parser.add_argument("--line-delimited", action="store_true", help="Use newline-delimited JSON instead of Content-Length frames")
    args = parser.parse_args(argv)
    store = CCLGStore(args.root)
    for message in iter_messages(sys.stdin.buffer, line_delimited=args.line_delimited):
        response = handle_message(message, store)
        if response is not None:
            write_message(sys.stdout.buffer, response, line_delimited=args.line_delimited)
            sys.stdout.flush()
    return 0


def handle_message(message: dict[str, Any], store: CCLGStore) -> dict[str, Any] | None:
    if "__parse_error__" in message:
        return error_response(message.get("id"), -32700, str(message["__parse_error__"]))
    method = message.get("method")
    msg_id = message.get("id")
    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cclg", "version": "0.1.0"},
            }
        elif method == "notifications/initialized":
            return None
        elif method == "tools/list":
            result = {"tools": tool_definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = call_tool(str(params.get("name")), dict(params.get("arguments") or {}), store)
        else:
            return error_response(msg_id, -32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except Exception as exc:  # noqa: BLE001 - MCP tool errors should be visible to the client.
        return error_response(msg_id, -32000, str(exc))


def call_tool(name: str, args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    handlers: dict[str, ToolHandler] = {
        "cclg.search": tool_search,
        "memory.search": tool_search,
        "cclg.grep": tool_grep,
        "memory.grep": tool_grep,
        "cclg.bm25": tool_search,
        "memory.bm25": tool_search,
        "cclg.pack": tool_pack,
        "memory.pack": tool_pack,
        "cclg.add": tool_add,
        "cclg.patch": tool_patch,
        "memory.patch": tool_patch,
        "cclg.raw": tool_raw,
        "cclg.code_index": tool_code_index,
        "cclg.code_search": tool_code_search,
        "cclg.bench": tool_bench,
        "cclg.audit": tool_audit,
        "memory.audit": tool_audit,
        "cclg.recall": tool_recall,
        "memory.recall": tool_recall,
        "cclg.cite": tool_cite,
        "memory.cite": tool_cite,
        "cclg.conflicts": tool_conflicts,
        "memory.conflicts": tool_conflicts,
        "cclg.resolve": tool_resolve,
        "memory.resolve": tool_resolve,
    }
    if name not in handlers:
        return tool_result({"error": f"unknown tool {name}"}, is_error=True)
    return handlers[name](args, store)


def tool_search(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    from .cli import load_dense_provider
    from .retrieval import search_memory

    mode = str(args.get("mode", "bm25"))
    hits = search_memory(str(args.get("query", "")), active_nodes(store), mode=mode, limit=int(args.get("limit", 10)), dense=load_dense_provider(store))
    data = [{"score": hit.score, "reasons": hit.reasons, "node": hit.node.to_dict()} for hit in hits]
    return tool_result({"schema_version": "cclg.tool_result.v0.1", "mode": mode, "hits": data})


def tool_grep(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    query = str(args.get("query", ""))
    needle = query.casefold()
    limit = int(args.get("limit", 20))
    results: list[dict[str, Any]] = []
    for node in active_nodes(store):
        if needle in node.content.casefold() or needle in node.id.casefold():
            results.append({"kind": "memory", "ref": node.id, "text": node.content})
            if len(results) >= limit:
                break
    if len(results) < limit:
        for path in sorted(store.raw_dir.rglob("*")) if store.raw_dir.exists() else []:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    results.append({"kind": "raw", "ref": f"{path.relative_to(store.root)}:{line_no}", "text": line[:300]})
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
    return tool_result({"schema_version": "cclg.tool_result.v0.1", "mode": "grep", "hits": results})


def tool_pack(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    pack = compile_pack(
        store,
        str(args.get("query", "")),
        max_nodes=int(args.get("max_nodes", 12)),
        max_chars=int(args.get("max_chars", 6000)),
        session_id=args.get("session_id"),
    )
    return tool_result(pack.to_dict())


def tool_add(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    node = MemoryNode.create(
        content=str(args["content"]),
        source=str(args["source"]),
        node_type=str(args.get("type", "memory")),
        quote=args.get("quote"),
        tags=list(args.get("tags", [])),
        scope=dict(args.get("scope", {})),
    )
    store.write_node(node)
    return tool_result(node.to_dict())


def tool_patch(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    patch = MemoryPatch.create(
        operation=args["operation"],
        target_ids=list(args["target_ids"]),
        reason=str(args["reason"]),
        new_content=args.get("new_content"),
        source=str(args.get("source", "mcp")),
    )
    written = apply_patch(store, patch)
    return tool_result({"patch": patch.to_dict(), "written_node_ids": [node.id for node in written]})


def tool_raw(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    path = store.append_raw(str(args.get("name", "mcp-raw.json")), str(args.get("text", "")))
    return tool_result({"schema_version": "cclg.tool_result.v0.1", "path": str(path)})


def tool_code_index(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    root = Path(str(args.get("root", "."))).expanduser().resolve()
    graph = build_code_graph(root)
    out = store.active_dir / "codegraphs" / f"{root.name or 'repo'}.json"
    save_code_graph(graph, out)
    return tool_result({"schema_version": "cclg.tool_result.v0.1", "path": str(out), "files": len(graph.files), "symbols": len(graph.symbols), "edges": len(graph.edges)})


def tool_code_search(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    root = Path(str(args.get("root", "."))).expanduser().resolve()
    graph = build_code_graph(root)
    text = render_code_pack(graph, str(args.get("query", "")), limit=int(args.get("limit", 20)))
    return {"content": [{"type": "text", "text": text}], "structuredContent": {"schema_version": "cclg.tool_result.v0.1", "root": str(root), "query": args.get("query", "")}}


def tool_bench(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    from .bench import run_benchmarks

    result = run_benchmarks(suite=str(args.get("suite", "all")), repo_root=args.get("repo_root"))
    return tool_result(result)


def tool_audit(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    nodes = list(store.iter_nodes())
    node_ids = {node.id for node in nodes}
    patches = list(store.iter_patches())
    patch_ids = {patch.id for patch in patches}
    problems: list[str] = []
    for node in nodes:
        problems.extend(validate_node(node.to_dict(), known_ids=node_ids))
    for patch in patches:
        problems.extend(validate_patch(patch.to_dict(), known_ids=node_ids))
    for edge in store.iter_edges():
        problems.extend(validate_edge(edge.to_dict(), known_ids=node_ids, known_patch_ids=patch_ids))
    for path in sorted(store.sessions_dir.glob("*.json")) if store.sessions_dir.exists() else []:
        problems.extend(validate_session(load_session(store, path.stem)))
    return tool_result({"schema_version": "cclg.tool_result.v0.1", "ok": not problems, "problems": problems})


def tool_recall(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    """Source recall: return matching active memory together with provenance citations."""
    from .cli import build_citation

    query = str(args.get("query", ""))
    limit = int(args.get("limit", 8))
    hits = search_nodes(query, active_nodes(store), limit=limit)
    data = [{"score": hit.score, "node": hit.node.to_dict(), "citation": build_citation(store, hit.node)} for hit in hits]
    return tool_result({"schema_version": "cclg.tool_result.v0.1", "mode": "recall", "hits": data})


def tool_cite(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    from .cli import build_citation

    node = store.read_node(str(args["memory_id"]))
    return tool_result(build_citation(store, node))


def tool_conflicts(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    nodes = [node.to_dict() for node in conflict_nodes(store)]
    return tool_result({"schema_version": "cclg.tool_result.v0.1", "conflicts": nodes, "count": len(nodes)})


def tool_resolve(args: dict[str, Any], store: CCLGStore) -> dict[str, Any]:
    patch = MemoryPatch.create(
        operation="resolve_conflict",
        target_ids=list(args["target_ids"]),
        reason=str(args.get("reason", "Resolved conflict.")),
        new_content=str(args["new_content"]),
        source=str(args.get("source", "mcp:resolve")),
    )
    written = apply_patch(store, patch)
    return tool_result({"patch": patch.to_dict(), "written_node_ids": [node.id for node in written]})


def tool_result(data: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        "structuredContent": data if isinstance(data, dict) else {"result": data},
        "isError": is_error,
    }


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {"name": "cclg.search", "description": "Search active CCLG memory. mode: auto|grep|bm25|dense|graph|temporal (default bm25). Dense is local and off unless configured.", "inputSchema": object_schema({"query": "string", "limit": "integer", "mode": "string"}, ["query"])},
        {"name": "cclg.grep", "description": "Exact search active memory and raw evidence. Use for IDs, dates, filenames, code, error strings, and exact previous wording.", "inputSchema": object_schema({"query": "string", "limit": "integer"}, ["query"])},
        {"name": "cclg.bm25", "description": "Lexical ranked active-memory search. Use when semantic embeddings are unnecessary or unavailable.", "inputSchema": object_schema({"query": "string", "limit": "integer", "mode": "string"}, ["query"])},
        {"name": "cclg.pack", "description": "Compile an ActiveMemoryPack for a task", "inputSchema": object_schema({"query": "string", "max_nodes": "integer", "max_chars": "integer", "session_id": "string"}, [])},
        {"name": "cclg.add", "description": "Add a source-grounded memory node", "inputSchema": object_schema({"content": "string", "source": "string", "type": "string", "quote": "string", "tags": "array", "scope": "object"}, ["content", "source"])},
        {"name": "cclg.patch", "description": "Apply a memory patch", "inputSchema": object_schema({"operation": "string", "target_ids": "array", "reason": "string", "new_content": "string", "source": "string"}, ["operation", "target_ids", "reason"])},
        {"name": "cclg.raw", "description": "Append raw source text", "inputSchema": object_schema({"name": "string", "text": "string"}, ["text"])},
        {"name": "cclg.code_index", "description": "Build and store a code graph for a repository", "inputSchema": object_schema({"root": "string"}, [])},
        {"name": "cclg.code_search", "description": "Build/search a repo code graph and return a code pack", "inputSchema": object_schema({"root": "string", "query": "string", "limit": "integer"}, ["query"])},
        {"name": "cclg.bench", "description": "Run CCLG benchmark suites", "inputSchema": object_schema({"suite": "string", "repo_root": "string"}, [])},
        {"name": "cclg.audit", "description": "Validate CCLG schemas, dangling references, stale active nodes, and session records.", "inputSchema": object_schema({}, [])},
        {"name": "cclg.recall", "description": "Source recall. Returns active memory plus provenance citations and recovered raw spans. Use to ground a remembered fact in its original source.", "inputSchema": object_schema({"query": "string", "limit": "integer"}, ["query"])},
        {"name": "cclg.cite", "description": "Recover the source turn/span and quote for a single active memory id.", "inputSchema": object_schema({"memory_id": "string"}, ["memory_id"])},
        {"name": "cclg.conflicts", "description": "List unresolved conflict_pending memory awaiting review.", "inputSchema": object_schema({}, [])},
        {"name": "cclg.resolve", "description": "Resolve a conflict by superseding the conflicting node(s) with reconciled content.", "inputSchema": object_schema({"target_ids": "array", "new_content": "string", "reason": "string", "source": "string"}, ["target_ids", "new_content"])},
        {"name": "memory.search", "description": "Alias for cclg.search.", "inputSchema": object_schema({"query": "string", "limit": "integer", "mode": "string"}, ["query"])},
        {"name": "memory.grep", "description": "Alias for cclg.grep.", "inputSchema": object_schema({"query": "string", "limit": "integer"}, ["query"])},
        {"name": "memory.bm25", "description": "Alias for cclg.bm25.", "inputSchema": object_schema({"query": "string", "limit": "integer"}, ["query"])},
        {"name": "memory.pack", "description": "Alias for cclg.pack.", "inputSchema": object_schema({"query": "string", "max_nodes": "integer", "max_chars": "integer", "session_id": "string"}, [])},
        {"name": "memory.patch", "description": "Alias for cclg.patch.", "inputSchema": object_schema({"operation": "string", "target_ids": "array", "reason": "string", "new_content": "string", "source": "string"}, ["operation", "target_ids", "reason"])},
        {"name": "memory.audit", "description": "Alias for cclg.audit.", "inputSchema": object_schema({}, [])},
        {"name": "memory.recall", "description": "Alias for cclg.recall.", "inputSchema": object_schema({"query": "string", "limit": "integer"}, ["query"])},
        {"name": "memory.cite", "description": "Alias for cclg.cite.", "inputSchema": object_schema({"memory_id": "string"}, ["memory_id"])},
        {"name": "memory.conflicts", "description": "Alias for cclg.conflicts.", "inputSchema": object_schema({}, [])},
        {"name": "memory.resolve", "description": "Alias for cclg.resolve.", "inputSchema": object_schema({"target_ids": "array", "new_content": "string", "reason": "string", "source": "string"}, ["target_ids", "new_content"])},
    ]


def object_schema(properties: dict[str, str], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": typ} for name, typ in properties.items()},
        "required": required,
        "additionalProperties": False,
    }


def error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def iter_messages(stream, *, line_delimited: bool):
    if line_delimited:
        for line in stream:
            if line.strip():
                try:
                    yield json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    yield {"jsonrpc": "2.0", "id": None, "__parse_error__": f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"}
        return

    while True:
        headers: list[str] = []
        while True:
            line = stream.readline()
            if not line:
                return
            if line in (b"\r\n", b"\n"):
                break
            headers.append(line.decode("ascii", errors="ignore").strip())

        length = 0
        for line in headers:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        if length <= 0:
            break
        body = stream.read(length)
        if not body:
            return
        try:
            yield json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            yield {"jsonrpc": "2.0", "id": None, "__parse_error__": f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"}


def write_message(stream, message: dict[str, Any], *, line_delimited: bool) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if line_delimited:
        stream.write(body + b"\n")
    else:
        stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
