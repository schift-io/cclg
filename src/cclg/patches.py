from __future__ import annotations

import re

from .models import MemoryEdge, MemoryNode, MemoryPatch, now_iso
from .retrieval import search_nodes
from .store import CCLGStore


EDGE_BY_OPERATION = {
    "supersede": "supersedes",
    "refine": "refines",
    "expand": "expands",
    "narrow": "narrows",
    "merge": "derived_from",
    "split": "derived_from",
    "resolve_conflict": "resolves",
}

# Operations that replace the prior target(s): the new node supersedes the old
# one(s), and the old node(s) must leave the effective view. Per PRD §7.2 an
# expansion/refinement must not leave a stale duplicate active alongside the new
# node, so expand/merge/split also retire their targets.
SUPERSEDING_OPERATIONS = {
    "update",
    "supersede",
    "refine",
    "expand",
    "narrow",
    "merge",
    "split",
    "resolve_conflict",
}

# Patch operations whose application retires the *old* target node(s) from the
# effective view, independent of whatever `apply_patch` did or didn't bake into
# `node.status`. Superset of SUPERSEDING_OPERATIONS: also covers the
# expire/forget/deprecate branch of `apply_patch`, which mutates target status
# directly rather than emitting a "supersedes" relation. This is the exact same
# set as the TS mirror's `EXCLUDING_PATCH_OPERATIONS`
# (derivatives/schift-ai-memory/packages/core/src/cclg-effective-view.ts) —
# keep the two lists identical; a divergence here reintroduces the cross-impl
# effective-view mismatch documented in docs/CCLG_CONTAINER.md's load
# semantics section. "create" and "rollback" are deliberately excluded: neither
# retires a target ("rollback" falls through to the generic branch in
# `apply_patch` but was never added to SUPERSEDING_OPERATIONS, so its target's
# status is left `active`).
RETIRING_PATCH_OPERATIONS = SUPERSEDING_OPERATIONS | {"expire", "forget", "deprecate"}


def apply_patch(store: CCLGStore, patch: MemoryPatch) -> list[MemoryNode]:
    """Apply a memory patch and return nodes written by the operation."""
    written: list[MemoryNode] = []
    targets = [store.read_node(target_id) for target_id in patch.target_ids]
    patch.prior_states = {target.id: target.status for target in targets}

    if patch.operation in {"expire", "forget", "deprecate"}:
        status = {"expire": "expired", "forget": "forgotten", "deprecate": "deprecated"}[patch.operation]
        for node in targets:
            node.status = status
            node.updated_at = now_iso()
            store.write_node(node)
            written.append(node)
        patch.applied_at = now_iso()
        store.write_patch(patch)
        return written

    new_type = targets[0].type if targets else "memory"
    new_scope = dict(targets[0].scope) if targets else {}
    new_key = targets[0].key if targets else None
    new_tags = sorted({tag for target in targets for tag in target.tags})
    new_node = MemoryNode.create(
        content=patch.new_content or "",
        source=f"patch:{patch.id}",
        node_type=new_type,
        quote=patch.reason,
        scope=new_scope,
        tags=new_tags,
    )
    new_node.key = new_key
    relation_key = EDGE_BY_OPERATION.get(patch.operation, "derived_from")
    if relation_key in new_node.relations:
        new_node.relations[relation_key] = [target.id for target in targets]
    if patch.operation in SUPERSEDING_OPERATIONS:
        new_node.relations["supersedes"] = [target.id for target in targets]

    old_status = "superseded" if patch.operation in SUPERSEDING_OPERATIONS else "active"
    for node in targets:
        node.status = old_status
        if old_status == "superseded":
            node.relations.setdefault("superseded_by", []).append(new_node.id)
        node.updated_at = now_iso()
        store.write_node(node)
        written.append(node)

    store.write_node(new_node)
    patch.new_node_ids = [new_node.id]
    patch.applied_at = now_iso()
    store.write_patch(patch)
    for target in targets:
        edge = MemoryEdge.create(from_id=new_node.id, to_id=target.id, edge_type=relation_key, source_patch_id=patch.id)
        store.write_edge(edge)
    written.append(new_node)
    return written


def _scope_rank(node: MemoryNode, session_id: str | None) -> int:
    """Effective-view scope precedence: session > project > workspace > global."""
    scope = node.scope or {}
    if node.status == "active_session" and scope.get("session") == session_id:
        return 4
    if scope.get("project"):
        return 3
    if scope.get("workspace"):
        return 2
    return 1


def _resolve_scope_precedence(nodes: list[MemoryNode], session_id: str | None) -> list[MemoryNode]:
    """Collapse keyed nodes so only the highest-precedence node per key survives.

    Nodes without a ``key`` are independent facts and are always kept.
    """
    winners: dict[str, MemoryNode] = {}
    keyless: list[MemoryNode] = []
    for node in nodes:
        if not node.key:
            keyless.append(node)
            continue
        current = winners.get(node.key)
        if current is None:
            winners[node.key] = node
            continue
        rank, current_rank = _scope_rank(node, session_id), _scope_rank(current, session_id)
        if rank > current_rank or (rank == current_rank and node.updated_at > current.updated_at):
            winners[node.key] = node
    return keyless + list(winners.values())


