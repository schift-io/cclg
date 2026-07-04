"""GateMem Office checkpoint side: policy-aware ActiveMemoryPack -> prediction
row (P4 Mode 1). Companion to ``cclg.gatemem_adapter``, which handles the
turn -> node/patch ingestion side; split out to keep both files under the
repo's 500-line god-file guideline. See that module's docstring for the full
Mode 1 boundary and documented limitations.
"""

from __future__ import annotations

import contextlib
import itertools
import re
from dataclasses import dataclass
from typing import Any, Iterator

from . import models as _cclg_models
from .container import ContainerBundle
from .gatemem_adapter import EpisodePolicyState, node_project_ids, node_speaker
from .gatemem_lexical import extract_proper_noun_pairs, search_nodes_normalized
from .patches import effective_view as _effective_view
from .retrieval import SearchHit

# Fields the agent (and thus this module's decision logic) is allowed to see
# per docs/GATEMEM_OFFICE.md's "Hidden Field Rule". query_type/expected_action/
# judge_spec/leak_targets/attack_type are evaluator-only and must never reach
# the functions below that decide `action` or build exposed context text.
AGENT_CHECKPOINT_FIELDS = ("checkpoint_id", "episode_id", "as_of_turn_id", "asker", "query_text")
HIDDEN_CHECKPOINT_FIELDS = frozenset({"query_type", "expected_action", "judge_spec", "leak_targets", "attack_type"})


@dataclass(frozen=True, slots=True)
class AgentCheckpointView:
    """Exactly the fields docs/GATEMEM_OFFICE.md's Hidden Field Rule allows
    the agent to see. Every decision function below takes this, never the
    raw checkpoint dict, so hidden-field leakage into the decision path is a
    type error, not just a convention."""

    checkpoint_id: str
    episode_id: str
    as_of_turn_id: str
    asker_principal_id: str
    asker_role: str
    query_text: str


def agent_view_of(checkpoint: dict[str, Any]) -> AgentCheckpointView:
    asker = checkpoint.get("asker") or {}
    return AgentCheckpointView(
        checkpoint_id=str(checkpoint.get("checkpoint_id")),
        episode_id=str(checkpoint.get("episode_id")),
        as_of_turn_id=str(checkpoint.get("as_of_turn_id")),
        asker_principal_id=str(asker.get("principal_id") or ""),
        asker_role=str(asker.get("role") or ""),
        query_text=str(checkpoint.get("query_text") or ""),
    )


@dataclass(slots=True)
class PolicyPackResult:
    action: str
    exposed_node_ids: list[str]
    exposed_text: str
    authorized_active_hit_ids: list[str]
    unauthorized_active_hit_ids: list[str]
    nonactive_hit_ids: list[str]


# Statuses that represent a real deletion/forgetting event -- GateMem's
# safety category ("deleted facts should not be recovered, confirmed, or
# reconstructed"). Deliberately excludes "superseded": an ordinary corrected
# value (budget/date/discount updates are the bulk of this benchmark's
# narratives) is not a safety violation to have existed, and effective_view()
# already keeps it out of the *active* pool -- it must not also force a
# blanket refusal for every later question that happens to share vocabulary
# with some past, since-corrected value.
FORGOTTEN_LIKE_STATUSES = frozenset({"forgotten", "deprecated", "expired", "discarded"})

# A query about a project shares that project's own name with *every* node
# tied to it, including a genuinely forgotten one -- e.g. "Project Maple's
# current budget?" scores ~1 against a forgotten "customer is Granite
# Mutual" node purely on "maple"/"current" overlap, versus 30+ against the
# actual current-budget node. Below this floor a hit is incidental vocabulary
# overlap, not evidence the query is *about* that record, so it must not be
# allowed to veto a strong authorized answer (or manufacture a "refuse").
MIN_RELEVANT_SCORE = 4.0


def _above_floor(hits: list[SearchHit], floor: float = MIN_RELEVANT_SCORE) -> list[SearchHit]:
    return [hit for hit in hits if hit.score >= floor]


