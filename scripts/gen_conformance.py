#!/usr/bin/env python3
"""Deterministic generator for the `.cclg` v1 golden conformance fixtures.

Run: ``PYTHONPATH=src python3 scripts/gen_conformance.py``

Every fixture uses fixed ids and a fixed timestamp -- no `uuid4()`, no
`now_iso()` / wall-clock reads -- so re-running this script reproduces the
committed `tests/conformance/*.cclg` / `*.expected.json` files byte-for-byte.
This script is not invoked at test time; `tests/test_conformance.py` reads the
already-committed fixtures.

Each fixture is a pair:
  - ``tests/conformance/<name>.cclg``          -- the container itself
  - ``tests/conformance/<name>.expected.json``  -- language-neutral expectation
    (sorted id lists, no Python-specific type names) a conforming reader in
    ANY language (this repo's Python, or the TypeScript port in
    ``derivatives/schift-ai-memory``) must reproduce.

See docs/CCLG_CONTAINER.md §13 for the coverage this suite is required to
provide and the rationale for each fixture.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "conformance"

import sys

sys.path.insert(0, str(REPO_ROOT / "src"))

from cclg.container import load_container, pack_container  # noqa: E402
from cclg.format import (  # noqa: E402
    CCLG_CONTAINER_ID,
    CCLG_CONTAINER_MAGIC,
    CCLG_CONTAINER_VERSION,
    CCLG_FORMAT_ID,
    MEMORY_EDGE_SCHEMA,
    MEMORY_NODE_SCHEMA,
    MEMORY_PATCH_SCHEMA,
    SESSION_SCHEMA,
)
from cclg.models import MemoryEdge, MemoryNode, MemoryPatch  # noqa: E402

FIXED_TS = "2026-01-01T00:00:00+00:00"
FIXED_GENERATED_AT = "2026-01-01T00:00:01+00:00"


def _node(
    node_id: str,
    content: str,
    *,
    status: str = "active",
    node_type: str = "memory",
    key: str | None = None,
    scope: dict[str, Any] | None = None,
    relations: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    raw = {
        "schema_version": MEMORY_NODE_SCHEMA,
        "id": node_id,
        "type": node_type,
        "scope": scope or {},
        "key": key,
        "content": content,
        "status": status,
        "confidence": 1.0,
        "priority": "normal",
        "created_at": FIXED_TS,
        "updated_at": FIXED_TS,
        "effective_from": FIXED_TS,
        "effective_until": None,
        "source": {"label": "conformance-fixture"},
        "relations": relations or {},
        "retrieval": {},
        "metadata": {"created_by": "conformance", "review_status": "auto_applied", "privacy": "local_default"},
        "tags": [],
    }
    return MemoryNode.from_dict(raw).to_dict()


def _patch(
    patch_id: str,
    operation: str,
    target_ids: list[str],
    *,
    new_content: str | None = None,
    new_node_ids: list[str] | None = None,
) -> dict[str, Any]:
    raw = {
        "schema_version": MEMORY_PATCH_SCHEMA,
        "id": patch_id,
        "operation": operation,
        "target_ids": target_ids,
        "new_node_ids": new_node_ids or [],
        "reason": f"conformance fixture: {operation}",
        "new_content": new_content,
        "source": {"label": "conformance-fixture"},
        "confidence": 1.0,
        "resolution_policy": {"rule": "manual", "auto_applied": True, "requires_review": False},
        "prior_states": {},
        "created_at": FIXED_TS,
        "applied_at": FIXED_TS,
    }
    return MemoryPatch.from_dict(raw).to_dict()


def _edge(edge_id: str, from_id: str, to_id: str, edge_type: str, *, source_patch_id: str | None = None) -> dict[str, Any]:
    raw = {
        "schema_version": MEMORY_EDGE_SCHEMA,
        "id": edge_id,
        "from": from_id,
        "to": to_id,
        "type": edge_type,
        "created_at": FIXED_TS,
        "source_patch_id": source_patch_id,
    }
    return MemoryEdge.from_dict(raw).to_dict()


def _session(session_id: str) -> dict[str, Any]:
    return {
        "schema_version": SESSION_SCHEMA,
        "id": session_id,
        "agent": "conformance",
        "workspace": "local",
        "project": "cclg",
        "started_at": FIXED_TS,
        "ended_at": None,
        "status": "active",
        "parent_session_id": None,
        "branch_name": "main",
        "loaded_memory_ids": [],
        "session_overlay_ids": [],
        "pending_patch_ids": [],
        "active_task": {"goal": "", "open_questions": [], "active_files": []},
        "policy": {"promotion": "default", "sync": "disabled", "retention": "keep_raw_local"},
        "events": [],
        "created_at": FIXED_TS,
        "updated_at": FIXED_TS,
    }


def _raw_container(sections: dict[str, list[dict[str, Any]]]) -> str:
    """Assemble a `.cclg` container text directly, bypassing `pack_container`'s
    schema validation AND auth-free guard. Used only for the negative fixtures
    (unknown patch operation, forbidden auth field) that must be structurally
    well-formed but semantically invalid -- `pack_container` itself refuses to
    emit either, by design, so those two fixtures cannot be produced through
    the normal packing API and are assembled at this lower level instead."""
    section_order = ("nodes", "patches", "edges", "sessions")
    body_lines: list[str] = []
    section_meta: list[dict[str, Any]] = []
    for name in section_order:
        body_lines.append(f"@{name}")
        for record in sections.get(name, []):
            body_lines.append(json.dumps(record, ensure_ascii=False))
        section_meta.append({"name": name, "count": len(sections.get(name, []))})

    content_sha256 = hashlib.sha256("\n".join(body_lines).encode("utf-8")).hexdigest()
    header = {
        "container": CCLG_CONTAINER_ID,
        "format_id": CCLG_FORMAT_ID,
        "versions": {
            "memory_node": MEMORY_NODE_SCHEMA,
            "memory_patch": MEMORY_PATCH_SCHEMA,
            "edge": MEMORY_EDGE_SCHEMA,
            "session": SESSION_SCHEMA,
        },
        "sections": section_meta,
        "counts": {name: len(sections.get(name, [])) for name in section_order},
        "generated_at": FIXED_GENERATED_AT,
        "content_sha256": content_sha256,
    }
    header_json = json.dumps(header, ensure_ascii=False)
    lines = [f"{CCLG_CONTAINER_MAGIC}\t{CCLG_CONTAINER_VERSION}", header_json, *body_lines]
    return "\n".join(lines) + "\n"


def _write_fixture(name: str, text: str, expected: dict[str, Any]) -> None:
    """Write the fixture pair. For a positive fixture (``effective_view_node_ids``
    present in ``expected``), this recomputes the effective view from the
    just-generated container and asserts it matches the hand-derived
    expectation already in ``expected`` -- a mismatch here means the
    generator's manual reasoning about a scenario disagrees with the actual
    implementation, and the script fails loudly rather than silently
    committing a wrong fixture. Negative fixtures (``expect_error`` present)
    skip this cross-check -- computing the view is exactly what must fail for
    those."""
    if "effective_view_node_ids" in expected:
        bundle = load_container(text)
        view = bundle.effective_view(session_id=expected["session_id"])
        actual_ids = sorted(node.id for node in view)
        expected_ids = sorted(expected["effective_view_node_ids"])
        assert actual_ids == expected_ids, (
            f"fixture {name!r}: hand-derived expected_view_node_ids {expected_ids} "
            f"does not match actual effective_view() output {actual_ids}"
        )
        expected["effective_view_node_ids"] = actual_ids

    (FIXTURES_DIR / f"{name}.cclg").write_text(text, encoding="utf-8")
    (FIXTURES_DIR / f"{name}.expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def gen_supersede_chain() -> None:
    """1. Supersede chain A -> B -> C, with A's `status` deliberately left
    unbaked ("active") to also exercise docs/CCLG_CONTAINER.md §3.1.1: the
    patch-driven exclusion must fire even when a producer never flipped the
    target's own status field. B is baked correctly ("superseded") -- both
    styles of producer must land on the same effective view."""
    node_a = _node("mem_chain_a", "A: original fact.", status="active")
    node_b = _node("mem_chain_b", "B: refined fact.", status="superseded", relations={"supersedes": ["mem_chain_a"]})
    node_c = _node("mem_chain_c", "C: final fact.", status="active", relations={"supersedes": ["mem_chain_b"]})
    patch_1 = _patch("patch_chain_1", "supersede", ["mem_chain_a"], new_content="B: refined fact.", new_node_ids=["mem_chain_b"])
    patch_2 = _patch("patch_chain_2", "supersede", ["mem_chain_b"], new_content="C: final fact.", new_node_ids=["mem_chain_c"])
    edge_1 = _edge("edge_chain_1", "mem_chain_b", "mem_chain_a", "supersedes", source_patch_id="patch_chain_1")
    edge_2 = _edge("edge_chain_2", "mem_chain_c", "mem_chain_b", "supersedes", source_patch_id="patch_chain_2")

    text = pack_container([node_a, node_b, node_c], [patch_1, patch_2], [edge_1, edge_2], [])
    _write_fixture(
        "01_supersede_chain",
        text,
        {
            "description": "A -> B -> C supersede chain; A's status is left unbaked ('active') to test patch-driven exclusion independent of baked status.",
            "session_id": None,
            "effective_view_node_ids": ["mem_chain_c"],
        },
    )


def gen_create_then_retire() -> None:
    """2. A node created via a `create` patch, later retired via `forget`."""
    node_created = _node("mem_created_d", "D: created via patch.")
    patch_create = _patch("patch_create_d", "create", [], new_content="D: created via patch.", new_node_ids=["mem_created_d"])
    node_survivor = _node("mem_survivor_e", "E: never touched.")
    patch_forget = _patch("patch_forget_d", "forget", ["mem_created_d"], new_content=None)

    text = pack_container([node_created, node_survivor], [patch_create, patch_forget], [], [])
    _write_fixture(
        "02_create_then_retire",
        text,
        {
            "description": "Node D created via a 'create' patch, then retired via 'forget'; only E (untouched) survives.",
            "session_id": None,
            "effective_view_node_ids": ["mem_survivor_e"],
        },
    )


def gen_scope_precedence() -> None:
    """3. Four nodes share key 'pref.tone' at global/workspace/project/session
    scope; only the session-scoped one (highest precedence) should survive."""
    session_id = "sess_scope_precedence"
    node_global = _node("mem_scope_global", "Tone: formal (global).", key="pref.tone", scope={})
    node_workspace = _node("mem_scope_workspace", "Tone: casual (workspace).", key="pref.tone", scope={"workspace": "ws1"})
    node_project = _node("mem_scope_project", "Tone: technical (project).", key="pref.tone", scope={"project": "proj1"})
    node_session = _node(
        "mem_scope_session",
        "Tone: playful (session).",
        key="pref.tone",
        status="active_session",
        scope={"session": session_id},
    )

    text = pack_container([node_global, node_workspace, node_project, node_session], [], [], [])
    _write_fixture(
        "03_scope_precedence",
        text,
        {
            "description": "Four same-key nodes at global/workspace/project/session scope; session > project > workspace > global collapses to the session node.",
            "session_id": session_id,
            "effective_view_node_ids": ["mem_scope_session"],
        },
    )


def gen_forget_expire_deprecate() -> None:
    """4. One node retired by each of forget/expire/deprecate, plus one
    untouched survivor."""
    node_forgotten = _node("mem_retire_forgotten", "F: forgotten.")
    node_expired = _node("mem_retire_expired", "G: expired.")
    node_deprecated = _node("mem_retire_deprecated", "H: deprecated.")
    node_survivor = _node("mem_retire_survivor", "I: untouched.")
    patches = [
        _patch("patch_retire_forget", "forget", ["mem_retire_forgotten"]),
        _patch("patch_retire_expire", "expire", ["mem_retire_expired"]),
        _patch("patch_retire_deprecate", "deprecate", ["mem_retire_deprecated"]),
    ]

    text = pack_container(
        [node_forgotten, node_expired, node_deprecated, node_survivor],
        patches,
        [],
        [],
    )
    _write_fixture(
        "04_forget_expire_deprecate",
        text,
        {
            "description": "One node each retired via forget/expire/deprecate; only the untouched survivor remains.",
            "session_id": None,
            "effective_view_node_ids": ["mem_retire_survivor"],
        },
    )


def gen_conflict_resolve() -> None:
    """5. A conflict_pending node resolved via `resolve_conflict` into a new
    active node."""
    node_conflict = _node("mem_conflict_pending", "J: conflicting claim.", status="conflict_pending")
    node_resolved = _node("mem_conflict_resolved", "J: resolved claim.", relations={"supersedes": ["mem_conflict_pending"]})
    patch_resolve = _patch(
        "patch_resolve_conflict",
        "resolve_conflict",
        ["mem_conflict_pending"],
        new_content="J: resolved claim.",
        new_node_ids=["mem_conflict_resolved"],
    )

    text = pack_container([node_conflict, node_resolved], [patch_resolve], [], [])
    _write_fixture(
        "05_conflict_pending_resolve",
        text,
        {
            "description": "A conflict_pending node resolved via resolve_conflict into a new active node; only the resolved node survives.",
            "session_id": None,
            "effective_view_node_ids": ["mem_conflict_resolved"],
        },
    )


def gen_rollback() -> None:
    """6. `rollback` is a deliberately non-retiring operation -- its target
    stays in the effective view."""
    node_a = _node("mem_rollback_a", "K: fact A.")
    node_b = _node("mem_rollback_b", "L: fact B.")
    patch_rollback = _patch("patch_rollback_a", "rollback", ["mem_rollback_a"])

    text = pack_container([node_a, node_b], [patch_rollback], [], [])
    _write_fixture(
        "06_rollback_non_retiring",
        text,
        {
            "description": "'rollback' does not retire its target -- both nodes remain in the effective view.",
            "session_id": None,
            "effective_view_node_ids": ["mem_rollback_a", "mem_rollback_b"],
        },
    )


def gen_unknown_patch_operation() -> None:
    """7. NEGATIVE fixture: a patch with an operation this format does not
    define at all ('archive'). Must error at load (schema validation, default
    validate=True) AND at effective-view computation if loaded with
    validate=False (patches.UnknownPatchOperationError) -- see
    docs/CCLG_CONTAINER.md §3.1.2/§4."""
    node = _node("mem_unknown_op_target", "M: targeted by an unrecognized operation.")
    bad_patch = _patch("patch_unknown_op", "archive", ["mem_unknown_op_target"], new_content="irrelevant")

    text = _raw_container({"nodes": [node], "patches": [bad_patch], "edges": [], "sessions": []})
    _write_fixture(
        "07_unknown_patch_operation",
        text,
        {
            "description": "A patch operation ('archive') outside the v1 closed set. Must error both at default (validate=True) load and, if loaded with validate=False, at effective-view computation.",
            "session_id": None,
            "expect_error": "load_default_and_effective_view",
        },
    )


def gen_forbidden_auth_field() -> None:
    """8. NEGATIVE fixture: a forbidden Schift platform-auth field
    ('org_id') nested inside a node record. Must error at load (auth-free
    guard, docs/CCLG_CONTAINER.md §3.2) regardless of `validate`."""
    node = _node("mem_auth_field_target", "N: carries a forbidden auth field.")
    node["metadata"] = dict(node["metadata"])
    node["metadata"]["org_id"] = "org_should_never_appear"

    text = _raw_container({"nodes": [node], "patches": [], "edges": [], "sessions": []})
    _write_fixture(
        "08_forbidden_auth_field",
        text,
        {
            "description": "A node record carrying a forbidden auth field ('org_id') nested in metadata. Must error at load regardless of validate=.",
            "session_id": None,
            "expect_error": "load_always",
        },
    )


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    gen_supersede_chain()
    gen_create_then_retire()
    gen_scope_precedence()
    gen_forget_expire_deprecate()
    gen_conflict_resolve()
    gen_rollback()
    gen_unknown_patch_operation()
    gen_forbidden_auth_field()
    print(f"wrote conformance fixtures to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