def effective_view(
    nodes: list[MemoryNode],
    *,
    session_id: str | None = None,
    patches: list[MemoryPatch] | None = None,
) -> list[MemoryNode]:
    """Pure effective-view over a node list (no store).

    Keeps active nodes (+ this session's active_session overlay), drops
    superseded/expired/forgotten/etc., then applies scope precedence. This is the
    store-less core so CCLG can run as a library over memories owned by an external
    store (e.g. the Schift memory backend).

    ``patches`` is optional and defaults to ``None``, which preserves this
    function's original behavior byte-for-byte (every existing caller —
    ``active_nodes()`` below, agent-hub's ``cclg_grounding.py``/``pack.py``, ...
    — keeps working unchanged because a live ``CCLGStore`` always bakes a
    patch's effect into ``node.status`` via ``apply_patch`` *before* a node is
    ever read back out, so ``node.status`` alone is authoritative there).

    When ``patches`` *is* given, this additionally and independently excludes
    any node referenced as a `target_ids` entry of a patch whose operation is
    in ``RETIRING_PATCH_OPERATIONS``, regardless of what that node's own
    `status` field says. This closes the gap a loaded `.cclg` container can hit
    that a live store never can: a container is schema-valid but was produced
    without replaying `apply_patch`'s status mutation (e.g. a producer records
    a `MemoryPatch(operation="supersede")` without also flipping the target
    node's `status` to `superseded`) — status-only filtering would then
    wrongly keep a superseded/forgotten node in the effective view. Per
    docs/CCLG_CONTAINER.md's load semantics section, baked status is an
    optimization a producer MAY apply; a conforming reader MUST NOT depend on
    it for correctness. Mirrors the TS port's
    `effectiveView(nodes, patches, sessionId)`
    (derivatives/schift-ai-memory/packages/core/src/cclg-effective-view.ts) —
    ``ContainerBundle.effective_view()`` in `container.py` is the canonical
    caller that wires a loaded container's patches through here.
    """
    excluded_ids: set[str] = set()
    if patches:
        for patch in patches:
            if patch.operation in RETIRING_PATCH_OPERATIONS:
                excluded_ids.update(patch.target_ids)

    candidates: list[MemoryNode] = []
    for node in nodes:
        if node.id in excluded_ids:
            continue
        if node.status == "active":
            candidates.append(node)
        elif session_id and node.status == "active_session" and (node.scope or {}).get("session") == session_id:
            candidates.append(node)
    return _resolve_scope_precedence(candidates, session_id)


def active_nodes(store: CCLGStore, *, session_id: str | None = None) -> list[MemoryNode]:
    return effective_view(list(store.iter_nodes()), session_id=session_id)


def suppressed_nodes(store: CCLGStore) -> list[MemoryNode]:
    return [node for node in store.iter_nodes() if node.status not in {"active", "active_session"}]


def conflict_nodes(store: CCLGStore) -> list[MemoryNode]:
    return [node for node in store.iter_nodes() if node.status == "conflict_pending"]


# --- Patch / contradiction detection (PRD §7.2, Step 6) ----------------------

CORRECTION_TRIGGERS = ["아니", "그게 아니라", "정확히는", "수정", "정정", "actually", "correction", "not quite"]
TEMPORAL_TRIGGERS = ["이제", "앞으로", "더 이상", "더이상", "바꿔", "폐기", "from now on", "no longer", "deprecate"]
SCOPE_TRIGGERS = ["이번 프로젝트", "이 repo", "이 레포", "global로", "local만", "this repo", "this project", "globally", "only local"]
EXPANSION_TRIGGERS = ["도 되어야", "다 지원", "지원해야", "도 지원", "포함해야", "also support", "must include", "as well"]
NEGATION_TRIGGERS = ["하지 마", "하지마", "쓰지 마", "쓰지마", "말고", "금지", "제외", "do not", "don't", "must not", "exclude"]


def _contains(text: str, triggers: list[str]) -> str | None:
    lowered = text.lower()
    for trigger in triggers:
        if trigger.lower() in lowered:
            return trigger
    return None


def classify_patch(text: str) -> str | None:
    """Classify a raw user turn into a patch operation, or None if no trigger fires."""
    if _contains(text, ["폐기", "deprecate"]):
        return "deprecate"
    if _contains(text, EXPANSION_TRIGGERS):
        return "expand"
    if _contains(text, NEGATION_TRIGGERS):
        return "narrow"
    if _contains(text, SCOPE_TRIGGERS):
        return "narrow"
    if _contains(text, CORRECTION_TRIGGERS):
        return "supersede"
    if _contains(text, TEMPORAL_TRIGGERS):
        return "update"
    return None


def detect_patch_candidates(text: str, nodes: list[MemoryNode], *, limit: int = 3) -> list[dict]:
    """Detect candidate mutations from a raw user turn against the effective view.

    Returns candidate dicts ``{operation, target_id, reason, trigger, score}``.
    Deterministic and embedding-independent; the caller decides whether to apply.
    """
    operation = classify_patch(text)
    if operation is None:
        return []
    trigger = (
        _contains(text, EXPANSION_TRIGGERS)
        or _contains(text, NEGATION_TRIGGERS)
        or _contains(text, SCOPE_TRIGGERS)
        or _contains(text, CORRECTION_TRIGGERS)
        or _contains(text, TEMPORAL_TRIGGERS)
        or ""
    )
    # Strip the trigger phrasing so retrieval matches the referenced fact, not the cue word.
    cleaned = re.sub(r"\s+", " ", text).strip()
    hits = search_nodes(cleaned, nodes, limit=limit)
    candidates: list[dict] = []
    for hit in hits:
        candidates.append(
            {
                "operation": operation,
                "target_id": hit.node.id,
                "reason": f"Detected '{trigger}' correction in user turn.",
                "trigger": trigger,
                "score": round(hit.score, 4),
            }
        )
    return candidates