# CCLG exposes/suppresses whole nodes; it has no notion of redacting a
# substring within one. That is a real gap when a *different*, legitimately
# active/authorized node happens to co-mention a protected value for
# disambiguation ("the names Meridian Freight and Meridian Fieldworks are
# similar enough that we need extra care") -- excluding that node entirely
# would also drop the legitimate fact it carries, but exposing it verbatim
# leaks the other project's protected customer name. This is the one place
# Mode 1 does substring-level work: mask any exact mention, inside an
# otherwise-exposed node, of a proper-noun value that only ever appears in a
# forgotten or unauthorized node -- and mark the action "answer_redacted"
# (matching GateMem's own action vocabulary) so this is visible in the
# prediction row, not silently folded into a plain "answer".
_REDACTION_MARKER = "[REDACTED]"


def _protected_proper_nouns(node_lists: list[list], exclude_terms: frozenset[str]) -> set[str]:
    values: set[str] = set()
    for nodes in node_lists:
        for node in nodes:
            for span in extract_proper_noun_pairs(node.content):
                if span.lower() not in exclude_terms:
                    values.add(span)
    return values


def _redact_protected_values(text: str, protected_terms: set[str]) -> tuple[str, bool]:
    redacted = False
    for term in protected_terms:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        text, count = pattern.subn(_REDACTION_MARKER, text)
        redacted = redacted or count > 0
    return text, redacted


def build_policy_pack(
    bundle: ContainerBundle,
    view: AgentCheckpointView,
    policy_state: EpisodePolicyState,
    *,
    max_nodes: int = 8,
) -> PolicyPackResult:
    """The policy-aware ActiveMemoryPack step: CCLG's ``effective_view`` (drops
    superseded/forgotten/etc.) plus an asker-authorization filter CCLG's
    stock effective view does not do on its own.

    ``bundle`` must come from a container round trip (``pack_container``/
    ``pack_from_store`` -> ``load_container``) per docs/GATEMEM_OFFICE.md's
    P4 Mode 1 definition -- callers must not pass a bare ``store.iter_nodes()``
    list here.

    Priority order is deliberately safety-first: any hit clearing
    ``MIN_RELEVANT_SCORE`` against a genuinely forgotten/deprecated/expired/
    discarded record (see ``FORGOTTEN_LIKE_STATUSES``) wins over everything
    else and yields an empty, no-answer pack -- recovering a deleted fact
    must never be possible, even if the same query also happens to match
    something the asker is authorized to see.
    """
    all_nodes = bundle.memory_nodes()
    active = _effective_view(all_nodes)
    forgotten_like = [node for node in all_nodes if node.status in FORGOTTEN_LIKE_STATUSES]

    active_by_id = {node.id: node for node in active}
    authorized_ids = {node.id for node in active if policy_state.is_authorized(view.asker_principal_id, node_project_ids(node))}
    authorized_active = [active_by_id[node_id] for node_id in authorized_ids]
    unauthorized_active = [node for node in active if node.id not in authorized_ids]

    query = view.query_text
    forgotten_hits = _above_floor(search_nodes_normalized(query, forgotten_like, limit=5))
    authorized_hits = _above_floor(search_nodes_normalized(query, authorized_active, limit=max_nodes))
    unauthorized_hits = _above_floor(search_nodes_normalized(query, unauthorized_active, limit=5))

    if forgotten_hits:
        action, selected = "no_memory", []
    elif authorized_hits:
        action, selected = "answer", [hit.node for hit in authorized_hits]
    elif unauthorized_hits:
        action, selected = "refuse", []
    else:
        action, selected = "no_memory", []

    exposed_lines: list[str] = []
    if selected:
        protected_terms = _protected_proper_nouns([forgotten_like, unauthorized_active], policy_state.directory.project_label_terms)
        any_redacted = False
        for node in selected:
            content, was_redacted = _redact_protected_values(node.content, protected_terms)
            any_redacted = any_redacted or was_redacted
            exposed_lines.append(f"[{node.id}] (speaker={node_speaker(node)}): {content}")
        if any_redacted:
            action = "answer_redacted"
    return PolicyPackResult(
        action=action,
        exposed_node_ids=[node.id for node in selected],
        exposed_text="\n".join(exposed_lines),
        authorized_active_hit_ids=[hit.node.id for hit in authorized_hits],
        unauthorized_active_hit_ids=[hit.node.id for hit in unauthorized_hits],
        nonactive_hit_ids=[hit.node.id for hit in forgotten_hits],
    )


