from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from .format import ACTIVE_MEMORY_PACK_SCHEMA, MEMORY_EDGE_SCHEMA, MEMORY_NODE_SCHEMA, MEMORY_PATCH_SCHEMA, source_label


NodeStatus = Literal[
    "active",
    "pending",
    "superseded",
    "deprecated",
    "expired",
    "forgotten",
    "conflict_pending",
    "active_session",
    "pending_promotion",
    "promoted",
    "discarded",
]
PatchOperation = Literal[
    "create",
    "update",
    "supersede",
    "refine",
    "expand",
    "narrow",
    "merge",
    "split",
    "expire",
    "deprecate",
    "forget",
    "resolve_conflict",
    "rollback",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def default_scope() -> dict[str, str | None]:
    return {"user": "user_local", "org": None, "workspace": None, "project": None, "agent": "global", "session": None}


def default_source(label: str = "manual", *, quote: str | None = None, raw_ref: str | None = None) -> dict[str, Any]:
    raw_spans = []
    if raw_ref:
        raw_spans.append({"source_id": raw_ref, "turn_id": None, "char_start": None, "char_end": None})
    value: dict[str, Any] = {
        "label": label,
        "session_ids": [],
        "turn_ids": [],
        "raw_spans": raw_spans,
        "tool_result_ids": [],
        "artifact_ids": [],
    }
    if quote is not None:
        value["quote"] = quote
    return value


def source_from_value(value: Any, *, fallback: str = "manual") -> dict[str, Any]:
    if isinstance(value, dict):
        if {"session_ids", "turn_ids", "raw_spans"}.intersection(value):
            merged = default_source(str(value.get("label") or value.get("source") or fallback))
            merged.update(value)
            return merged
        return default_source(str(value.get("source") or value.get("label") or fallback), quote=value.get("quote"), raw_ref=value.get("raw_ref"))
    if isinstance(value, str) and value:
        return default_source(value)
    return default_source(fallback)


def default_relations() -> dict[str, list[str]]:
    return {
        "supersedes": [],
        "superseded_by": [],
        "refines": [],
        "expands": [],
        "narrows": [],
        "contradicts": [],
        "depends_on": [],
        "derived_from": [],
    }


def default_retrieval(content: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    sparse = sorted({*(tags or []), *[part.strip(".,:;()[]{}").lower() for part in content.split() if len(part.strip(".,:;()[]{}")) > 2]})[:24]
    return {"sparse_keys": sparse, "dense_text": content, "entity_keys": [], "temporal_keys": []}


@dataclass(slots=True)
class Provenance:
    source: str
    quote: str | None = None
    raw_ref: str | None = None
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def from_value(cls, value: dict[str, Any] | str) -> "Provenance":
        if isinstance(value, str):
            return cls(source=value)
        return cls(
            source=str(value["source"]),
            quote=value.get("quote"),
            raw_ref=value.get("raw_ref"),
            created_at=value.get("created_at", now_iso()),
        )


@dataclass(slots=True)
class MemoryNode:
    id: str
    type: str
    content: str
    schema_version: str = MEMORY_NODE_SCHEMA
    scope: dict[str, str | None] = field(default_factory=default_scope)
    key: str | None = None
    status: NodeStatus = "active"
    confidence: float = 1.0
    priority: str = "normal"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    effective_from: str = field(default_factory=now_iso)
    effective_until: str | None = None
    source: dict[str, Any] = field(default_factory=default_source)
    relations: dict[str, list[str]] = field(default_factory=default_relations)
    retrieval: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=lambda: {"created_by": "manual", "review_status": "auto_applied", "privacy": "local_default"})
    tags: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        content: str,
        source: str,
        node_type: str = "memory",
        quote: str | None = None,
        scope: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> "MemoryNode":
        if not content.strip():
            raise ValueError("memory content cannot be empty")
        if not source.strip():
            raise ValueError("memory nodes require source label or source span")
        now = now_iso()
        node = cls(
            id=new_id("mem"),
            type=node_type,
            content=content.strip(),
            source=default_source(source.strip(), quote=quote),
            scope={**default_scope(), **(scope or {})},
            tags=tags or [],
            created_at=now,
            updated_at=now,
            effective_from=now,
        )
        node.retrieval = default_retrieval(node.content, node.tags)
        return node

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryNode":
        legacy_provenance = value.get("provenance")
        source = source_from_value(value.get("source", legacy_provenance), fallback=(legacy_provenance or {}).get("source", "manual") if isinstance(legacy_provenance, dict) else "manual")
        relations = default_relations()
        relations.update({key: list(items) for key, items in dict(value.get("relations", {})).items()})
        if "supersedes" in value:
            relations["supersedes"] = list(value.get("supersedes", []))
        tags = list(value.get("tags", []))
        content = str(value["content"])
        retrieval = dict(value.get("retrieval", {})) or default_retrieval(content, tags)
        return cls(
            id=str(value["id"]),
            type=str(value.get("type", "memory")),
            content=content,
            schema_version=value.get("schema_version", MEMORY_NODE_SCHEMA),
            scope={**default_scope(), **dict(value.get("scope", {}))},
            key=value.get("key"),
            status=value.get("status", "active"),
            confidence=float(value.get("confidence", 1.0)),
            priority=str(value.get("priority", "normal")),
            created_at=value.get("created_at", now_iso()),
            updated_at=value.get("updated_at", now_iso()),
            effective_from=value.get("effective_from", value.get("created_at", now_iso())),
            effective_until=value.get("effective_until"),
            source=source,
            relations=relations,
            retrieval=retrieval,
            metadata=dict(value.get("metadata", {"created_by": "unknown", "review_status": "imported", "privacy": "local_default"})),
            tags=tags,
        )

    @property
    def provenance(self) -> Provenance:
        return Provenance(source=source_label(self.source), quote=self.source.get("quote"), raw_ref=(self.source.get("raw_spans") or [{}])[0].get("source_id") if self.source.get("raw_spans") else None)

    @property
    def supersedes(self) -> list[str]:
        return self.relations.setdefault("supersedes", [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "type": self.type,
            "scope": self.scope,
            "key": self.key,
            "content": self.content,
            "status": self.status,
            "confidence": self.confidence,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "effective_from": self.effective_from,
            "effective_until": self.effective_until,
            "source": self.source,
            "relations": self.relations,
            "retrieval": self.retrieval or default_retrieval(self.content, self.tags),
            "metadata": self.metadata,
            "tags": self.tags,
        }


@dataclass(slots=True)
class MemoryPatch:
    id: str
    operation: PatchOperation
    target_ids: list[str]
    reason: str
    schema_version: str = MEMORY_PATCH_SCHEMA
    new_node_ids: list[str] = field(default_factory=list)
    new_content: str | None = None
    source: dict[str, Any] = field(default_factory=default_source)
    confidence: float = 1.0
    resolution_policy: dict[str, Any] = field(default_factory=lambda: {"rule": "manual", "auto_applied": True, "requires_review": False})
    prior_states: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    applied_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        operation: PatchOperation,
        target_ids: list[str],
        reason: str,
        new_content: str | None = None,
        source: str = "manual",
    ) -> "MemoryPatch":
        if operation not in {"create", "update", "supersede", "refine", "expand", "narrow", "merge", "split", "expire", "deprecate", "forget", "resolve_conflict", "rollback"}:
            raise ValueError(f"unsupported patch operation: {operation}")
        if operation != "create" and not target_ids:
            raise ValueError("patch target_ids cannot be empty")
        if operation in {"create", "update", "supersede", "refine", "expand", "narrow", "merge", "split", "resolve_conflict"} and not new_content:
            raise ValueError(f"{operation} patch requires new_content")
        return cls(
            id=new_id("patch"),
            operation=operation,
            target_ids=target_ids,
            reason=reason.strip() or operation,
            new_content=new_content.strip() if new_content else None,
            source=default_source(source),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryPatch":
        source = source_from_value(value.get("source"), fallback="manual")
        return cls(
            id=str(value["id"]),
            operation=value["operation"],
            target_ids=list(value.get("target_ids", [])),
            reason=str(value.get("reason", "")),
            schema_version=value.get("schema_version", MEMORY_PATCH_SCHEMA),
            new_node_ids=list(value.get("new_node_ids", [])),
            new_content=value.get("new_content"),
            source=source,
            confidence=float(value.get("confidence", 1.0)),
            resolution_policy=dict(value.get("resolution_policy", {"rule": "manual", "auto_applied": True, "requires_review": False})),
            prior_states=dict(value.get("prior_states", {})),
            created_at=value.get("created_at", now_iso()),
            applied_at=value.get("applied_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "operation": self.operation,
            "target_ids": self.target_ids,
            "new_node_ids": self.new_node_ids,
            "reason": self.reason,
            "new_content": self.new_content,
            "source": self.source,
            "confidence": self.confidence,
            "resolution_policy": self.resolution_policy,
            "prior_states": self.prior_states,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
        }


@dataclass(slots=True)
class MemoryEdge:
    id: str
    from_id: str
    to_id: str
    type: str
    schema_version: str = MEMORY_EDGE_SCHEMA
    created_at: str = field(default_factory=now_iso)
    source_patch_id: str | None = None

    @classmethod
    def create(cls, *, from_id: str, to_id: str, edge_type: str, source_patch_id: str | None = None) -> "MemoryEdge":
        return cls(id=new_id("edge"), from_id=from_id, to_id=to_id, type=edge_type, source_patch_id=source_patch_id)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryEdge":
        return cls(
            id=str(value["id"]),
            from_id=str(value.get("from", value.get("from_id"))),
            to_id=str(value.get("to", value.get("to_id"))),
            type=str(value["type"]),
            schema_version=value.get("schema_version", MEMORY_EDGE_SCHEMA),
            created_at=value.get("created_at", now_iso()),
            source_patch_id=value.get("source_patch_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "from": self.from_id,
            "to": self.to_id,
            "type": self.type,
            "created_at": self.created_at,
            "source_patch_id": self.source_patch_id,
        }


@dataclass(slots=True)
class ActiveMemoryPack:
    schema_version: str
    query: str
    generated_at: str
    memory_nodes: list[dict[str, Any]]
    suppressed_nodes: list[dict[str, Any]]
    budget: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
