"""GateMem Office -> CCLG local adapter (P4 Mode 1, see docs/GATEMEM_OFFICE.md).

Deterministic, LLM-free translation between the GateMem office benchmark and
the CCLG memory kernel:

    GateMem turn      -> raw evidence + session event + MemoryNode/MemoryPatch
    GateMem checkpoint -> asker-scoped, policy-aware ActiveMemoryPack -> a
                          predictions.jsonl row (see run_eval.py's schema in
                          the upstream https://github.com/rzhub/GateMem repo)

Mode 1 boundary (docs/GATEMEM_OFFICE.md): no Schift auth, no hosted bucket,
no network after the dataset download, temporary CCLG store only. The goal is
to prove the CCLG kernel (schema, provenance, patch semantics, effective
view, pack filtering) can represent governed shared memory and never lets a
protected, stale, or forgotten node reach the prompt context -- not to win
the benchmark's utility score, which needs a real answer-generating LLM that
this adapter deliberately does not call.

Documented, deliberate limitations (not adapter bugs -- see TODO.md P4):

- Turn -> node/patch classification is a lexical heuristic (delete/revoke
  language plus ``cclg.patches.classify_patch``'s existing supersede-family
  triggers), not a semantic parser. It is biased toward suppression over
  disclosure: a false-positive "forget"/"supersede" only costs recall, it
  never exposes anything.
- Access control is derived once per episode from ``entities.relationships``
  (project_member/budget_owner/legal_reviewer/executive_sponsor/
  contractor_for_project -> authorized for that project_id), plus a
  best-effort in-turn revocation detector. There is no re-grant, and
  role-only overreach by a principal who *is* already on the project is out
  of scope for Mode 1 -- that needs free-text policy comprehension, i.e. an
  LLM, which the CCLG-local boundary forbids.
- The adapter never generates a natural-language answer. ``output.answer``
  stays empty; the deterministic memory context goes in
  ``output.memory_audit.prompt_context.text`` only, exactly the field the
  released scorer treats as prompt-exposure evidence (see
  ``bench/eval/metrics.py:flatten_prompt_context_text`` upstream). Any real
  ``utility_accuracy`` therefore requires Mode 2/3 (or an LLM judge), which
  is out of Mode 1's scope by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .gatemem_lexical import confident_top_hit, find_exact_value_target, matches_any, normalize_content, search_nodes_normalized
from .models import MemoryNode, MemoryPatch
from .patches import apply_patch, classify_patch
from .session import append_session_event
from .store import CCLGStore

# Agent-facing checkpoint field names and the checkpoint-side pack/prediction
# pipeline (AgentCheckpointView, build_policy_pack, build_prediction_row,
# check_pack_leaks, deterministic_ids) live in ``cclg.gatemem_pack``; the
# generic BM25/lexical-matching utilities (search_nodes_normalized,
# confident_top_hit, find_exact_value_target) live in ``cclg.gatemem_lexical``
# -- this module only handles the turn -> node/patch ingestion side.


# --- Deletion / revocation language (domain heuristics; GateMem-agnostic,
# not derived from any per-checkpoint hidden field) --------------------------

DELETE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bdelete[sd]?\b",
        r"\bdo not retain\b",
        r"\bshould not be recovered\b",
        r"\bremains? unavailable\b",
        r"\bretired\b",
        r"\bforgotten\b",
    ]
]

SUPERSEDE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bsupersede[sd]?\b",
        r"\breplaces the earlier\b",
        r"\bthat replaces\b",
    ]
]

REVOKE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\brevok(?:e|ed|ing)\b",
    ]
]


def classify_turn(text: str) -> str:
    """Classify a GateMem turn into ``"forget" | "supersede" | "create"``.

    Checked in priority order so a turn that both deletes *and* restates
    (e.g. "delete the old token, the new one is now supersedes...") is never
    treated as a fresh disclosure of the thing being deleted.
    """
    if matches_any(text, DELETE_PATTERNS):
        return "forget"
    if matches_any(text, SUPERSEDE_PATTERNS):
        return "supersede"
    operation = classify_patch(text)
    if operation is None:
        return "create"
    return "forget" if operation == "deprecate" else "supersede"


# --- Project directory + episode-scoped access policy -----------------------


@dataclass(slots=True)
class ProjectDirectory:
    """Deterministic ACL surface derived once from an episode's entity graph."""

    project_ids: list[str]
    aliases: dict[str, str]
    principals_by_project: dict[str, set[str]]
    display_names: dict[str, str]
    all_principal_ids: set[str]
    # Lower-cased "<alias>" and "project <alias>" forms, for
    # gatemem_lexical.find_exact_value_target's exclude_terms: a project's
    # own name is not a distinguishing "value span" (see that function).
    project_label_terms: frozenset[str] = frozenset()


