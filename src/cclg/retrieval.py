from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import MemoryNode


TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+|[가-힣]+")

# Signals that exact/lexical retrieval beats semantic recall (PRD §7.4 routing).
EXACT_HINT_RE = re.compile(
    r"""["']             # quoted text
        |\b\d{4}-\d{2}-\d{2}\b   # ISO dates
        |\b(mem|patch|edge|sess|session)_[A-Za-z0-9]+\b  # CCLG ids
        |[/\\][\w./\\-]+         # path-like
        |\b\w+\.(py|ts|js|json|toml|md|jsonl)\b  # filenames
        |`[^`]+`          # inline code
    """,
    re.VERBOSE,
)


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


@dataclass(slots=True)
class SearchHit:
    node: MemoryNode
    score: float
    reasons: list[str]


# --- Dense provider interface (optional, disabled by default) -----------------


@runtime_checkable
class DenseProvider(Protocol):
    """Optional semantic-recall backend. Local MVP ships with none enabled."""

    def search(self, query: str, nodes: list[MemoryNode], *, limit: int) -> list[SearchHit]: ...


def get_dense_provider(config: dict | None = None) -> DenseProvider | None:
    """Return a configured dense backend, or None when dense is disabled.

    Default install performs no network egress, so dense is off unless explicitly
    enabled in config (PRD §14.1, MVP completion: dense optional / off by default).
    Backends (local sentence-transformers, Ollama, OpenAI/Schift/Google/Cloudflare,
    or any OpenAI-compatible runtime like llama.cpp/LM Studio/vLLM) live in
    ``cclg.dense``; imported lazily to keep this module dependency-free.
    """
    from .dense import resolve_provider

    return resolve_provider(config)


# --- Retrieval modes ----------------------------------------------------------


def search_nodes(query: str, nodes: list[MemoryNode], *, limit: int = 10) -> list[SearchHit]:
    """BM25-style lexical ranked search (the default sparse path)."""
    query_terms = tokenize(query)
    if not query_terms:
        return []

    docs = [tokenize(node.content + " " + " ".join(node.tags)) for node in nodes]
    doc_freq = Counter(term for doc in docs for term in set(doc))
    total_docs = max(1, len(docs))
    hits: list[SearchHit] = []

    for node, terms in zip(nodes, docs, strict=True):
        term_counts = Counter(terms)
        score = 0.0
        reasons: list[str] = []
        lower_content = node.content.lower()
        if query.lower() in lower_content:
            score += 5.0
            reasons.append("phrase")
        for term in query_terms:
            if term in term_counts:
                idf = math.log((total_docs + 1) / (doc_freq[term] + 0.5)) + 1
                score += term_counts[term] * idf
                reasons.append(term)
        if score > 0:
            hits.append(SearchHit(node=node, score=score, reasons=sorted(set(reasons))))

    # node.id as the final tiebreak: equal-score hits must rank identically
    # across runs regardless of store directory enumeration order.
    hits.sort(key=lambda hit: (hit.score, hit.node.updated_at, hit.node.id), reverse=True)
    return hits[:limit]


bm25_search = search_nodes


def grep_search(query: str, nodes: list[MemoryNode], *, limit: int = 20) -> list[SearchHit]:
    """Exact (case-insensitive substring) search over node content, id, and tags."""
    needle = query.casefold()
    if not needle:
        return []
    hits: list[SearchHit] = []
    for node in nodes:
        if needle in node.content.casefold() or needle in node.id.casefold() or any(needle in tag.casefold() for tag in node.tags):
            hits.append(SearchHit(node=node, score=1.0, reasons=["exact"]))
            if len(hits) >= limit:
                break
    return hits


