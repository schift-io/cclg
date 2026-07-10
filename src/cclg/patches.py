from __future__ import annotations

import contextlib
import re

try:  # POSIX advisory locking; absent on non-POSIX platforms (best-effort there).
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .models import MemoryEdge, MemoryNode, MemoryPatch, now_iso
from .retrieval import SearchHit, search_nodes
from .store import CCLGStore


@contextlib.contextmanager
def _patch_lock(store: CCLGStore):
    """Serialize patch application across concurrent processes.

    apply_patch does a multi-file read-modify-write (retire targets + write the
    replacement + patch + edges). Two concurrent apply_patch calls on overlapping
    targets could interleave and drop a ``superseded_by`` link or leave duplicate
    active supersedors. An exclusive advisory lock makes application atomic.
    Degrades to a no-op on non-POSIX platforms.
    """
    store.init()
    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        yield
        return
    lock_path = store.patches_dir / ".patches.lock"
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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

# The complete closed set of patch operations this reader's effective-view
# logic has classified one way or the other: either retiring
# (RETIRING_PATCH_OPERATIONS) or explicitly known-non-retiring ("create",
# "rollback" — see the comment above). Mirrors `schema.PATCH_OPERATIONS`
# exactly; kept as a separate constant here (rather than importing schema.py)
# because this module must not depend on schema.py's validator wiring.
KNOWN_PATCH_OPERATIONS = RETIRING_PATCH_OPERATIONS | {"create", "rollback"}


class UnknownPatchOperationError(ValueError):
    """Raised by `effective_view()` when a patch's `operation` is not in
    `KNOWN_PATCH_OPERATIONS` (docs/CCLG_CONTAINER.md §3.1.1 fail-closed load
    semantics).

    An operation this reader doesn't recognize as retiring-or-not is not safe
    to silently treat as non-retiring: a future container format version (or
    a hand-edited/corrupted patch record) could carry an operation that
    *should* retire its target, and computing an effective view that quietly
    ignores it would resurrect a memory that was supposed to be superseded,
    expired, forgotten, or deprecated. Fail-closed here, not fail-open —
    the caller must upgrade this reader or reject the container, not guess.
    """


def apply_patch(store: CCLGStore, patch: MemoryPatch) -> list[MemoryNode]:
    """Apply a memory patch and return nodes written by the operation."""
    with _patch_lock(store):
        return _apply_patch_locked(store, patch)