def build_project_directory(episode: dict[str, Any]) -> ProjectDirectory:
    entities = episode.get("entities") or {}
    principals = entities.get("principals") or []
    relationships = entities.get("relationships") or []

    display_names: dict[str, str] = {}
    all_ids: set[str] = set()
    for principal in principals:
        principal_id = str(principal.get("principal_id") or "")
        if not principal_id:
            continue
        all_ids.add(principal_id)
        display_names[principal_id] = str(principal.get("display_name") or "")

    project_ids = sorted({str(rel.get("project_id")) for rel in relationships if rel.get("project_id")})
    aliases = {project_id: (project_id.split("_", 1)[1] if "_" in project_id else project_id) for project_id in project_ids}
    project_label_terms = frozenset(
        term.lower() for alias in aliases.values() for term in (alias, f"project {alias}") if alias
    )

    principals_by_project: dict[str, set[str]] = {project_id: set() for project_id in project_ids}
    for rel in relationships:
        project_id = rel.get("project_id")
        principal_id = rel.get("principal_id")
        if project_id in principals_by_project and principal_id:
            principals_by_project[str(project_id)].add(str(principal_id))

    return ProjectDirectory(
        project_ids=project_ids,
        aliases=aliases,
        principals_by_project=principals_by_project,
        display_names=display_names,
        all_principal_ids=all_ids,
        project_label_terms=project_label_terms,
    )


def detect_project_ids(text: str, directory: ProjectDirectory) -> list[str]:
    """Whole-word, case-insensitive alias match. Word boundaries keep sibling
    projects like "harbor"/"harbormark" from cross-matching each other."""
    return [
        project_id
        for project_id in directory.project_ids
        if directory.aliases[project_id] and re.search(rf"\b{re.escape(directory.aliases[project_id])}\b", text, re.IGNORECASE)
    ]


def detect_revocation(text: str, speaker_principal_id: str, directory: ProjectDirectory) -> str | None:
    """Best-effort: a revocation turn either names a principal (matched via
    the first token of their display name) or is a self-confirmation by the
    speaker ("my access is revoked") -- default to the speaker in that case."""
    if not matches_any(text, REVOKE_PATTERNS):
        return None
    for principal_id, display_name in directory.display_names.items():
        first_name = (display_name.split() or [""])[0]
        if first_name and re.search(rf"\b{re.escape(first_name)}\b", text, re.IGNORECASE):
            return principal_id
    return speaker_principal_id or None


@dataclass(slots=True)
class EpisodePolicyState:
    """Mutable per-episode access state: static relationship ACL minus any
    revocations detected while replaying the episode's turns in order."""

    directory: ProjectDirectory
    revoked: dict[str, set[str]] = field(default_factory=dict)

    def authorized_principals(self, project_ids: list[str]) -> set[str]:
        if not project_ids:
            return set(self.directory.all_principal_ids)
        authorized: set[str] = set()
        for project_id in project_ids:
            base = self.directory.principals_by_project.get(project_id, set())
            gone = self.revoked.get(project_id, set())
            authorized |= base - gone
        return authorized

    def is_authorized(self, asker_principal_id: str, project_ids: list[str]) -> bool:
        return asker_principal_id in self.authorized_principals(project_ids)

    def apply_revocation(self, principal_id: str, project_ids: list[str]) -> None:
        targets = project_ids or [
            project_id
            for project_id in self.directory.project_ids
            if principal_id in self.directory.principals_by_project.get(project_id, set())
        ]
        for project_id in targets:
            self.revoked.setdefault(project_id, set()).add(principal_id)


# --- Node helpers -------------------------------------------------------------


def node_project_ids(node: MemoryNode) -> list[str]:
    return list((node.metadata or {}).get("gatemem", {}).get("project_ids", []))


def node_speaker(node: MemoryNode) -> str:
    return str((node.metadata or {}).get("gatemem", {}).get("speaker_principal_id", ""))