def graph_search(query: str, nodes: list[MemoryNode], *, limit: int = 10) -> list[SearchHit]:
    """Seed with lexical hits, then expand one hop along memory relations.

    Follows supersedes/refines/expands/narrows/depends_on/derived_from so a task
    or decision pulls in its directly related active memory (PRD §7.4 graph mode).
    """
    by_id = {node.id: node for node in nodes}
    seeds = search_nodes(query, nodes, limit=limit)
    scored: dict[str, SearchHit] = {hit.node.id: hit for hit in seeds}
    relation_keys = ("supersedes", "refines", "expands", "narrows", "depends_on", "derived_from")
    for hit in seeds:
        for relation in relation_keys:
            for neighbor_id in hit.node.relations.get(relation, []):
                neighbor = by_id.get(neighbor_id)
                if neighbor is None or neighbor.id in scored:
                    continue
                scored[neighbor.id] = SearchHit(node=neighbor, score=hit.score * 0.5, reasons=[f"graph:{relation}"])
    ranked = sorted(scored.values(), key=lambda hit: (hit.score, hit.node.updated_at, hit.node.id), reverse=True)
    return ranked[:limit]


def _as_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def temporal_search(query: str, nodes: list[MemoryNode], *, as_of: datetime | None = None, limit: int = 10) -> list[SearchHit]:
    """Time-aware retrieval: filter to nodes effective at ``as_of`` (or now), then rank by recency + lexical match."""
    candidates: list[MemoryNode] = []
    for node in nodes:
        if as_of is not None:
            start = _as_dt(node.effective_from)
            end = _as_dt(node.effective_until)
            if start and start > as_of:
                continue
            if end and end <= as_of:
                continue
        candidates.append(node)
    lexical = {hit.node.id: hit.score for hit in search_nodes(query, candidates, limit=len(candidates) or 1)} if query else {}
    hits = [
        SearchHit(node=node, score=lexical.get(node.id, 0.0) + _recency_score(node), reasons=["temporal"])
        for node in candidates
    ]
    # node.id as the final tiebreak: equal-score hits must rank identically
    # across runs regardless of store directory enumeration order.
    hits.sort(key=lambda hit: (hit.score, hit.node.updated_at, hit.node.id), reverse=True)
    return hits[:limit]


def _recency_score(node: MemoryNode) -> float:
    dt = _as_dt(node.updated_at) or _as_dt(node.created_at)
    return dt.timestamp() / 1e12 if dt else 0.0


def route_query(query: str) -> list[str]:
    """Pick retrieval modes for a query (PRD §7.4 default routing policy)."""
    if EXACT_HINT_RE.search(query):
        return ["grep", "bm25"]
    return ["bm25", "graph"]


_MODE_FUNCS = {
    "grep": grep_search,
    "bm25": bm25_search,
    "graph": graph_search,
    "temporal": temporal_search,
}


def fuse_results(results: dict[str, list[SearchHit]], *, k: int = 60, limit: int = 10) -> list[SearchHit]:
    """Reciprocal Rank Fusion across modes (PRD Step 4 RRF fusion)."""
    fused: dict[str, float] = {}
    nodes: dict[str, MemoryNode] = {}
    reasons: dict[str, list[str]] = {}
    for mode, hits in results.items():
        for rank, hit in enumerate(hits):
            nodes[hit.node.id] = hit.node
            fused[hit.node.id] = fused.get(hit.node.id, 0.0) + 1.0 / (k + rank + 1)
            reasons.setdefault(hit.node.id, []).append(mode)
    ranked = sorted(fused.items(), key=lambda item: (item[1], item[0]), reverse=True)
    return [SearchHit(node=nodes[node_id], score=round(score, 6), reasons=reasons[node_id]) for node_id, score in ranked[:limit]]


def search_memory(
    query: str,
    nodes: list[MemoryNode],
    *,
    mode: str = "auto",
    limit: int = 10,
    as_of: datetime | None = None,
    dense: DenseProvider | None = None,
) -> list[SearchHit]:
    """Unified entry point honouring an explicit mode or the auto router with RRF fusion."""
    if mode == "temporal":
        return temporal_search(query, nodes, as_of=as_of, limit=limit)
    if mode in _MODE_FUNCS:
        return _MODE_FUNCS[mode](query, nodes, limit=limit)
    if mode == "dense":
        if dense is None:
            return search_nodes(query, nodes, limit=limit)  # graceful fallback when dense is disabled
        return dense.search(query, nodes, limit=limit)
    # auto
    modes = route_query(query)
    results = {m: _MODE_FUNCS[m](query, nodes, limit=limit) for m in modes}
    if dense is not None:
        results["dense"] = dense.search(query, nodes, limit=limit)
    return fuse_results(results, limit=limit)
