from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .format import SESSION_SCHEMA
from .models import MemoryNode, now_iso
from .store import CCLGStore


def normalize_session_id(session_id: str | None = None) -> str:
    return session_id or f"session_{uuid4().hex[:12]}"


def session_path(store: CCLGStore, session_id: str) -> Path:
    store.init()
    return store.sessions_dir / f"{session_id}.json"


def load_session(store: CCLGStore, session_id: str) -> dict:
    path = session_path(store, session_id)
    if not path.exists():
        return new_session_state(session_id=session_id)
    session = json.loads(path.read_text(encoding="utf-8"))
    session.setdefault("schema_version", SESSION_SCHEMA)
    for key, value in new_session_state(session_id=session.get("id", session_id)).items():
        session.setdefault(key, value)
    return session


def save_session(store: CCLGStore, session: dict) -> None:
    session.setdefault("schema_version", SESSION_SCHEMA)
    session["updated_at"] = now_iso()
    session_path(store, session["id"]).write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_session_event(store: CCLGStore, *, session_id: str | None, event: str, payload: dict) -> dict:
    sid = normalize_session_id(session_id)
    session = load_session(store, sid)
    record = {"event": event, "payload": payload, "created_at": now_iso()}
    session.setdefault("events", []).append(record)
    save_session(store, session)
    store.append_raw(f"{sid}-{event}-{record['created_at'].replace(':', '')}.json", json.dumps(record, ensure_ascii=False, indent=2))
    store.append_audit({"event": "session_event", "session_id": sid, "session_event": event})
    return session


def new_session_state(
    *,
    session_id: str,
    agent: str = "codex",
    workspace: str = "local",
    project: str = "default",
    parent_session_id: str | None = None,
    branch_name: str = "main",
) -> dict:
    now = now_iso()
    return {
        "schema_version": SESSION_SCHEMA,
        "id": session_id,
        "agent": agent,
        "workspace": workspace,
        "project": project,
        "started_at": now,
        "ended_at": None,
        "status": "active",
        "parent_session_id": parent_session_id,
        "branch_name": branch_name,
        "loaded_memory_ids": [],
        "session_overlay_ids": [],
        "pending_patch_ids": [],
        "active_task": {"goal": "", "open_questions": [], "active_files": []},
        "policy": {"promotion": "default", "sync": "disabled", "retention": "keep_raw_local"},
        "events": [],
        "created_at": now,
        "updated_at": now,
    }


def start_session(
    store: CCLGStore,
    *,
    session_id: str | None = None,
    agent: str = "codex",
    workspace: str = "local",
    project: str = "default",
    parent_session_id: str | None = None,
    branch_name: str = "main",
) -> dict:
    sid = normalize_session_id(session_id)
    session = new_session_state(
        session_id=sid,
        agent=agent,
        workspace=workspace,
        project=project,
        parent_session_id=parent_session_id,
        branch_name=branch_name,
    )
    save_session(store, session)
    store.append_audit({"event": "session_started", "session_id": sid})
    return session


def end_session(store: CCLGStore, session_id: str, *, status: str = "ended", policy: str = "keep") -> dict:
    session = load_session(store, session_id)
    promoted: list[str] = []
    discarded: list[str] = []
    if policy in {"promote", "discard"}:
        for node_id in list(session.get("session_overlay_ids", [])):
            path = store.nodes_dir / f"{node_id}.json"
            if not path.exists():
                continue
            node = store.read_node(node_id)
            if node.status != "active_session":
                continue
            if policy == "promote":
                _promote(store, node)
                promoted.append(node_id)
            else:
                node.status = "discarded"
                node.updated_at = now_iso()
                store.write_node(node)
                discarded.append(node_id)
    session["status"] = status
    session["ended_at"] = now_iso()
    save_session(store, session)
    store.append_audit({"event": "session_ended", "session_id": session_id, "status": status, "policy": policy, "promoted": promoted, "discarded": discarded})
    session["promoted_node_ids"] = promoted
    session["discarded_node_ids"] = discarded
    return session


def _promote(store: CCLGStore, node: MemoryNode) -> MemoryNode:
    node.status = "active"
    scope = dict(node.scope)
    scope["session"] = None
    node.scope = scope
    node.updated_at = now_iso()
    store.write_node(node)
    return node


def promote_session_node(store: CCLGStore, *, session_id: str, node_id: str) -> MemoryNode:
    session = load_session(store, session_id)
    if node_id not in session.get("session_overlay_ids", []):
        raise ValueError(f"node {node_id} is not an overlay of session {session_id}")
    node = _promote(store, store.read_node(node_id))
    store.append_audit({"event": "session_node_promoted", "session_id": session_id, "node_id": node_id})
    return node


def fork_session(store: CCLGStore, parent_session_id: str, *, branch_name: str = "fork", new_session_id: str | None = None) -> dict:
    parent = load_session(store, parent_session_id)
    child = start_session(
        store,
        session_id=normalize_session_id(new_session_id),
        agent=parent.get("agent", "codex"),
        workspace=parent.get("workspace", "local"),
        project=parent.get("project", "default"),
        parent_session_id=parent_session_id,
        branch_name=branch_name,
    )
    child["loaded_memory_ids"] = list(parent.get("loaded_memory_ids", []))
    save_session(store, child)
    store.append_audit({"event": "session_forked", "parent": parent_session_id, "child": child["id"]})
    return child


def merge_session(store: CCLGStore, session_id: str) -> dict:
    session = load_session(store, session_id)
    active_keys = {
        node.key: node.id
        for node in store.iter_nodes()
        if node.status == "active" and node.key and node.id not in session.get("session_overlay_ids", [])
    }
    merged: list[str] = []
    conflicts: list[str] = []
    for node_id in list(session.get("session_overlay_ids", [])):
        path = store.nodes_dir / f"{node_id}.json"
        if not path.exists():
            continue
        node = store.read_node(node_id)
        if node.status != "active_session":
            continue
        if node.key and node.key in active_keys:
            node.status = "conflict_pending"
            scope = dict(node.scope)
            scope["session"] = None
            node.scope = scope
            node.updated_at = now_iso()
            store.write_node(node)
            conflicts.append(node_id)
        else:
            _promote(store, node)
            merged.append(node_id)
    session["status"] = "merged"
    session["ended_at"] = now_iso()
    save_session(store, session)
    store.append_audit({"event": "session_merged", "session_id": session_id, "merged": merged, "conflicts": conflicts})
    return {"session_id": session_id, "merged_node_ids": merged, "conflict_node_ids": conflicts}


def write_overlay_node(
    store: CCLGStore,
    *,
    session_id: str,
    content: str,
    source: str = "session-overlay",
    node_type: str = "memory",
) -> MemoryNode:
    session = load_session(store, session_id)
    node = MemoryNode.create(
        content=content,
        source=source,
        node_type=node_type,
        scope={
            "agent": session.get("agent", "global"),
            "workspace": session.get("workspace"),
            "project": session.get("project"),
            "session": session_id,
        },
    )
    node.status = "active_session"
    store.write_node(node)
    session.setdefault("session_overlay_ids", []).append(node.id)
    save_session(store, session)
    store.append_audit({"event": "session_overlay_written", "session_id": session_id, "node_id": node.id})
    return node