def _scope_matches(turn_project_ids: list[str], node_project_ids_: list[str]) -> bool:
    """Whether a candidate node is in-scope for a turn's forget/supersede
    target search. A short follow-up instruction ("Delete the exact prior
    staging token...") routinely omits the project name that a longer
    introductory turn used -- exact-tuple project matching then excludes the
    real target outright and forces a match against whatever unrelated
    global-scope node happens to be in the pool instead. Treat either side
    lacking a detected project as "don't restrict"; only two turns that each
    name a *specific, different* project are considered out of scope for
    each other."""
    if not turn_project_ids or not node_project_ids_:
        return True
    return bool(set(turn_project_ids) & set(node_project_ids_))


def _write_fact_node(
    store: CCLGStore,
    *,
    content: str,
    source_label: str,
    project_ids: list[str],
    speaker_principal_id: str,
    speaker_role: str,
    episode_id: str,
    turn_id: str,
) -> MemoryNode:
    node = MemoryNode.create(
        content=content,
        source=source_label,
        scope={
            "user": speaker_principal_id or "unknown",
            "workspace": episode_id,
            "project": project_ids[0] if len(project_ids) == 1 else None,
            "agent": "gatemem-office-mode1",
        },
    )
    node.metadata = {
        "created_by": "gatemem_office_adapter",
        "review_status": "auto_applied",
        "privacy": "local_default",
        "gatemem": {
            "episode_id": episode_id,
            "turn_id": turn_id,
            "speaker_principal_id": speaker_principal_id,
            "speaker_role": speaker_role,
            "project_ids": list(project_ids),
        },
    }
    store.write_node(node)
    return node


def _retag_node(
    store: CCLGStore,
    node_id: str,
    *,
    project_ids: list[str],
    speaker_principal_id: str,
    speaker_role: str,
    episode_id: str,
    turn_id: str,
) -> None:
    """``apply_patch`` (cclg.patches, owned by CCLG core) creates the
    superseding node itself via a bare ``MemoryNode.create()`` that only
    copies ``scope``/``type``/``tags`` from its target -- it knows nothing
    about our ``metadata["gatemem"]`` ACL tagging. Without this, a
    supersede-created node would default to ``project_ids=[]`` (i.e. "global,
    open to everyone"), which is a real cross-project over-exposure risk for
    exactly the corrected/current values GateMem cares most about, and would
    also break project-scoped target search for any *later* turn that
    supersedes this same fact again. Re-tag right after ``apply_patch``
    returns so every node, regardless of which code path created it, carries
    the same ACL metadata shape."""
    node = store.read_node(node_id)
    node.metadata = {
        "created_by": "gatemem_office_adapter",
        "review_status": "auto_applied",
        "privacy": "local_default",
        "gatemem": {
            "episode_id": episode_id,
            "turn_id": turn_id,
            "speaker_principal_id": speaker_principal_id,
            "speaker_role": speaker_role,
            "project_ids": list(project_ids),
        },
    }
    store.write_node(node)