def _apply_patch_locked(store: CCLGStore, patch: MemoryPatch) -> list[MemoryNode]:
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

    # Create-then-retire: persist the replacement (and the patch record, which
    # also carries new_content) BEFORE retiring the targets. A crash or a
    # concurrent reader between the two writes then sees a harmless transient
    # duplicate (old + new both present, collapsed by scope/key precedence)
    # instead of a target retired with no replacement on disk — which would lose
    # the fact permanently and leave a dangling superseded_by.
    patch.new_node_ids = [new_node.id]
    patch.applied_at = now_iso()
    store.write_node(new_node)
    store.write_patch(patch)

    old_status = "superseded" if patch.operation in SUPERSEDING_OPERATIONS else "active"
    for node in targets:
        node.status = old_status
        if old_status == "superseded":
            node.relations.setdefault("superseded_by", []).append(new_node.id)
        node.updated_at = now_iso()
        store.write_node(node)
        written.append(node)

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

    Fail-closed on an unrecognized operation: when ``patches`` is given, every
    patch's `operation` MUST be in ``KNOWN_PATCH_OPERATIONS`` (retiring or
    explicitly known-non-retiring) or this raises
    ``UnknownPatchOperationError``. This function is the single place that
    decision is enforced (not `MemoryPatch.from_dict`, which stays a plain,
    non-raising constructor so `schema.py`'s validators can keep aggregating
    *every* problem in a container in one pass rather than failing fast on the
    first bad record — see `container.load_container`). Silently treating an
    unknown operation as non-retiring here would be exactly the failure mode
    this whole gate exists to prevent: a future container-format version (or a
    hand-edited/corrupted patch record) naming an operation this reader has
    never heard of could be one that *should* retire its target, and
    computing an effective view that quietly ignores it would resurrect a
    memory that was supposed to have been superseded, expired, forgotten, or
    deprecated.
    """
    excluded_ids: set[str] = set()
    if patches:
        for patch in patches:
            if patch.operation not in KNOWN_PATCH_OPERATIONS:
                raise UnknownPatchOperationError(
                    f"patch {patch.id!r} has unrecognized operation {patch.operation!r}; "
                    f"refusing to compute effective view (docs/CCLG_CONTAINER.md §3.1.1 "
                    f"fail-closed load semantics) — known operations: {sorted(KNOWN_PATCH_OPERATIONS)}"
                )
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

# Explicit, unambiguous correction language -- a speaker directly saying a
# prior statement was wrong. Strong enough on its own to retire a target on
# ordinary lexical (BM25) overlap; no further gating applied.
#
# Round-2 fix (계열C, "아니" hedge false positive): bare "아니" was removed
# from this list -- see WEAK_NEGATION_TRIGGERS below. "아니라" is kept as its
# own entry because "A가 아니라 B" is a structural correction (부정 뒤 새
# 값/명제가 반드시 이어지는 접속형), unlike a bare sentence-final
# "아니야/아니지/아닌데/아니고" hedge, which says nothing about whether a
# replacement value follows. "정확히 말하면" is added alongside the existing
# "다시 말하면" as the same rephrasing idiom; both are also carved out of
# `_looks_like_conditional`'s mid-clause "-면" pattern (see below) since they
# end in "면" but are not conditionals.
CORRECTION_TRIGGERS = [
    "그게 아니라",
    "아니라",
    "정확히는",
    "정확히 말하면",
    "수정",
    "정정",
    "잘못 말했",
    "잘못 말씀",
    "다시 말하면",
    "actually",
    "correction",
    "not quite",
    "i meant",
]

# Forward-looking change-of-policy language ("from now on", "더 이상"/"no
# longer") names an explicit before/after transition, not just "now" in the
# temporal-filler sense -- strong enough to retire a target on lexical
# overlap alone, same as CORRECTION_TRIGGERS.
STRONG_TEMPORAL_TRIGGERS = ["앞으로", "더 이상", "더이상", "바꿔", "from now on", "no longer"]

# Bare temporal adverbs ("이제"="now"/"from here on", "지금"="right now") are
# common in ordinary scheduling/logistics chatter and, alone, say nothing
# about whether any specific existing memory is being contradicted -- real
# corpus false positive: "이제 회의는 다른 걸로 잡자" (not a correction) only
# shares generic-noun BM25 overlap ('회의는') with an unrelated meeting-time
# memory and used to retire it outright. detect_patch_candidates() only
# promotes a *weak-only* turn (no other trigger family fires -- see
# `_is_weak_signal_only`) to a candidate when it also names a concrete value
# that *contradicts* (not just repeats) a value already in the specific
# target node (see `_has_value_contradiction`), and never for a turn that
# reads as a question (see `_looks_like_question`) or a conditional/
# hypothetical clause (see `_looks_like_conditional`). Repro pinned in
# agent-hub's tests/test_cclg_grounding.py
# ``test_apply_corrections_false_positive_neutral_temporal_utterance``.
WEAK_TEMPORAL_TRIGGERS = ["이제", "지금"]

# Round-2 addition (계열C): a bare sentence-final negation hedge --
# "아니야/아니지/아닌데/아니고" -- is, by itself, just as weak a signal as a
# bare temporal adverb: "이번 예산은 확정된 게 아니야, 아직 논의 중이야" and
# "이제 5억 정도는 우리한테 큰돈도 아니지" both hedge without asserting any
# replacement fact. Held to the same `_has_value_contradiction` gate as
# WEAK_TEMPORAL_TRIGGERS below. The structurally different "A가 아니라 B"
# construction (그 자체로 새 값이 뒤따르는 접속형) stays a strong,
# ungated CORRECTION_TRIGGERS entry ("아니라") -- see the comment above
# CORRECTION_TRIGGERS.
WEAK_NEGATION_TRIGGERS = ["아니야", "아니지", "아닌데", "아니고"]

TEMPORAL_TRIGGERS = STRONG_TEMPORAL_TRIGGERS + WEAK_TEMPORAL_TRIGGERS + ["폐기", "deprecate"]
SCOPE_TRIGGERS = ["이번 프로젝트", "이 repo", "이 레포", "global로", "local만", "this repo", "this project", "globally", "only local"]
EXPANSION_TRIGGERS = ["도 되어야", "다 지원", "지원해야", "도 지원", "포함해야", "also support", "must include", "as well"]
NEGATION_TRIGGERS = ["하지 마", "하지마", "쓰지 마", "쓰지마", "말고", "금지", "제외", "do not", "don't", "must not", "exclude"]

# Operations classify_patch()/detect_patch_candidates() can return that retire
# a target rather than merely annotate it -- mirrors the family agent-hub's
# ``cclg_grounding._RETIRING_OPS`` filters candidates by (that module lives in
# schift/services/agent-hub and is not owned here; kept in sync by hand).
# classify_patch() never returns "forget", but the name is included for
# parity with the consumer-side set.
RETIRING_CANDIDATE_OPERATIONS = {"supersede", "update", "narrow", "deprecate", "forget"}


def _contains(text: str, triggers: list[str]) -> str | None:
    lowered = text.lower()
    for trigger in triggers:
        if trigger.lower() in lowered:
            return trigger
    return None


# Round-3 fix (기계적 버그 1, "아니라" lexeme substring bug): the CORRECTION_
# TRIGGERS entries "아니라"/"그게 아니라" were matched by plain substring via
# `_contains`, which wrongly fires inside two completely different Hangul
# lexemes fused directly onto "아니라" with no word boundary:
#   - "아니라면"/"아니라며"/"아니라니"/"아니라면서" -- a conditional/concessive
#     connective ("if it's not X" / "not only X but"), not a correction at
#     all. Worse, because the substring match made
#     `_contains(text, CORRECTION_TRIGGERS)` truthy, the exception carve-out
#     in `detect_patch_candidates` (`_looks_like_conditional(text) and not
#     _contains(text, CORRECTION_TRIGGERS)`) was self-defeating: a bare
#     conditional like "회의가 3시가 아니라면 그냥 진행하자" satisfied its own
#     "has an explicit correction idiom" exception and sailed straight past
#     the conditional gate.
#   - "아니라고" -- the reported-speech particle ("A라고 하다" = "said that
#     A"), not a correction; see `_looks_like_reported_speech` below for the
#     broader third-party-attribution suppression this feeds.
# The true "A가 아니라 B" correction construction requires "아니라" to end its
# own word (followed by whitespace/punctuation/end-of-string), so both
# entries route through this boundary-aware check instead of plain
# substring matching; every other CORRECTION_TRIGGERS entry is unaffected
# and keeps using plain `_contains`.
_ANIRA_TRIGGERS = ("그게 아니라", "아니라")
_ANIRA_EXCLUDED_SUFFIX_RE = re.compile(r"아니라(?:면서|면|며|니|고)")
_NON_ANIRA_CORRECTION_TRIGGERS = [t for t in CORRECTION_TRIGGERS if t not in _ANIRA_TRIGGERS]


def _true_anira_correction(text: str) -> str | None:
    """Boundary-aware replacement for substring-matching "그게 아니라"/
    "아니라" against ``text``. Every fused (non-boundary) occurrence --
    "아니라면"/"아니라며"/"아니라니"/"아니라면서"/"아니라고" -- is stripped out
    first; only a surviving bare "아니라"/"그게 아니라" counts as a hit."""
    stripped = _ANIRA_EXCLUDED_SUFFIX_RE.sub("", text)
    for trigger in _ANIRA_TRIGGERS:
        if trigger in stripped:
            return trigger
    return None


def _contains_correction_trigger(text: str) -> str | None:
    """Lexeme-aware drop-in for ``_contains(text, CORRECTION_TRIGGERS)`` --
    used everywhere that used to call the latter directly (``classify_patch``,
    ``_is_weak_signal_only``, and both CORRECTION_TRIGGERS checks in
    ``detect_patch_candidates``). Every entry behaves exactly as plain
    substring matching except "아니라"/"그게 아니라", which route through
    ``_true_anira_correction`` (see above) instead."""
    hit = _contains(text, _NON_ANIRA_CORRECTION_TRIGGERS)
    if hit:
        return hit
    return _true_anira_correction(text)


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
    if _contains_correction_trigger(text):
        return "supersede"
    if _contains(text, WEAK_NEGATION_TRIGGERS):
        return "supersede"
    if _contains(text, TEMPORAL_TRIGGERS):
        return "update"
    return None


def _is_weak_signal_only(text: str) -> bool:
    """True when the only trigger present is a bare temporal adverb (이제/지금)
    and/or a bare negation hedge (아니야/아니지/아닌데/아니고), with no
    explicit correction, forward-looking temporal, expansion, negation, or
    scope signal. Such a turn needs a concrete target match (see
    ``_has_value_contradiction``) before it is promoted to a candidate."""
    if _contains(text, ["폐기", "deprecate"]):
        return False
    if _contains(text, EXPANSION_TRIGGERS):
        return False
    if _contains(text, NEGATION_TRIGGERS):
        return False
    if _contains(text, SCOPE_TRIGGERS):
        return False
    if _contains_correction_trigger(text):
        return False
    if _contains(text, STRONG_TEMPORAL_TRIGGERS):
        return False
    return bool(_contains(text, WEAK_TEMPORAL_TRIGGERS) or _contains(text, WEAK_NEGATION_TRIGGERS))


# A "concrete value" span: a number glued to a short quantity/unit suffix
# ("5억", "12억원", "3시", "1200만"), an ISO date, a quoted phrase (straight or
# curly quotes), an English proper-noun-ish capitalized word ("Hermes"), or a
# code-like token with an internal ``_``/``-`` ("mp_stg_4R2N"). Generic common
# nouns ("회의"/"일정"/"매출" alone) never match -- see module docstring above
# for why bare-noun overlap is not sufficient signal for a weak-only turn.
_VALUE_TOKEN_RE = re.compile(
    r"""
    \d[\d,\.]*(?:억|만|천|백만|원|명|개|시|분|초|월|일|년|%|퍼센트|건|회|차|층|호|번|위|점|살)
    |\b\d{4}-\d{2}-\d{2}\b
    |"[^"]+"|'[^']+'|“[^”]+”|‘[^’]+’
    |\b[A-Z][A-Za-z0-9]{2,}\b
    |\b[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+\b
    """,
    re.VERBOSE,
)

# Round-2 fix (계열B, "value coincidence" false positive): coarse comparison
# buckets for a numeric+unit value token, keyed by the unit suffix -- e.g.
# "3시"/"4시" both bucket to "time", "5억"/"12억" both bucket to "money".
# `_has_value_contradiction` only treats two values as a genuine replacement
# (not mere coincidental repetition -- "이제 3시 다 됐네, 슬슬 준비하자"
# against a "회의는 내일 3시다" memory shares the *same* 3시, not a
# contradiction) when they land in the same bucket but differ.
_UNIT_CATEGORY = {
    "시": "time",
    "분": "time",
    "초": "time",
    "억": "money",
    "만": "money",
    "천": "money",
    "백만": "money",
    "원": "money",
    "월": "date",
    "일": "date",
    "년": "date",
    "%": "count",
    "퍼센트": "count",
    "건": "count",
    "회": "count",
    "차": "count",
    "층": "count",
    "호": "count",
    "번": "count",
    "위": "count",
    "점": "count",
    "살": "count",
    "명": "count",
    "개": "count",
}

_NUMERIC_VALUE_RE = re.compile(r"^([\d,\.]+)(.+)$")

_QUESTION_ENDING_RE = re.compile(r"(습니까|ㅂ니까|나요|ㄹ까요|을까요)\s*$")


def _value_category(raw_token: str) -> str:
    """Coarse comparison bucket for one raw ``_VALUE_TOKEN_RE`` match (before
    quote-stripping/lowercasing) -- see the comment above ``_UNIT_CATEGORY``.
    ISO dates get their own "date" bucket (shared with 월/일/년 tokens);
    quoted spans, code-like tokens, and capitalized proper nouns each get a
    distinct bucket so e.g. a quoted string can never "contradict" a number.
    """
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_token):
        return "date"
    numeric = _NUMERIC_VALUE_RE.match(raw_token)
    if numeric:
        unit = numeric.group(2)
        return _UNIT_CATEGORY.get(unit, f"unit:{unit}")
    if raw_token[:1] in "\"'“‘":
        return "quote"
    if re.match(r"^[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+$", raw_token):
        return "code"
    if re.match(r"^[A-Z][A-Za-z0-9]{2,}$", raw_token):
        return "proper_noun"
    return "other"


# Round-3 fix (기계적 버그 4, numeric normalization): "money"-category units
# (억/만/천/백만) compound with a leading digit run into a genuine numeral
# multiplier, so "5억"/"500,000,000원" name the *same* amount despite sharing
# no literal substring. Deliberately scoped to money only -- "time" (시/분/초)
# shares one category bucket but has no multiplier relationship between its
# units, and the spec calls for "N시==N:00 수준만" (no clock-format
# canonicalization), so every other category keeps the exact
# quote-stripped/lowercased raw token, unchanged from round 2.
_MONEY_MULTIPLIER = {"억": 100_000_000, "만": 10_000, "천": 1_000, "백만": 1_000_000}


def _canonical_value(category: str, raw_token: str) -> str:
    """Comparison key for one raw ``_VALUE_TOKEN_RE`` match, given its
    ``_value_category``. For "money", strips thousands commas and expands a
    Korean numeral multiplier into its integer value so "5억"/"5억원"/
    "500,000,000원" all canonicalize to the same "500000000" key -- see the
    module comment above ``_MONEY_MULTIPLIER``. Every other category is
    unchanged: quote-stripped and lowercased, exactly as round 2 produced it.
    """
    normalized = raw_token.strip("\"'“”‘’").lower()
    if category != "money":
        return normalized
    numeric = _NUMERIC_VALUE_RE.match(raw_token)
    if not numeric:
        return normalized
    digits_part, unit = numeric.groups()
    digits_part = digits_part.replace(",", "")
    try:
        base: float = int(digits_part) if digits_part.isdigit() else float(digits_part)
    except ValueError:
        return normalized
    multiplier = _MONEY_MULTIPLIER.get(unit)
    if multiplier is not None:
        base = base * multiplier
    if isinstance(base, float) and base.is_integer():
        base = int(base)
    return str(base)


def _value_entries(text: str) -> list[tuple[str, str]]:
    """Every concrete value span in ``text`` as ``(category, canonical_value)``
    pairs -- category from the raw match (``_value_category``), value
    canonicalized for equality comparison (``_canonical_value``, round 3)."""
    entries: list[tuple[str, str]] = []
    for raw_token in _VALUE_TOKEN_RE.findall(text):
        category = _value_category(raw_token)
        normalized = _canonical_value(category, raw_token)
        if not normalized:
            continue
        entries.append((category, normalized))
    return entries


def _has_value_contradiction(text: str, node_content: str) -> bool:
    """Whether ``text`` asserts a single, unambiguous replacement value for a
    concrete value already present in ``node_content``.

    For every value category the turn and the node share, first collect
    every turn value in that category that *differs* from all of the node's
    values in that category. The turn contradicts the node only if some
    category ends up with **exactly one** such differing value.

    Round-3 나열 가드 (기계적 버그 3): a correction turn asserts exactly one
    replacement value. When 2+ distinct differing values land in the same
    category ("이제 예산 후보가 1200만원, 1500만원, 1800만원 중에 정해야해" --
    three money candidates, none singled out as "the" new figure), the turn
    is enumerating options rather than asserting a specific replacement, so
    none of them counts as a contradiction, regardless of which node is
    compared against.

    This is also the round-2 replacement for the plain "any shared token"
    overlap check: a weak-only turn that merely *repeats* a value already in
    the target ("이제 3시 다 됐네, 슬슬 준비하자" / "지금 3시밖에 안 됐는데
    벌써 배고프다" against a "회의는 내일 3시다" memory) contributes zero
    differing values and is not a contradiction -- only a turn offering a
    genuinely different value in the same category (as in "이제 3시 회의를
    4시로 옮기자", where "3시" matches the node and only "4시" differs) does.
    A turn naming no concrete value at all, or a category the node has no
    value in, never contradicts.
    """
    turn_entries = _value_entries(text)
    if not turn_entries:
        return False
    node_values_by_category: dict[str, set[str]] = {}
    for category, value in _value_entries(node_content):
        node_values_by_category.setdefault(category, set()).add(value)
    differing_by_category: dict[str, set[str]] = {}
    for category, value in turn_entries:
        existing = node_values_by_category.get(category)
        if existing and value not in existing:
            differing_by_category.setdefault(category, set()).add(value)
    return any(len(values) == 1 for values in differing_by_category.values())


def _looks_like_question(text: str) -> bool:
    """Deterministic, punctuation/ending-based question detector -- a weak
    temporal turn that is actually a question ("지금 3시인데 회의 언제
    시작해?") is not an assertion of a changed fact even if it happens to
    restate a concrete value shared with an existing memory."""
    stripped = text.strip()
    if "?" in stripped:
        return True
    return bool(_QUESTION_ENDING_RE.search(stripped))


# Round-2 fix (계열A, conditional/hypothetical false positive): "만약 이제
# 매출가 5억을 넘으면 다시 얘기하자" names a real value ("5억") but only as
# the threshold of a hypothetical, not an assertion that the fact has
# changed. "다시 말하면"/"정확히 말하면" are carved out of the mid-clause
# "-면" pattern below because they are rephrasing idioms (already strong
# CORRECTION_TRIGGERS entries), not conditionals, despite ending in "면".
_CONDITIONAL_MARKERS = ["만약", "혹시"]
_CONDITIONAL_IDIOM_EXCEPTIONS = ("다시 말하면", "정확히 말하면")
_MID_CLAUSE_CONDITIONAL_RE = re.compile(r"[가-힣]면(?=[\s,.]|$)")
_ENGLISH_CONDITIONAL_RE = re.compile(r"\bif\b|\bin case\b|\bunless\b")


def _looks_like_conditional(text: str) -> bool:
    """True when ``text`` reads as a conditional/hypothetical clause rather
    than an assertion: "만약"/"혹시", a mid-clause Korean conditional
    connective ("~(하)면 "), or English "if"/"in case"/"unless". A turn
    naming a value only as a hypothetical threshold or trigger condition
    ("만약 매출가 5억을 넘으면 다시 얘기하자") is not asserting that a memory
    is currently wrong, even if it shares or contradicts a concrete value
    with a target node -- see ``detect_patch_candidates``, which suppresses
    *all* retiring candidates for a conditional turn unless it also carries
    an explicit correction idiom (CORRECTION_TRIGGERS)."""
    working = text
    for idiom in _CONDITIONAL_IDIOM_EXCEPTIONS:
        working = working.replace(idiom, "")
    if _contains(working, _CONDITIONAL_MARKERS):
        return True
    if _ENGLISH_CONDITIONAL_RE.search(working.lower()):
        return True
    return bool(_MID_CLAUSE_CONDITIONAL_RE.search(working))


# Round-3 fix (기계적 버그 2, 전달화법 억제): a turn that attributes a
# statement to a third party -- "(이)라고 했/그러던데/하더라/전했" -- reports
# what someone else said; it is not the speaker's own assertion that a
# memory is wrong, even when it happens to also trip a weak temporal adverb
# or bare negation hedge independently of the "아니라고" lexeme carved out of
# CORRECTION_TRIGGERS above (e.g. "철수가 예산은 1200만원 아니고
# 1500만원이라고 하더라" trips WEAK_NEGATION_TRIGGERS' "아니고" and would
# otherwise clear ``_has_value_contradiction`` on the differing "1500만"
# figure). An explicit first-person correction idiom (CORRECTION_TRIGGERS,
# e.g. "잘못 말했") always overrides this suppression -- "아까 내가
# 5억이라고 했는데 잘못 말했어" IS a correction -- but in practice that case
# never even reaches this function: ``_is_weak_signal_only`` already returns
# False the moment CORRECTION_TRIGGERS matches, so ``weak_only`` (the only
# gate this suppression augments in ``detect_patch_candidates``) is never
# True for it. The explicit check here is a defensive belt-and-suspenders
# for any future caller that doesn't already imply that guarantee.
_REPORTED_SPEECH_RE = re.compile(r"(?:이)?라고\s*(?:했|그러던데|하더라|전했)")


def _looks_like_reported_speech(text: str) -> bool:
    """True when ``text`` attributes a statement to a third party via a
    reported-speech marker, unless an explicit first-person correction idiom
    is also present (see module comment above)."""
    if _contains_correction_trigger(text):
        return False
    return bool(_REPORTED_SPEECH_RE.search(text))


# Round-2 recall backstop: two lexical-mismatch shapes ``search_nodes``'s
# plain BM25-over-whitespace/Hangul-run tokenizer (``retrieval.TOKEN_RE``)
# structurally can't bridge, because the two sides never share a single
# token. Both are patches.py-only (retrieval.py is shared/common code and is
# not modified here).
#
# (1) A Korean reported-speech particle ("라고"/"이라고") glued directly onto
#     the value token: "이철수라고" is one indivisible Hangul-run token to
#     ``retrieval.TOKEN_RE``, sharing nothing with a node's "이철수이다".
# (2) "N월 N일" phrasing vs. an ISO "YYYY-MM-DD" date: tokenized entirely
#     differently ("7", "월", "3", "일" vs. a single "2026-07-03" token).
#
# `_supplemental_hits` only ever *adds* nodes ``search_nodes`` missed
# entirely -- it never reorders or removes ``search_nodes``'s own hits.
_REPORTED_VALUE_RE = re.compile(r"(?<![0-9])([가-힣A-Za-z][가-힣A-Za-z0-9]{1,})(?:이)?라고\b")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")
_ISO_DATE_ANYWHERE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _reported_value_tokens(text: str) -> set[str]:
    """Bare values a turn attributes to a prior statement via 라고/이라고
    (e.g. "이철수라고" -> "이철수"). The negative lookbehind keeps this from
    firing on a "N일이라고" date suffix (see `_date_key_variants` instead)."""
    return {match.group(1) for match in _REPORTED_VALUE_RE.finditer(text) if len(match.group(1)) >= 2}


def _date_key_variants(text: str) -> set[str]:
    """Normalized month-day (and year-month-day) keys for every date-like
    span in ``text`` -- bridges "N월 N일" phrasing and ISO ``YYYY-MM-DD``
    onto the same comparable key regardless of which style a given turn or
    node uses."""
    keys: set[str] = set()
    for month, day in _MONTH_DAY_RE.findall(text):
        keys.add(f"{int(month):02d}-{int(day):02d}")
    for year, month, day in _ISO_DATE_ANYWHERE_RE.findall(text):
        keys.add(f"{int(month):02d}-{int(day):02d}")
        keys.add(f"{year}-{int(month):02d}-{int(day):02d}")
    return keys


def _supplemental_hits(text: str, nodes: list[MemoryNode], exclude_ids: set[str]) -> list[SearchHit]:
    """Recall backstop for the two lexical-mismatch shapes documented above.
    Only ever adds nodes ``search_nodes`` missed entirely (``exclude_ids`` is
    the id set of ``search_nodes``'s own hits)."""
    reported = _reported_value_tokens(text)
    turn_dates = _date_key_variants(text)
    if not reported and not turn_dates:
        return []
    extra: list[SearchHit] = []
    for node in nodes:
        if node.id in exclude_ids:
            continue
        name_match = any(token in node.content for token in reported)
        date_match = bool(turn_dates and turn_dates & _date_key_variants(node.content))
        if name_match or date_match:
            extra.append(SearchHit(node=node, score=0.0, reasons=["supplemental"]))
    return extra


def detect_patch_candidates(text: str, nodes: list[MemoryNode], *, limit: int = 3) -> list[dict]:
    """Detect candidate mutations from a raw user turn against the effective view.

    Returns candidate dicts ``{operation, target_id, reason, trigger, score}``.
    Deterministic and embedding-independent; the caller decides whether to apply.

    A conditional/hypothetical turn (``_looks_like_conditional``) never
    produces a candidate at all, unless it also carries an explicit
    correction idiom (CORRECTION_TRIGGERS) -- e.g. "만약 이제 매출가 5억을
    넘으면 다시 얘기하자" names a real value only as a hypothetical
    threshold, not an assertion that a memory is wrong.

    A turn whose *only* trigger is a bare temporal adverb (이제/지금) or a
    bare negation hedge (아니야/아니지/아닌데/아니고, see
    ``_is_weak_signal_only``) is additionally required to name a concrete
    value that *contradicts* -- not merely repeats, and not as one of 2+
    candidates in an enumeration (round 3, ``_has_value_contradiction``) --
    a value already in the specific target node, and to not read as a
    question (``_looks_like_question``) or a third-party report
    (``_looks_like_reported_speech``, round 3), before it is returned as a
    candidate at all. This closes several false-positive families this
    function used to produce: a plain scheduling remark like "이제 회의는
    다른 걸로 잡자" against an unrelated meeting-time memory, a coincidental
    value restatement like "이제 3시 다 됐네, 슬슬 준비하자" against a
    "회의는 내일 3시다" memory (round 2), a candidate list like "이제 예산
    후보가 1200만원, 1500만원, 1800만원 중에 정해야해" (round 3), and a
    third-party attribution like "김대리가 회의는 3시가 아니라고 그러던데"
    (round 3).

    The CORRECTION_TRIGGERS "아니라"/"그게 아니라" entries route through the
    lexeme-aware ``_contains_correction_trigger`` (round 3) instead of plain
    substring matching, so they no longer fire inside the fused conditional/
    concessive ("아니라면"/"아니라며"/"아니라니"/"아니라면서") or
    reported-speech ("아니라고") lexemes -- see that function's docstring.
    """
    operation = classify_patch(text)
    if operation is None:
        return []
    if _looks_like_conditional(text) and not _contains_correction_trigger(text):
        return []
    trigger = (
        _contains(text, EXPANSION_TRIGGERS)
        or _contains(text, NEGATION_TRIGGERS)
        or _contains(text, SCOPE_TRIGGERS)
        or _contains_correction_trigger(text)
        or _contains(text, WEAK_NEGATION_TRIGGERS)
        or _contains(text, TEMPORAL_TRIGGERS)
        or ""
    )
    weak_only = _is_weak_signal_only(text)
    is_question = weak_only and _looks_like_question(text)
    reported_speech = weak_only and _looks_like_reported_speech(text)
    # Strip the trigger phrasing so retrieval matches the referenced fact, not the cue word.
    cleaned = re.sub(r"\s+", " ", text).strip()
    hits = search_nodes(cleaned, nodes, limit=limit)
    hits = hits + _supplemental_hits(text, nodes, {hit.node.id for hit in hits})
    candidates: list[dict] = []
    for hit in hits:
        if weak_only and (is_question or reported_speech or not _has_value_contradiction(text, hit.node.content)):
            continue
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