def build_prediction_row(checkpoint: dict[str, Any], view: AgentCheckpointView, pack: PolicyPackResult) -> dict[str, Any]:
    """Shape matches the upstream GateMem harness's own predictions.jsonl row
    (checkpoint_id/episode_id/as_of_turn_id/asker/query_type/attack_type/
    expected_action/query_text/output -- see run_eval.py:run_episode in
    https://github.com/rzhub/GateMem). The outer bookkeeping fields
    (query_type/attack_type/expected_action) are read from ``checkpoint``
    here purely for scorer/report convenience -- ``pack`` was already built
    upstream from ``view`` alone, so they never influenced ``pack.action`` or
    what got exposed.

    ``output.answer``/``answer_structured`` stay empty: this adapter never
    calls an LLM, so it never fabricates prose. The deterministic memory
    context lives only in ``output.memory_audit.prompt_context.text``, the
    field the released rule-based scorer reads for prompt-exposure leakage
    (see ``bench/eval/metrics.py:flatten_prompt_context_text`` upstream).
    """
    output = {
        "action": pack.action,
        "answer": "",
        "answer_structured": {},
        "used_record_ids": list(pack.exposed_node_ids),
        "memory_audit": {
            "schema_version": 1,
            "stage": "prompt_context",
            "prompt_context": {
                "text": pack.exposed_text,
                "n_chars": len(pack.exposed_text),
                "n_items": len(pack.exposed_node_ids),
            },
        },
        "cclg_mode": "cclg_local",
    }
    return {
        "checkpoint_id": view.checkpoint_id,
        "episode_id": view.episode_id,
        "as_of_turn_id": view.as_of_turn_id,
        "asker": {"principal_id": view.asker_principal_id, "role": view.asker_role},
        "query_type": checkpoint.get("query_type"),
        "attack_type": checkpoint.get("attack_type"),
        "expected_action": checkpoint.get("expected_action"),
        "query_text": view.query_text,
        "output": output,
    }


def check_pack_leaks(
    bundle: ContainerBundle,
    view: AgentCheckpointView,
    pack: PolicyPackResult,
    policy_state: EpisodePolicyState,
) -> list[str]:
    """Independent, per-checkpoint invariant check (P4 step 4): every node id
    this checkpoint exposed must be active *and* authorized for the asker.
    A hit here means the adapter's own filter has a bug -- ``build_policy_pack``
    is supposed to make this structurally impossible."""
    violations: list[str] = []
    nodes_by_id = {node.id: node for node in bundle.memory_nodes()}
    for node_id in pack.exposed_node_ids:
        node = nodes_by_id.get(node_id)
        if node is None:
            violations.append(f"{view.checkpoint_id}: exposed unknown node {node_id}")
            continue
        if node.status != "active":
            violations.append(f"{view.checkpoint_id}: exposed non-active node {node_id} (status={node.status})")
        if not policy_state.is_authorized(view.asker_principal_id, node_project_ids(node)):
            violations.append(f"{view.checkpoint_id}: exposed node {node_id} unauthorized for asker {view.asker_principal_id}")
    return violations


@contextlib.contextmanager
def deterministic_ids(width: int = 10) -> Iterator[None]:
    """Replace ``cclg.models.new_id``'s uuid4-based ids with a monotonic
    counter for the duration of the block, so two runs over the same input
    produce byte-identical predictions.jsonl (including used_record_ids).
    Restores the original generator on exit -- this must not leak into other
    tests/processes sharing the same interpreter."""
    counter = itertools.count(1)
    original = _cclg_models.new_id

    def _next_id(prefix: str) -> str:
        return f"{prefix}_{next(counter):0{width}d}"

    _cclg_models.new_id = _next_id
    try:
        yield
    finally:
        _cclg_models.new_id = original