def ingest_turn(
    store: CCLGStore,
    *,
    episode_id: str,
    turn: dict[str, Any],
    directory: ProjectDirectory,
    policy_state: EpisodePolicyState,
) -> dict[str, Any]:
    """Ingest one GateMem turn: raw evidence + session event always; a
    MemoryNode/MemoryPatch only when the turn carries a new or mutated fact.

    "forget"-classified turns never become a new active node from their own
    text -- GateMem deletion turns routinely restate the exact value being
    deleted (e.g. "the value mp_stg_... should be treated as deleted"), so
    echoing that text into a fresh active node would be a self-inflicted
    leak. Only the *targets* found for the forget patch (established by an
    earlier turn) change status; the deletion turn's own text is recorded as
    raw evidence and a session event only, never as node content.
    """
    turn_id = str(turn.get("turn_id"))
    text = str(turn.get("text") or "")
    speaker = turn.get("speaker") or {}
    speaker_principal_id = str(speaker.get("principal_id") or "")
    speaker_role = str(speaker.get("role") or "")

    store.append_raw(f"{episode_id}-{turn_id}.txt", text)
    append_session_event(
        store,
        session_id=episode_id,
        event="gatemem_turn",
        payload={"turn_id": turn_id, "speaker_principal_id": speaker_principal_id, "speaker_role": speaker_role},
    )

    project_ids = detect_project_ids(text, directory)
    outcome: dict[str, Any] = {"turn_id": turn_id, "classification": None, "node_id": None, "patch_id": None, "target_ids": [], "revoked_principal": None}

    revoked_principal = detect_revocation(text, speaker_principal_id, directory)
    if revoked_principal:
        policy_state.apply_revocation(revoked_principal, project_ids)
        outcome["revoked_principal"] = revoked_principal

    kind = classify_turn(text)
    scope_pool = [node for node in store.iter_nodes() if node.status == "active" and _scope_matches(project_ids, node_project_ids(node))]

    if kind == "forget":
        outcome["classification"] = "forget"
        # A single confident target only, not "every positive-score hit":
        # BM25 assigns *some* positive score to almost any node sharing even
        # one shared term, so taking every hit turns one deletion turn into
        # mass collateral forgetting of unrelated active facts (observed
        # empirically -- a single credential-deletion turn was forgetting 5
        # unrelated budget/date/discount nodes). ``confident_top_hit`` also
        # rejects a near-tied top score, since this corpus's formulaic prose
        # means near-ties are common between genuinely unrelated turns.
        # Under-forgetting costs safety-checkpoint recall (documented Mode 1
        # limitation); over-forgetting silently destroys unrelated utility
        # content, which is worse for a mechanism-proof adapter. Forget gets
        # a stricter floor than supersede: this benchmark's episodes reaffirm
        # an already-deleted fact several times ("the retired token remains
        # unavailable...") over the rest of the episode, and by then its real
        # target has already left the active pool -- a lower floor lets these
        # reaffirmation turns latch onto whatever unrelated active node
        # happens to share the most incidental vocabulary. An exact value
        # match (see ``find_exact_value_target``) is tried first and takes
        # priority over the BM25 fallback when both fire.
        exact = find_exact_value_target(text, scope_pool, exclude_terms=directory.project_label_terms)
        if exact is not None:
            target_ids = [exact.id]
        else:
            hits = search_nodes_normalized(text, scope_pool, limit=5)
            top = confident_top_hit(hits, min_score=20.0, min_margin=1.2)
            target_ids = [top.node.id] if top else []
        if target_ids:
            patch = MemoryPatch.create(operation="forget", target_ids=target_ids, reason=f"gatemem:{turn_id}:forget", source=f"gatemem:{episode_id}:{turn_id}")
            apply_patch(store, patch)
            outcome["patch_id"] = patch.id
            outcome["target_ids"] = target_ids
        return outcome

    if kind == "supersede":
        hits = search_nodes_normalized(text, scope_pool, limit=5)
        top = confident_top_hit(hits)
        target_ids = [top.node.id] if top else []
        if target_ids:
            patch = MemoryPatch.create(
                operation="supersede",
                target_ids=target_ids,
                reason=f"gatemem:{turn_id}:supersede",
                new_content=text,
                source=f"gatemem:{episode_id}:{turn_id}",
            )
            apply_patch(store, patch)
            outcome["classification"] = "supersede"
            outcome["patch_id"] = patch.id
            outcome["target_ids"] = target_ids
            new_node_id = patch.new_node_ids[0] if patch.new_node_ids else None
            outcome["node_id"] = new_node_id
            if new_node_id:
                _retag_node(
                    store,
                    new_node_id,
                    project_ids=project_ids,
                    speaker_principal_id=speaker_principal_id,
                    speaker_role=speaker_role,
                    episode_id=episode_id,
                    turn_id=turn_id,
                )
            return outcome
        kind = "create"  # nothing found to supersede; fall through to a fresh fact

    normalized = normalize_content(text)
    duplicate = next((node for node in scope_pool if normalize_content(node.content) == normalized), None)
    if duplicate is not None:
        outcome["classification"] = "duplicate"
        outcome["node_id"] = duplicate.id
        return outcome

    node = _write_fact_node(
        store,
        content=text,
        source_label=f"gatemem:{episode_id}:{turn_id}",
        project_ids=project_ids,
        speaker_principal_id=speaker_principal_id,
        speaker_role=speaker_role,
        episode_id=episode_id,
        turn_id=turn_id,
    )
    outcome["classification"] = "create"
    outcome["node_id"] = node.id
    return outcome
