from __future__ import annotations

from typing import Any

from .format import (
    ACTIVE_MEMORY_PACK_SCHEMA,
    MEMORY_EDGE_SCHEMA,
    MEMORY_NODE_SCHEMA,
    MEMORY_PATCH_SCHEMA,
    SESSION_SCHEMA,
)


NODE_STATUSES = {
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
}

PATCH_OPERATIONS = {
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
}

EDGE_TYPES = {
    "supersedes",
    "refines",
    "expands",
    "narrows",
    "contradicts",
    "depends_on",
    "derived_from",
    "temporary_override",
    "source_of",
    "blocks",
    "resolves",
}

NODE_TYPES = {
    "preference",
    "identity_fact",
    "project_fact",
    "project_decision",
    "task",
    "commitment",
    "correction",
    "constraint",
    "tool_result",
    "artifact_reference",
    "code_state",
    "warning",
    "relationship",
    "temporal_event",
    "runbook",
    "benchmark_requirement",
    "security_policy",
    "memory",
}

REQUIRED_NODE_FIELDS = [
    "schema_version",
    "id",
    "type",
    "scope",
    "key",
    "content",
    "status",
    "confidence",
    "priority",
    "created_at",
    "updated_at",
    "effective_from",
    "effective_until",
    "source",
    "relations",
    "retrieval",
    "metadata",
]

REQUIRED_PATCH_FIELDS = [
    "schema_version",
    "id",
    "operation",
    "target_ids",
    "new_node_ids",
    "reason",
    "source",
    "confidence",
    "resolution_policy",
    "created_at",
    "applied_at",
]

REQUIRED_EDGE_FIELDS = ["schema_version", "id", "from", "to", "type", "created_at", "source_patch_id"]
REQUIRED_SESSION_FIELDS = [
    "schema_version",
    "id",
    "agent",
    "workspace",
    "project",
    "started_at",
    "ended_at",
    "status",
    "parent_session_id",
    "branch_name",
    "loaded_memory_ids",
    "session_overlay_ids",
    "pending_patch_ids",
    "active_task",
    "policy",
    "events",
    "created_at",
    "updated_at",
]
REQUIRED_PACK_FIELDS = ["schema_version", "query", "generated_at", "memory_nodes", "suppressed_nodes", "budget"]


def validate_node(value: dict[str, Any], *, known_ids: set[str] | None = None) -> list[str]:
    problems = missing_fields("node", value, REQUIRED_NODE_FIELDS)
    if value.get("schema_version") != MEMORY_NODE_SCHEMA:
        problems.append(f"node {value.get('id')}: schema_version must be {MEMORY_NODE_SCHEMA}")
    if not str(value.get("id", "")).startswith("mem_"):
        problems.append(f"node {value.get('id')}: id must start with mem_")
    if value.get("type") not in NODE_TYPES:
        problems.append(f"node {value.get('id')}: unknown type {value.get('type')}")
    if value.get("status") not in NODE_STATUSES:
        problems.append(f"node {value.get('id')}: unknown status {value.get('status')}")
    if not str(value.get("content", "")).strip():
        problems.append(f"node {value.get('id')}: content cannot be empty")
    source = value.get("source")
    if not is_source_grounded(source) and value.get("status") not in {"active_session", "pending", "pending_promotion"}:
        problems.append(f"node {value.get('id')}: long-term node requires source label or source raw span")
    relations = value.get("relations")
    if not isinstance(relations, dict):
        problems.append(f"node {value.get('id')}: relations must be object")
    elif known_ids is not None:
        for relation, node_ids in relations.items():
            if not isinstance(node_ids, list):
                problems.append(f"node {value.get('id')}: relations.{relation} must be list")
                continue
            for node_id in node_ids:
                if node_id not in known_ids:
                    problems.append(f"node {value.get('id')}: relations.{relation} missing node {node_id}")
    return problems


