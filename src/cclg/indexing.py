from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .format import CCLG_NAMESPACE
from .models import now_iso
from .patches import active_nodes
from .retrieval import tokenize
from .store import CCLGStore


INDEX_SCHEMA = "cclg.index.v0.1"


def build_index(store: CCLGStore) -> dict[str, Any]:
    """Build and persist embedding-independent retrieval indexes.

    Produces a BM25 term postings index, a graph adjacency index, and a temporal
    bucket index under ``<root>/indexes/``. Dense indexing is intentionally
    omitted (optional, off by default) so the default build performs no network
    egress (PRD §7.4 / §14.1).
    """
    store.init()
    index_dir = store.root / "indexes"
    for sub in ("bm25", "graph", "temporal"):
        (index_dir / sub).mkdir(parents=True, exist_ok=True)

    nodes = active_nodes(store)

    postings: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for term in set(tokenize(node.content + " " + " ".join(node.tags))):
            postings[term].append(node.id)
    bm25 = {"schema_version": INDEX_SCHEMA, "generated_at": now_iso(), "doc_count": len(nodes), "postings": {term: sorted(ids) for term, ids in sorted(postings.items())}}

    adjacency: dict[str, dict[str, list[str]]] = {}
    for node in store.iter_nodes():
        relations = {rel: list(ids) for rel, ids in node.relations.items() if ids}
        if relations:
            adjacency[node.id] = relations
    graph = {"schema_version": INDEX_SCHEMA, "generated_at": now_iso(), "adjacency": adjacency}

    buckets: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        day = (node.effective_from or node.created_at or "")[:10]
        buckets[day].append(node.id)
    temporal = {"schema_version": INDEX_SCHEMA, "generated_at": now_iso(), "buckets": {day: sorted(ids) for day, ids in sorted(buckets.items())}}

    _write(index_dir / "bm25" / "postings.json", bm25)
    _write(index_dir / "graph" / "adjacency.json", graph)
    _write(index_dir / "temporal" / "buckets.json", temporal)

    # N-Triples export so an external SPARQL store (e.g. Oxigraph) can load the
    # memory graph directly. Optional consumer; the JSON adjacency stays primary.
    triples = export_ntriples(store)
    (index_dir / "graph" / "memory.nt").write_text(triples, encoding="utf-8")

    dense_status = "disabled"
    dense_embedded = 0
    config_path = store.root / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config = {}
        if (config.get("dense") or {}).get("enabled"):
            from .dense import CachedBackend, resolve_provider

            backend = resolve_provider(config)
            if backend is not None:
                dense_status = backend.name
                try:  # warming is best-effort: a missing daemon/key must not fail indexing
                    dense_embedded = CachedBackend(backend, index_dir / "dense" / "cache.json").warm(nodes)
                except Exception as exc:  # noqa: BLE001
                    dense_status = f"{backend.name} (warm failed: {exc})"

    summary = {
        "schema_version": INDEX_SCHEMA,
        "generated_at": now_iso(),
        "active_nodes": len(nodes),
        "terms": len(postings),
        "graph_nodes": len(adjacency),
        "temporal_days": len(buckets),
        "dense": dense_status,
        "dense_embedded": dense_embedded,
        "path": str(index_dir),
    }
    _write(index_dir / "meta.json", summary)
    store.append_audit({"event": "index_built", **{k: v for k, v in summary.items() if k != "schema_version"}})
    return summary


def export_ntriples(store: CCLGStore) -> str:
    """Serialize the memory graph as N-Triples for SPARQL/Oxigraph consumers."""
    base = CCLG_NAMESPACE.rstrip("/")

    def iri(suffix: str) -> str:
        return f"<{base}/{suffix}>"

    def literal(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'

    lines: list[str] = []
    for node in store.iter_nodes():
        subject = iri(node.id)
        lines.append(f"{subject} {iri('type')} {literal(node.type)} .")
        lines.append(f"{subject} {iri('status')} {literal(node.status)} .")
        if node.key:
            lines.append(f"{subject} {iri('key')} {literal(node.key)} .")
        for relation, targets in node.relations.items():
            for target in targets:
                lines.append(f"{subject} {iri('rel/' + relation)} {iri(target)} .")
    return "\n".join(lines) + ("\n" if lines else "")


def _write(path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
