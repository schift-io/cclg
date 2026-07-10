from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from cclg.container import ContainerError, load_container
from cclg.models import MemoryNode
from cclg.pull import PULL_SECRET_ENV
from cclg.push import PushError, push, push_container_text, select_node_ids
from cclg.store import CCLGStore


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _seeded_store(tmp: str) -> tuple[CCLGStore, MemoryNode, MemoryNode]:
    store = CCLGStore(Path(tmp))
    store.init()
    acme_node = MemoryNode.create(content="Q3 revenue target is 5B KRW.", source="cli:add", scope={"org": "acme"})
    other_node = MemoryNode.create(content="Unrelated tenant fact.", source="cli:add", scope={"org": "other-tenant"})
    store.write_node(acme_node)
    store.write_node(other_node)
    return store, acme_node, other_node


class SelectNodeIdsTests(unittest.TestCase):
    def test_no_explicit_nodes_selects_active_nodes_matching_tenant_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, acme_node, other_node = _seeded_store(tmp)
            selected = select_node_ids(store, tenant="acme")
            self.assertEqual(selected, [acme_node.id])
            self.assertNotIn(other_node.id, selected)

    def test_non_active_status_is_excluded_from_scope_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, acme_node, _other_node = _seeded_store(tmp)
            acme_node.status = "conflict_pending"
            store.write_node(acme_node)
            self.assertEqual(select_node_ids(store, tenant="acme"), [])

    def test_explicit_node_ids_are_verified_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, acme_node, other_node = _seeded_store(tmp)
            # Explicit selection can cross tenant scope filters and status —
            # the caller asked for this id specifically.
            selected = select_node_ids(store, tenant="acme", node_ids=[other_node.id])
            self.assertEqual(selected, [other_node.id])

    def test_explicit_unknown_node_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _acme_node, _other_node = _seeded_store(tmp)
            with self.assertRaises(PushError):
                select_node_ids(store, tenant="acme", node_ids=["mem_doesnotexist"])


class PushContainerTextTests(unittest.TestCase):
    def test_missing_secret_raises_push_error(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(PushError):
                push_container_text(remote="https://agent-hub.internal", tenant="acme", scope="core", text="body")

    def test_invalid_scope_raises_before_any_network_call(self) -> None:
        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with self.assertRaises(PushError):
                push_container_text(remote="https://agent-hub.internal", tenant="acme", scope="session", text="body")

    def test_agent_scope_requires_agent_id(self) -> None:
        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with self.assertRaises(PushError):
                push_container_text(remote="https://agent-hub.internal", tenant="acme", scope="agent", text="body")

    def test_sends_shared_secret_header_query_params_and_body(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            captured["method"] = request.get_method()
            captured["data"] = request.data
            return _FakeResponse(b'{"imported_pending": 2, "skipped": 0}')

        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with mock.patch("cclg.push.urllib.request.urlopen", fake_urlopen):
                response = push_container_text(
                    remote="https://agent-hub.internal/",
                    tenant="acme",
                    scope="agent",
                    agent="cs-bot",
                    text="container body",
                )

        self.assertEqual(response, {"imported_pending": 2, "skipped": 0})
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["data"], b"container body")
        self.assertIn("tenant=acme", captured["url"])
        self.assertIn("scope=agent", captured["url"])
        self.assertIn("agent=cs-bot", captured["url"])
        self.assertTrue(str(captured["url"]).startswith("https://agent-hub.internal/v1/memory/import.cclg?"))
        self.assertEqual(captured["headers"].get("X-room821-agent-hub-secret"), "s3cr3t")

    def test_404_becomes_route_not_deployed_message(self) -> None:
        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, io.BytesIO(b""))

        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with mock.patch("cclg.push.urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(PushError) as ctx:
                    push_container_text(remote="https://agent-hub.internal", tenant="acme", scope="core", text="body")
        self.assertIn("import.cclg 미지원", str(ctx.exception))

    def test_other_http_error_becomes_generic_push_error(self) -> None:
        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, io.BytesIO(b"nope"))

        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with mock.patch("cclg.push.urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(PushError) as ctx:
                    push_container_text(remote="https://agent-hub.internal", tenant="acme", scope="core", text="body")
        self.assertIn("401", str(ctx.exception))


class PushEndToEndTests(unittest.TestCase):
    def test_push_selects_packs_posts_and_summarizes(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            captured["data"] = request.data
            return _FakeResponse(b'{"imported_pending": 1, "skipped": 0}')

        with tempfile.TemporaryDirectory() as tmp:
            store, acme_node, other_node = _seeded_store(tmp)
            with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
                with mock.patch("cclg.push.urllib.request.urlopen", fake_urlopen):
                    summary = push(store, remote="https://agent-hub.internal", tenant="acme", scope="core")

        self.assertEqual(summary.tenant, "acme")
        self.assertEqual(summary.node_count, 1)
        self.assertEqual(summary.imported_pending, 1)
        self.assertEqual(summary.skipped, 0)

        posted_text = captured["data"].decode("utf-8")
        bundle = load_container(posted_text)
        self.assertEqual([node["id"] for node in bundle.nodes], [acme_node.id])
        self.assertNotIn(other_node.id, [node["id"] for node in bundle.nodes])

    def test_push_with_no_matching_nodes_raises_before_any_network_call(self) -> None:
        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            raise AssertionError("network call must not happen when there is nothing to push")

        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
                with mock.patch("cclg.push.urllib.request.urlopen", fake_urlopen):
                    with self.assertRaises(PushError):
                        push(store, remote="https://agent-hub.internal", tenant="acme", scope="core")

    def test_push_refuses_node_with_forbidden_auth_field(self) -> None:
        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            raise AssertionError("network call must not happen when the auth-free guard rejects a node")

        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            node = MemoryNode.create(content="Has a forbidden field.", source="cli:add", scope={"org": "acme"})
            record = node.to_dict()
            # Simulate a local node that somehow picked up a platform-auth-shaped
            # field (docs/CCLG_CONTAINER.md §3.2's FORBIDDEN_AUTH_KEYS) — the
            # auth-free guard inside `pack_for_export`/`pack_container` must
            # reject this at pack time, before any network call.
            record["metadata"] = {**record["metadata"], "api_key": "sk-leaked"}
            store._write_json(store.nodes_dir / f"{node.id}.json", record)

            with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
                with mock.patch("cclg.push.urllib.request.urlopen", fake_urlopen):
                    with self.assertRaises(ContainerError):
                        push(store, remote="https://agent-hub.internal", tenant="acme", scope="core")

    def test_missing_secret_raised_before_pack(self) -> None:
        # Reordering-sensitive: today's implementation packs before checking the
        # secret, which is fine functionally (no network call happens either
        # way) but this test pins the actual failure mode so a future refactor
        # doesn't silently start leaking pack-time errors before the auth check
        # would have fired.
        with tempfile.TemporaryDirectory() as tmp:
            store, _acme_node, _other_node = _seeded_store(tmp)
            with mock.patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(PushError):
                    push(store, remote="https://agent-hub.internal", tenant="acme", scope="core")


if __name__ == "__main__":
    unittest.main()
