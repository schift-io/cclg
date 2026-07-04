"""`.cclg` portable container: pack/load the ledger as a single self-contained
artifact (see docs/CCLG_CONTAINER.md, the normative spec this module implements).

Distinct from `store.py`: the store is one backend's mutable directory layout
(`~/.cclg/nodes/*.json`, ...). This module packs/loads a layout-independent,
self-describing, checksummed text artifact from/into plain record dicts, reusing
`models.py`'s existing `to_dict()` / `from_dict()` contract — no new record
serialization is invented here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .format import (
    CCLG_CONTAINER_ID,
    CCLG_CONTAINER_MAGIC,
    CCLG_CONTAINER_VERSION,
    CCLG_FORMAT_ID,
    MEMORY_EDGE_SCHEMA,
    MEMORY_NODE_SCHEMA,
    MEMORY_PATCH_SCHEMA,
    SESSION_SCHEMA,
)
from .models import MemoryEdge, MemoryNode, MemoryPatch, now_iso
from .schema import validate_edge, validate_node, validate_patch, validate_session
from .store import CCLGStore

# Fixed on-disk order (docs/CCLG_CONTAINER.md §2.3). New known sections are a
# breaking container-version bump, not an addition to this tuple.
SECTION_ORDER: tuple[str, ...] = ("nodes", "patches", "edges", "sessions")

_SECTION_SCHEMA_VERSION = {
    "nodes": MEMORY_NODE_SCHEMA,
    "patches": MEMORY_PATCH_SCHEMA,
    "edges": MEMORY_EDGE_SCHEMA,
    "sessions": SESSION_SCHEMA,
}

# Schift platform-auth fields that must never appear in a container, at any
# nesting depth, in header or records (docs/CCLG_CONTAINER.md §3.2). Deliberately
# narrow: CCLG's own local scope model legitimately uses bare "user"/"org" keys
# (MemoryNode.scope) which are not platform credentials and must not be flagged.
FORBIDDEN_AUTH_KEYS = {
    "org_id",
    "user_id",
    "bucket",
    "collection",
    "api_key",
    "apikey",
    "token",
    "access_token",
}


class ContainerError(ValueError):
    """Raised for any structural, checksum, auth-guard, or schema violation."""


@dataclass(slots=True)
class ContainerBundle:
    """Parsed, validated contents of a loaded `.cclg` container."""

    header: dict[str, Any]
    nodes: list[dict[str, Any]] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    unknown_sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name in SECTION_ORDER}

    def memory_nodes(self) -> list[MemoryNode]:
        return [MemoryNode.from_dict(record) for record in self.nodes]

    def memory_patches(self) -> list[MemoryPatch]:
        return [MemoryPatch.from_dict(record) for record in self.patches]

    def memory_edges(self) -> list[MemoryEdge]:
        return [MemoryEdge.from_dict(record) for record in self.edges]


def _as_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return record
    if hasattr(record, "to_dict"):
        return record.to_dict()
    raise ContainerError(f"unsupported record type for container packing: {type(record)!r}")


def _canonical_json(record: dict[str, Any]) -> str:
    # No sort_keys: "canonical" here means "whatever field order the record's own
    # to_dict() produced" (docs/CCLG_CONTAINER.md §2.3), which mirrors
    # format/cclg.format.v0.1.toml's per-record canonical_order table. Sorting
    # keys alphabetically would silently diverge from that declared order and
    # contradict §2.3's "no new serialization is invented here" claim.
    return json.dumps(record, ensure_ascii=False)


def _scan_forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            if str(key).lower() in FORBIDDEN_AUTH_KEYS:
                found.add(str(key))
            found |= _scan_forbidden_keys(sub)
    elif isinstance(value, list):
        for item in value:
            found |= _scan_forbidden_keys(item)
    return found


def _guard_auth_free(value: dict[str, Any], *, ref: str) -> None:
    found = _scan_forbidden_keys(value)
    if found:
        raise ContainerError(f"{ref}: forbidden auth field(s) present (container must be auth-free): {', '.join(sorted(found))}")


def pack_container(
    nodes: Iterable[Any],
    patches: Iterable[Any] = (),
    edges: Iterable[Any] = (),
    sessions: Iterable[Any] = (),
    *,
    validate: bool = True,
) -> str:
    """Pack node/patch/edge/session records into a `.cclg` container string.

    Accepts either the dataclass instances (``MemoryNode``/``MemoryPatch``/
    ``MemoryEdge``) or plain dicts already shaped like their ``to_dict()``
    output (sessions are always plain dicts — there is no session dataclass).
    """
    sections: dict[str, list[dict[str, Any]]] = {
        "nodes": [_as_dict(record) for record in nodes],
        "patches": [_as_dict(record) for record in patches],
        "edges": [_as_dict(record) for record in edges],
        "sessions": [_as_dict(record) for record in sessions],
    }

    known_ids = {record.get("id") for record in sections["nodes"]}
    known_patch_ids = {record.get("id") for record in sections["patches"]}

    if validate:
        problems: list[str] = []
        for record in sections["nodes"]:
            problems.extend(validate_node(record, known_ids=known_ids))
        for record in sections["patches"]:
            problems.extend(validate_patch(record, known_ids=known_ids))
        for record in sections["edges"]:
            problems.extend(validate_edge(record, known_ids=known_ids, known_patch_ids=known_patch_ids))
        for record in sections["sessions"]:
            problems.extend(validate_session(record))
        if problems:
            raise ContainerError("invalid records: " + "; ".join(problems))

    for name in SECTION_ORDER:
        for record in sections[name]:
            _guard_auth_free(record, ref=f"{name}:{record.get('id')}")

    body_lines: list[str] = []
    section_meta: list[dict[str, Any]] = []
    for name in SECTION_ORDER:
        body_lines.append(f"@{name}")
        for record in sections[name]:
            body_lines.append(_canonical_json(record))
        section_meta.append({"name": name, "count": len(sections[name])})

    # Checksum domain is exactly the body: every line after the header line,
    # joined with a literal "\n" (not str.splitlines() semantics — see
    # docs/CCLG_CONTAINER.md §6 for why that distinction matters).
    content_sha256 = hashlib.sha256("\n".join(body_lines).encode("utf-8")).hexdigest()

    header = {
        "container": CCLG_CONTAINER_ID,
        "format_id": CCLG_FORMAT_ID,
        "versions": {
            "memory_node": _SECTION_SCHEMA_VERSION["nodes"],
            "memory_patch": _SECTION_SCHEMA_VERSION["patches"],
            "edge": _SECTION_SCHEMA_VERSION["edges"],
            "session": _SECTION_SCHEMA_VERSION["sessions"],
        },
        "sections": section_meta,
        "counts": {name: len(sections[name]) for name in SECTION_ORDER},
        "generated_at": now_iso(),
        "content_sha256": content_sha256,
    }
    _guard_auth_free(header, ref="header")

    # Header and each record line both keep their as-constructed field order —
    # the header's container/format_id/versions/sections/counts/generated_at/
    # content_sha256 (docs/CCLG_CONTAINER.md §2.2), and each record's
    # to_dict()-defined order, mirroring format/cclg.format.v0.1.toml's
    # per-record canonical_order table (§2.3). Order is not semantically
    # load-bearing for either (both are plain JSON objects), only a
    # diff-stability convention this container format reuses verbatim instead
    # of re-deriving via alphabetical sort.
    header_json = json.dumps(header, ensure_ascii=False)
    lines = [f"{CCLG_CONTAINER_MAGIC}\t{CCLG_CONTAINER_VERSION}", header_json, *body_lines]
    return "\n".join(lines) + "\n"


def pack_from_store(store: CCLGStore, *, session_ids: Iterable[str] | None = None, validate: bool = True) -> str:
    """Pack a whole `CCLGStore` ledger into a `.cclg` container string.

    ``session_ids``, when given, narrows the ``@sessions`` section to just
    those session ids (nodes/patches/edges are always packed in full — the
    container is a ledger export, not a per-session slice of the graph).
    """
    from .session import load_session

    store.init()
    nodes = [node.to_dict() for node in store.iter_nodes()]
    patches = [patch.to_dict() for patch in store.iter_patches()]
    edges = [edge.to_dict() for edge in store.iter_edges()]

    session_paths = sorted(store.sessions_dir.glob("*.json")) if store.sessions_dir.exists() else []
    wanted = set(session_ids) if session_ids is not None else None
    sessions = [load_session(store, path.stem) for path in session_paths if wanted is None or path.stem in wanted]

    return pack_container(nodes, patches, edges, sessions, validate=validate)


def load_container(text: str, *, validate: bool = True) -> ContainerBundle:
    """Parse, structurally verify, and (optionally) schema-validate a `.cclg` container.

    Raises ``ContainerError`` on bad magic, unsupported container version,
    malformed header, section/checksum count mismatch (including a section
    present in the body but missing its ``counts``/``sections`` header entry,
    docs/CCLG_CONTAINER.md §5), checksum mismatch, an auth field anywhere in
    header or records, or (when ``validate=True``) any record failing its
    ``schema.py`` validator. Unknown sections are not an error: they are
    collected into ``ContainerBundle.unknown_sections`` with a warning, per
    docs/CCLG_CONTAINER.md §7. A ``header.format_id`` that does not match this
    reader's ``CCLG_FORMAT_ID`` is likewise not an error: it is appended to
    ``ContainerBundle.warnings``, per §4.
    """
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines = raw_lines[:-1]
    if not raw_lines:
        raise ContainerError("empty container: missing magic line")

    magic_line = raw_lines[0]
    if "\t" not in magic_line:
        raise ContainerError(f"malformed magic line (expected 'MAGIC<TAB>VERSION'): {magic_line!r}")
    magic, container_version = magic_line.split("\t", 1)
    if magic != CCLG_CONTAINER_MAGIC:
        raise ContainerError(f"bad magic: expected {CCLG_CONTAINER_MAGIC!r}, got {magic!r}")
    if container_version != CCLG_CONTAINER_VERSION:
        raise ContainerError(
            f"unsupported container version {container_version!r}: this reader only implements "
            f"{CCLG_CONTAINER_VERSION!r} (0.x minor bumps are breaking, per docs/CCLG_CONTAINER.md §1)"
        )
    if len(raw_lines) < 2:
        raise ContainerError("container missing header line")

    try:
        header = json.loads(raw_lines[1])
    except json.JSONDecodeError as exc:
        raise ContainerError(f"invalid header JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise ContainerError("header line must be a JSON object")
    if header.get("container") != CCLG_CONTAINER_ID:
        raise ContainerError(f"unsupported container id {header.get('container')!r}, expected {CCLG_CONTAINER_ID!r}")
    for required_key in ("format_id", "versions", "sections", "counts", "generated_at", "content_sha256"):
        if required_key not in header:
            raise ContainerError(f"header missing required key: {required_key}")

    body_lines = raw_lines[2:]
    sections: dict[str, list[dict[str, Any]]] = {}
    section_order: list[str] = []
    current: str | None = None
    for line in body_lines:
        if not line:
            continue
        if line.startswith("@"):
            current = line[1:].strip()
            if current not in sections:
                sections[current] = []
                section_order.append(current)
            continue
        if current is None:
            raise ContainerError(f"record line before any '@section' marker: {line!r}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContainerError(f"invalid record JSON under @{current}: {exc}") from exc
        if not isinstance(record, dict):
            raise ContainerError(f"record under @{current} must be a JSON object")
        sections[current].append(record)

    # Integrity checks before anything else is trusted (docs/CCLG_CONTAINER.md §6).
    recomputed_sha256 = hashlib.sha256("\n".join(body_lines).encode("utf-8")).hexdigest()
    expected_sha256 = header.get("content_sha256")
    if expected_sha256 != recomputed_sha256:
        raise ContainerError(f"checksum mismatch: header content_sha256={expected_sha256!r}, recomputed={recomputed_sha256!r}")

    header_counts = header.get("counts") or {}
    header_section_counts = {entry.get("name"): entry.get("count") for entry in header.get("sections") or []}
    for name in SECTION_ORDER:
        actual = len(sections.get(name, []))
        # A section actually present in the body (its "@name" marker was seen,
        # even with zero records under it) MUST be redundantly described by
        # *both* header.counts and header.sections (docs/CCLG_CONTAINER.md §5).
        # A missing entry for a present section is not "nothing to compare" —
        # it is exactly the truncated/hand-edited-header case §5 exists to
        # catch, so it is promoted to the same hard ContainerError as a
        # numeric mismatch, not silently skipped.
        section_present = name in sections
        for source_name, source_counts in (("counts", header_counts), ("sections", header_section_counts)):
            if name not in source_counts:
                if section_present:
                    raise ContainerError(
                        f"count mismatch for '{name}': header.{source_name} has no entry for it, "
                        f"but body has {actual} record(s) under '@{name}'"
                    )
                continue
            expected = source_counts[name]
            if expected != actual:
                raise ContainerError(f"count mismatch for '{name}': header.{source_name} says {expected}, body has {actual}")

    warnings: list[str] = []
    if header.get("format_id") != CCLG_FORMAT_ID:
        # §4: informational cross-check only — warn, don't hard-fail. The
        # authoritative check is each record's own schema_version, re-validated
        # per-record below regardless of what this header field claims.
        warnings.append(
            f"format_id mismatch: header declares {header.get('format_id')!r}, this reader implements "
            f"{CCLG_FORMAT_ID!r} — continuing per docs/CCLG_CONTAINER.md §4 (record-level schema_version "
            "is the authoritative check)"
        )
    unknown_sections: dict[str, list[dict[str, Any]]] = {}
    for name in section_order:
        if name not in SECTION_ORDER:
            unknown_sections[name] = sections[name]
            warnings.append(f"unknown section '@{name}' skipped ({len(sections[name])} record(s)) — forward-compat passthrough")

    bundle = ContainerBundle(
        header=header,
        nodes=sections.get("nodes", []),
        patches=sections.get("patches", []),
        edges=sections.get("edges", []),
        sessions=sections.get("sessions", []),
        unknown_sections=unknown_sections,
        warnings=warnings,
    )

    _guard_auth_free(header, ref="header")
    for name in SECTION_ORDER:
        for record in getattr(bundle, name):
            _guard_auth_free(record, ref=f"{name}:{record.get('id')}")

    if validate:
        known_ids = {record.get("id") for record in bundle.nodes}
        known_patch_ids = {record.get("id") for record in bundle.patches}
        problems: list[str] = []
        for record in bundle.nodes:
            problems.extend(validate_node(record, known_ids=known_ids))
        for record in bundle.patches:
            problems.extend(validate_patch(record, known_ids=known_ids))
        for record in bundle.edges:
            problems.extend(validate_edge(record, known_ids=known_ids, known_patch_ids=known_patch_ids))
        for record in bundle.sessions:
            problems.extend(validate_session(record))
        if problems:
            raise ContainerError("invalid records: " + "; ".join(problems))

    return bundle