def validate_patch(value: dict[str, Any], *, known_ids: set[str] | None = None) -> list[str]:
    problems = missing_fields("patch", value, REQUIRED_PATCH_FIELDS)
    if value.get("schema_version") != MEMORY_PATCH_SCHEMA:
        problems.append(f"patch {value.get('id')}: schema_version must be {MEMORY_PATCH_SCHEMA}")
    if not str(value.get("id", "")).startswith("patch_"):
        problems.append(f"patch {value.get('id')}: id must start with patch_")
    operation = value.get("operation")
    if operation not in PATCH_OPERATIONS:
        problems.append(f"patch {value.get('id')}: unknown operation {operation}")
    if operation != "create" and not value.get("target_ids"):
        problems.append(f"patch {value.get('id')}: target_ids required for {operation}")
    if operation in {"create", "update", "supersede", "refine", "expand", "narrow", "merge", "split", "resolve_conflict"} and not value.get("new_content"):
        problems.append(f"patch {value.get('id')}: new_content required for {operation}")
    if known_ids is not None:
        for node_id in value.get("target_ids", []):
            if node_id not in known_ids:
                problems.append(f"patch {value.get('id')}: missing target node {node_id}")
        for node_id in value.get("new_node_ids", []):
            if node_id not in known_ids:
                problems.append(f"patch {value.get('id')}: missing new node {node_id}")
    return problems


def validate_edge(value: dict[str, Any], *, known_ids: set[str] | None = None, known_patch_ids: set[str] | None = None) -> list[str]:
    problems = missing_fields("edge", value, REQUIRED_EDGE_FIELDS)
    if value.get("schema_version") != MEMORY_EDGE_SCHEMA:
        problems.append(f"edge {value.get('id')}: schema_version must be {MEMORY_EDGE_SCHEMA}")
    if not str(value.get("id", "")).startswith("edge_"):
        problems.append(f"edge {value.get('id')}: id must start with edge_")
    if value.get("type") not in EDGE_TYPES:
        problems.append(f"edge {value.get('id')}: unknown type {value.get('type')}")
    if known_ids is not None:
        if value.get("from") not in known_ids:
            problems.append(f"edge {value.get('id')}: missing from node {value.get('from')}")
        if value.get("to") not in known_ids:
            problems.append(f"edge {value.get('id')}: missing to node {value.get('to')}")
    if known_patch_ids is not None and value.get("source_patch_id") and value.get("source_patch_id") not in known_patch_ids:
        problems.append(f"edge {value.get('id')}: missing source patch {value.get('source_patch_id')}")
    return problems


def validate_session(value: dict[str, Any]) -> list[str]:
    problems = missing_fields("session", value, REQUIRED_SESSION_FIELDS)
    if value.get("schema_version") != SESSION_SCHEMA:
        problems.append(f"session {value.get('id')}: schema_version must be {SESSION_SCHEMA}")
    if value.get("status") not in {"active", "ended", "forked", "merged"}:
        problems.append(f"session {value.get('id')}: unknown status {value.get('status')}")
    return problems


def validate_active_pack(value: dict[str, Any]) -> list[str]:
    problems = missing_fields("active_memory_pack", value, REQUIRED_PACK_FIELDS)
    if value.get("schema_version") != ACTIVE_MEMORY_PACK_SCHEMA:
        problems.append(f"active_memory_pack: schema_version must be {ACTIVE_MEMORY_PACK_SCHEMA}")
    for node in value.get("memory_nodes", []):
        if node.get("status") != "active":
            problems.append(f"active_memory_pack: non-active node emitted as memory {node.get('id')}")
    return problems


def missing_fields(kind: str, value: dict[str, Any], required: list[str]) -> list[str]:
    return [f"{kind} {value.get('id')}: missing {field}" for field in required if field not in value]


def is_source_grounded(source: Any) -> bool:
    if not isinstance(source, dict):
        return False
    if source.get("label"):
        return True
    raw_spans = source.get("raw_spans")
    return isinstance(raw_spans, list) and any(span.get("source_id") for span in raw_spans if isinstance(span, dict))
