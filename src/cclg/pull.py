"""`cclg pull` — import a tenant's CORE/AGENT memory scope from a Schift
agent-hub `.cclg` export (docs/plans/2026-07-10-cclg-schift-memory-coexistence.md
§2/S2) into the local CCLG store.

Network side mirrors ``dense.py``'s policy: the auth secret is read from the
environment only, never accepted as a CLI flag and never persisted to
``config.json`` — a pulled container itself is already auth-free by
construction (``container.load_container``'s auth-free guard), but the
*request* to fetch it still needs a credential, and that credential must not
end up on disk or in shell history.

Import semantics (deliberately conservative, no "last write wins"):

- The container is loaded with ``load_container(validate=True)`` — fail-closed
  on bad checksum, unknown format_id, or an unrecognized patch operation
  propagates straight out of this module (docs/CCLG_CONTAINER.md's load
  semantics section; §2.5 of the plan doc extends the same posture to pull).
- Every imported node gets ``scope.org`` stamped to the pulling tenant,
  merged into (not replacing) whatever scope the node already carried, and
  ``metadata.pulled_from`` recorded so provenance survives round-trips
  (``source`` itself — including its ``label``, i.e. the original citation —
  is left untouched).
- Same node id, identical content already on disk: skip (idempotent re-pull).
- Same node id, different content: the *local* node is left in place but
  flipped to ``conflict_pending`` (the same status ``session.merge_session``
  already uses for a session/long-term key collision — this reuses that
  vocabulary rather than inventing a new one), and the remote version is
  written under a *new* local id with a ``relations.contradicts`` link in
  both directions. The remote copy is never allowed to silently overwrite a
  local fact by reusing its id — that would be exactly the "most recent
  wins" heuristic the plan doc rules out (§1, §S2).
- Patches are append-only: a patch id already on disk is skipped, otherwise
  written verbatim, with its ``target_ids``/``new_node_ids`` remapped through
  whatever id substitutions the conflict handling above introduced.
- Edges follow the same id-remap + skip-if-already-present rule as patches,
  for completeness (a Schift export today never actually emits edges/sessions
  — ``agent_hub.memory_export`` only ever packs nodes + patches — but nothing
  here assumes that stays true).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .container import ContainerBundle, load_container
from .models import MemoryEdge, MemoryNode, MemoryPatch, now_iso
from .store import CCLGStore

# Env-only, by design (see module docstring): never a CLI flag, never written
# to config.json. Matches the header the agent-hub shared-secret gate expects.
PULL_SECRET_ENV = "CCLG_PULL_SECRET"
_SECRET_HEADER = "X-Room821-Agent-Hub-Secret"

_VALID_SCOPES = {"core", "agent"}


class PullError(RuntimeError):
    """Raised for a missing secret, a transport failure, or a bad response."""


@dataclass(slots=True)
class PullSummary:
    tenant: str
    scope: str
    agent: str
    remote: str
    imported_nodes: int = 0
    skipped_nodes: int = 0
    conflict_nodes: int = 0
    imported_patches: int = 0
    skipped_patches: int = 0
    imported_edges: int = 0
    skipped_edges: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "scope": self.scope,
            "agent": self.agent,
            "remote": self.remote,
            "imported_nodes": self.imported_nodes,
            "skipped_nodes": self.skipped_nodes,
            "conflict_nodes": self.conflict_nodes,
            "imported_patches": self.imported_patches,
            "skipped_patches": self.skipped_patches,
            "imported_edges": self.imported_edges,
            "skipped_edges": self.skipped_edges,
        }


def _pull_secret() -> str:
    secret = (os.environ.get(PULL_SECRET_ENV) or "").strip()
    if not secret:
        raise PullError(
            f"{PULL_SECRET_ENV} is not set — the pull secret is env-only by policy "
            f"(never a CLI flag, never persisted to config.json). Export "
            f"{PULL_SECRET_ENV} and retry."
        )
    return secret


def fetch_container_text(*, remote: str, tenant: str, scope: str, agent: str = "") -> str:
    """Fetch a `.cclg` container's raw text from agent-hub's export bridge.

    ``remote`` is the agent-hub base URL (e.g. ``https://agent-hub.internal``);
    this hits ``GET {remote}/v1/memory/export.cclg`` exactly as
    ``routes_memory_ops.export_memory_scope_cclg_route`` defines it.
    """
    if scope not in _VALID_SCOPES:
        raise PullError(f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope!r}")
    if scope == "agent" and not agent.strip():
        raise PullError("agent is required when scope=agent")

    secret = _pull_secret()
    params = {"tenant": tenant, "scope": scope}
    if agent.strip():
        params["agent"] = agent.strip()
    url = f"{remote.rstrip('/')}/v1/memory/export.cclg?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={_SECRET_HEADER: secret})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - operator-provided remote, not user input from a webpage
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise PullError(f"pull request failed: HTTP {exc.code} {exc.reason}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise PullError(f"pull request failed: {exc.reason}") from exc


def _stamp_scope_org(node: MemoryNode, *, tenant: str) -> None:
    node.scope = {**node.scope, "org": tenant}


def _stamp_provenance(node: MemoryNode, *, remote: str) -> None:
    node.metadata = {**node.metadata, "pulled_from": remote, "pulled_at": now_iso()}


def _nodes_equal(a: MemoryNode, b: MemoryNode) -> bool:
    """Content-equality ignoring the pull's own bookkeeping fields
    (``scope.org``/``metadata.pulled_from``/``metadata.pulled_at``, which a
    re-pull of an already-imported node would otherwise always disagree on)."""
    left, right = a.to_dict(), b.to_dict()
    for value in (left, right):
        value["scope"] = {k: v for k, v in value["scope"].items() if k != "org"}
        value["metadata"] = {k: v for k, v in value["metadata"].items() if k not in {"pulled_from", "pulled_at"}}
        value.pop("updated_at", None)
    return left == right


def import_container(store: CCLGStore, bundle: ContainerBundle, *, tenant: str, remote: str) -> PullSummary:
    """Import a loaded `.cclg` bundle's nodes/patches/edges into ``store``.

    ``bundle`` must already have passed ``load_container(validate=True)`` —
    this function does no additional structural validation, only the
    id-collision / conflict-marking policy described in the module docstring.
    """
    store.init()
    summary = PullSummary(tenant=tenant, scope="", agent="", remote=remote)

    # Maps a remote node id to the *local* id it actually landed under, only
    # populated for the conflict branch (remote content got a fresh id instead
    # of reusing the colliding one). Every non-conflicting import keeps its
    # remote id verbatim, so patches/edges referencing it need no rewrite.
    id_remap: dict[str, str] = {}

    for record in bundle.nodes:
        remote_node = MemoryNode.from_dict(record)
        local_path = store.nodes_dir / f"{remote_node.id}.json"
        if not local_path.exists():
            _stamp_scope_org(remote_node, tenant=tenant)
            _stamp_provenance(remote_node, remote=remote)
            store.write_node(remote_node)
            summary.imported_nodes += 1
            continue

        local_node = store.read_node(remote_node.id)
        candidate = MemoryNode.from_dict(remote_node.to_dict())
        _stamp_scope_org(candidate, tenant=tenant)
        _stamp_provenance(candidate, remote=remote)
        if _nodes_equal(local_node, candidate):
            summary.skipped_nodes += 1
            continue

        # Conflict: never clobber the local node by id. Reassign the remote
        # copy a fresh id, mark the local node conflict_pending (same
        # vocabulary session.merge_session uses for a key collision), and
        # link both ways via relations.contradicts.
        from .models import new_id

        new_local_id = new_id("mem")
        id_remap[remote_node.id] = new_local_id
        candidate.id = new_local_id
        candidate.relations.setdefault("contradicts", []).append(local_node.id)
        store.write_node(candidate)

        local_node.status = "conflict_pending"
        local_node.relations.setdefault("contradicts", []).append(new_local_id)
        local_node.updated_at = now_iso()
        store.write_node(local_node)
        summary.conflict_nodes += 1
        store.append_audit(
            {
                "event": "pull_conflict",
                "local_node_id": local_node.id,
                "remote_node_id": new_local_id,
                "remote": remote,
                "tenant": tenant,
            }
        )

    for record in bundle.patches:
        remote_patch = MemoryPatch.from_dict(record)
        local_path = store.patches_dir / f"{remote_patch.id}.json"
        if local_path.exists():
            summary.skipped_patches += 1
            continue
        remote_patch.target_ids = [id_remap.get(nid, nid) for nid in remote_patch.target_ids]
        remote_patch.new_node_ids = [id_remap.get(nid, nid) for nid in remote_patch.new_node_ids]
        store.write_patch(remote_patch)
        summary.imported_patches += 1

    for record in bundle.edges:
        remote_edge = MemoryEdge.from_dict(record)
        local_path = store.edges_dir / f"{remote_edge.id}.json"
        if local_path.exists():
            summary.skipped_edges += 1
            continue
        remote_edge.from_id = id_remap.get(remote_edge.from_id, remote_edge.from_id)
        remote_edge.to_id = id_remap.get(remote_edge.to_id, remote_edge.to_id)
        store.write_edge(remote_edge)
        summary.imported_edges += 1

    store.append_audit(
        {
            "event": "pull_completed",
            "remote": remote,
            "tenant": tenant,
            "imported_nodes": summary.imported_nodes,
            "skipped_nodes": summary.skipped_nodes,
            "conflict_nodes": summary.conflict_nodes,
            "imported_patches": summary.imported_patches,
            "skipped_patches": summary.skipped_patches,
        }
    )
    return summary


def pull(store: CCLGStore, *, remote: str, tenant: str, scope: str, agent: str = "") -> PullSummary:
    """End-to-end `cclg pull`: fetch, load (fail-closed), import."""
    text = fetch_container_text(remote=remote, tenant=tenant, scope=scope, agent=agent)
    bundle = load_container(text, validate=True)
    summary = import_container(store, bundle, tenant=tenant, remote=remote)
    summary.scope = scope
    summary.agent = agent
    return summary
