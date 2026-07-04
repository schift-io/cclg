from __future__ import annotations

from .format import ACTIVE_MEMORY_PACK_SCHEMA
from .models import ActiveMemoryPack, MemoryNode, now_iso
from .patches import effective_view
from .retrieval import search_nodes
from .store import CCLGStore


def compile_pack_from_nodes(
    nodes: list[MemoryNode],
    query: str,
    *,
    max_nodes: int = 12,
    max_chars: int = 6000,
    session_id: str | None = None,
) -> ActiveMemoryPack:
    """Store-less pack compiler: effective-view + rank + budget over a node list.

    Use when memories are owned by an external store (e.g. Schift). ``nodes`` may
    contain superseded/forgotten nodes — they are filtered into the suppressed
    section, never injected as active.
    """
    active = effective_view(nodes, session_id=session_id)
    active_ids = {node.id for node in active}
    hits = search_nodes(query, active, limit=max_nodes) if query else []
    selected = [hit.node for hit in hits] if hits else active[:max_nodes]

    used_chars = 0
    packed = []
    for node in selected:
        entry = node.to_dict()
        if used_chars + len(node.content) > max_chars and packed:
            break
        used_chars += len(node.content)
        packed.append(entry)

    suppressed = []
    for node in nodes:
        if node.id in active_ids or node.status in {"active", "active_session"}:
            continue
        preview = node.content[:240]
        if used_chars + len(preview) > max_chars and suppressed:
            break
        used_chars += len(preview)
        suppressed.append({"id": node.id, "status": node.status, "content": preview, "supersedes": node.supersedes})

    return ActiveMemoryPack(
        schema_version=ACTIVE_MEMORY_PACK_SCHEMA,
        query=query,
        generated_at=now_iso(),
        memory_nodes=packed,
        suppressed_nodes=suppressed,
        budget={"max_nodes": max_nodes, "max_chars": max_chars, "used_chars": used_chars},
    )


def compile_pack(store: CCLGStore, query: str, *, max_nodes: int = 12, max_chars: int = 6000, session_id: str | None = None) -> ActiveMemoryPack:
    return compile_pack_from_nodes(
        list(store.iter_nodes()),
        query,
        max_nodes=max_nodes,
        max_chars=max_chars,
        session_id=session_id,
    )
