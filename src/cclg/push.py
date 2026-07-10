"""`cclg push` — push a local tenant's memory into a Schift agent-hub `.cclg`
import bridge (docs/plans/2026-07-10-cclg-schift-memory-coexistence.md §2.5/S3).

Mirrors ``pull.py``'s network/auth posture exactly (see that module's
docstring for the full rationale): the shared secret is read from the
environment only (``CCLG_PULL_SECRET`` — the same secret pull uses, since both
sides talk to the same agent-hub shared-secret gate), never a CLI flag, never
persisted to ``config.json``.

Import semantics on the *server* side (out of scope for this module, see
agent-hub's ``routes_memory_ops`` once that route ships): a pushed container
lands with ``review_status=pending`` — a local push never auto-promotes to
tenant truth. This module's job ends at "the container was accepted for
review"; it does not poll for approval.

Node selection:

- ``node_ids`` given: exactly those ids, verified present in the local store
  (an unknown id is a hard error, not a silent skip — pushing a container that
  is silently missing a node the caller explicitly asked for would be worse
  than refusing).
- ``node_ids`` omitted: every node whose ``scope.org`` equals the pushing
  ``tenant`` and whose ``status`` is ``"active"`` (superseded/forgotten/
  conflict_pending/etc. nodes are local housekeeping state, not memory this
  tenant should re-import).

Packing reuses ``container.pack_for_export`` unmodified (the same function
`cclg export schift` already uses), so the auth-free guard
(``container._guard_auth_free`` — no ``org_id``/``api_key``/``token``/etc. at
any nesting depth) and schema validation apply identically here: a local node
that somehow picked up a forbidden auth-shaped field is refused at pack time,
before any network call, exactly like a malformed remote container is refused
at pull's load time.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .container import ContainerBundle, load_container, pack_for_export
from .pull import PULL_SECRET_ENV
from .store import CCLGStore

# Same header agent-hub's shared-secret gate expects
# (schift/services/agent-hub/src/agent_hub/api_deps.py:require_shared_secret) —
# duplicated as a plain string (not imported from `.pull`) because it is a
# wire-protocol constant this module also owns, not a `pull`-private detail.
_SECRET_HEADER = "X-Room821-Agent-Hub-Secret"

_VALID_SCOPES = {"core", "agent"}


class PushError(RuntimeError):
    """Raised for a missing secret, an unknown node id, a transport failure,
    an unsupported server response, or (propagated from ``container.py``) a
    ``ContainerError`` — auth-free violation or schema failure at pack time."""


@dataclass(slots=True)
class PushSummary:
    tenant: str
    scope: str
    agent: str
    remote: str
    node_count: int = 0
    patch_count: int = 0
    imported_pending: int = 0
    skipped: int = 0
    raw_response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "scope": self.scope,
            "agent": self.agent,
            "remote": self.remote,
            "node_count": self.node_count,
            "patch_count": self.patch_count,
            "imported_pending": self.imported_pending,
            "skipped": self.skipped,
        }


def _push_secret() -> str:
    import os

    secret = (os.environ.get(PULL_SECRET_ENV) or "").strip()
    if not secret:
        raise PushError(
            f"{PULL_SECRET_ENV} is not set — the shared secret is env-only by policy "
            f"(never a CLI flag, never persisted to config.json). Export "
            f"{PULL_SECRET_ENV} and retry."
        )
    return secret


def select_node_ids(store: CCLGStore, *, tenant: str, node_ids: list[str] | None = None) -> list[str]:
    """Pick the local node ids a push should include.

    Explicit ``node_ids`` are verified to exist and returned verbatim (order
    preserved, no dedup — a caller passing duplicates gets duplicates, which
    ``pack_for_export`` tolerates via its set-based selection). Otherwise every
    ``status == "active"`` node whose ``scope.org == tenant`` is selected.
    """
    store.init()
    if node_ids:
        selected: list[str] = []
        for node_id in node_ids:
            if not (store.nodes_dir / f"{node_id}.json").exists():
                raise PushError(f"node not found in local store: {node_id}")
            selected.append(node_id)
        return selected
    return [node.id for node in store.iter_nodes() if node.scope.get("org") == tenant and node.status == "active"]


def push_container_text(*, remote: str, tenant: str, scope: str, agent: str = "", text: str) -> dict[str, Any]:
    """POST a packed `.cclg` container to agent-hub's import bridge.

    Hits ``POST {remote}/v1/memory/import.cclg`` with ``tenant``/``scope``
    (and ``agent`` when scope is ``"agent"``) as query params, exactly
    mirroring ``pull.fetch_container_text``'s request shape but as a POST
    with the container body as an ``application/octet-stream`` payload.

    A 404 or 501 response is translated into an unambiguous message — the
    route may simply not be deployed yet on the target agent-hub — rather
    than a generic "HTTP 404" the caller has to go interpret.
    """
    if scope not in _VALID_SCOPES:
        raise PushError(f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope!r}")
    if scope == "agent" and not agent.strip():
        raise PushError("agent is required when scope=agent")

    secret = _push_secret()
    params = {"tenant": tenant, "scope": scope}
    if agent.strip():
        params["agent"] = agent.strip()
    url = f"{remote.rstrip('/')}/v1/memory/import.cclg?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        data=text.encode("utf-8"),
        method="POST",
        headers={_SECRET_HEADER: secret, "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - operator-provided remote, not user input from a webpage
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 501):
            raise PushError(
                f"push request failed: HTTP {exc.code} {exc.reason} — 서버가 import.cclg 미지원 "
                f"— agent-hub 배포 필요 ({url})"
            ) from exc
        response_body = exc.read().decode("utf-8", errors="ignore")
        raise PushError(f"push request failed: HTTP {exc.code} {exc.reason}: {response_body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise PushError(f"push request failed: {exc.reason}") from exc

    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def push(
    store: CCLGStore,
    *,
    remote: str,
    tenant: str,
    scope: str,
    agent: str = "",
    node_ids: list[str] | None = None,
) -> PushSummary:
    """End-to-end `cclg push`: select, pack (auth-free guarded), POST, summarize."""
    selected_ids = select_node_ids(store, tenant=tenant, node_ids=node_ids)
    if not selected_ids:
        raise PushError(f"no nodes selected to push (tenant={tenant!r}, scope={scope!r}, explicit_nodes={bool(node_ids)})")

    # Reuses the exact same packer `cclg export schift` uses — the auth-free
    # guard and schema validation inside `pack_container` run here
    # unconditionally, before any network call. A ContainerError here
    # propagates straight out of `push()`, same fail-closed posture pull.py
    # documents for `load_container`.
    text = pack_for_export(store, node_ids=selected_ids)
    bundle: ContainerBundle = load_container(text)

    response = push_container_text(remote=remote, tenant=tenant, scope=scope, agent=agent, text=text)
    summary = PushSummary(
        tenant=tenant,
        scope=scope,
        agent=agent,
        remote=remote,
        node_count=len(bundle.nodes),
        patch_count=len(bundle.patches),
        imported_pending=int(response.get("imported_pending", 0) or 0),
        skipped=int(response.get("skipped", 0) or 0),
        raw_response=response or None,
    )
    store.append_audit(
        {
            "event": "push_completed",
            "remote": remote,
            "tenant": tenant,
            "scope": scope,
            "agent": agent,
            "node_count": summary.node_count,
            "patch_count": summary.patch_count,
            "imported_pending": summary.imported_pending,
            "skipped": summary.skipped,
        }
    )
    return summary
