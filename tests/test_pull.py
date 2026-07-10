from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from cclg.container import ContainerBundle, pack_container
from cclg.models import MemoryNode, MemoryPatch
from cclg.pull import PULL_SECRET_ENV, PullError, fetch_container_text, import_container, pull
from cclg.store import CCLGStore


def _remote_container(*, node_content: str = "Q3 revenue target is 5B KRW.") -> str:
    node = MemoryNode.create(content=node_content, source="agent-hub:core", node_type="project_decision")
    return pack_container([node], [])


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FetchContainerTests(unittest.TestCase):
    def test_missing_secret_raises_pull_error(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(PullError):
                fetch_container_text(remote="https://agent-hub.internal", tenant="acme", scope="core")

    def test_invalid_scope_raises_before_any_network_call(self) -> None:
        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with self.assertRaises(PullError):
                fetch_container_text(remote="https://agent-hub.internal", tenant="acme", scope="session")

    def test_agent_scope_requires_agent_id(self) -> None:
        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with self.assertRaises(PullError):
                fetch_container_text(remote="https://agent-hub.internal", tenant="acme", scope="agent")

    def test_sends_shared_secret_header_and_query_params(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=30):  # noqa: ANN001 - matches urllib.request.urlopen signature loosely
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            return _FakeResponse(b"container body")

        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with mock.patch("cclg.pull.urllib.request.urlopen", fake_urlopen):
                text = fetch_container_text(remote="https://agent-hub.internal/", tenant="acme", scope="agent", agent="cs-bot")

        self.assertEqual(text, "container body")
        self.assertIn("tenant=acme", captured["url"])
        self.assertIn("scope=agent", captured["url"])
        self.assertIn("agent=cs-bot", captured["url"])
        self.assertTrue(str(captured["url"]).startswith("https://agent-hub.internal/v1/memory/export.cclg?"))
        # urllib.request.Request lower-cases/title-cases header keys internally.
        self.assertEqual(captured["headers"].get("X-room821-agent-hub-secret"), "s3cr3t")

    def test_http_error_becomes_pull_error(self) -> None:
        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, io.BytesIO(b"nope"))

        with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
            with mock.patch("cclg.pull.urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(PullError):
                    fetch_container_text(remote="https://agent-hub.internal", tenant="acme", scope="core")


class ImportContainerTests(unittest.TestCase):
    def test_fresh_import_stamps_scope_org_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            text = _remote_container()
            from cclg.container import load_container

            bundle = load_container(text)
            summary = import_container(store, bundle, tenant="acme", remote="https://agent-hub.internal")

            self.assertEqual(summary.imported_nodes, 1)
            self.assertEqual(summary.skipped_nodes, 0)
            self.assertEqual(summary.conflict_nodes, 0)

            nodes = list(store.iter_nodes())
            self.assertEqual(len(nodes), 1)
            node = nodes[0]
            self.assertEqual(node.scope["org"], "acme")
            self.assertEqual(node.metadata["pulled_from"], "https://agent-hub.internal")
            self.assertEqual(node.source["label"], "agent-hub:core")

    def test_re_pull_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()
            from cclg.container import load_container

            text = _remote_container()
            bundle1 = load_container(text)
            import_container(store, bundle1, tenant="acme", remote="https://agent-hub.internal")

            bundle2 = load_container(text)
            summary2 = import_container(store, bundle2, tenant="acme", remote="https://agent-hub.internal")

            self.assertEqual(summary2.imported_nodes, 0)
            self.assertEqual(summary2.skipped_nodes, 1)
            self.assertEqual(len(list(store.iter_nodes())), 1)

    def test_conflicting_same_id_marks_local_conflict_pending_and_links_contradicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()

            shared_node = MemoryNode.create(content="Q3 revenue target is 5B KRW.", source="agent-hub:core")
            local_container_text = pack_container([shared_node], [])
            from cclg.container import load_container

            bundle = load_container(local_container_text)
            import_container(store, bundle, tenant="acme", remote="https://agent-hub.internal")

            # Now the remote diverges: same node id, different content.
            diverged = MemoryNode.from_dict(shared_node.to_dict())
            diverged.content = "Q3 revenue target is 8B KRW."
            diverged_container_text = pack_container([diverged], [])
            diverged_bundle = load_container(diverged_container_text)

            summary = import_container(store, diverged_bundle, tenant="acme", remote="https://agent-hub.internal")

            self.assertEqual(summary.conflict_nodes, 1)
            self.assertEqual(summary.imported_nodes, 0)
            self.assertEqual(summary.skipped_nodes, 0)

            nodes = {node.id: node for node in store.iter_nodes()}
            self.assertEqual(len(nodes), 2)
            local_node = nodes[shared_node.id]
            self.assertEqual(local_node.status, "conflict_pending")
            self.assertEqual(local_node.content, "Q3 revenue target is 5B KRW.")

            remote_copies = [node for node in nodes.values() if node.id != shared_node.id]
            self.assertEqual(len(remote_copies), 1)
            remote_copy = remote_copies[0]
            self.assertEqual(remote_copy.content, "Q3 revenue target is 8B KRW.")
            self.assertIn(shared_node.id, remote_copy.relations["contradicts"])
            self.assertIn(remote_copy.id, local_node.relations["contradicts"])

    def test_patches_are_appended_and_deduped_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            store.init()

            node = MemoryNode.create(content="Original fact.", source="agent-hub:core")
            patch = MemoryPatch.create(
                operation="supersede",
                target_ids=[node.id],
                reason="schift-memory correction",
                new_content="Corrected fact.",
                source="agent-hub",
            )
            # A container that names a patch referencing this node, without a
            # replacement node present, is a valid schema-level export shape
            # (agent-hub's export bridge always packs the already-superseded
            # node with baked status, but the patch ledger entry stands alone
            # here for a focused unit test of append/skip behavior).
            node.status = "superseded"
            text = pack_container([node], [patch])
            from cclg.container import load_container

            bundle = load_container(text)
            summary = import_container(store, bundle, tenant="acme", remote="https://agent-hub.internal")
            self.assertEqual(summary.imported_patches, 1)
            self.assertEqual(list(store.iter_patches())[0].id, patch.id)

            summary2 = import_container(store, bundle, tenant="acme", remote="https://agent-hub.internal")
            self.assertEqual(summary2.imported_patches, 0)
            self.assertEqual(summary2.skipped_patches, 1)
            self.assertEqual(len(list(store.iter_patches())), 1)


class PullEndToEndTests(unittest.TestCase):
    def test_pull_fetches_loads_and_imports(self) -> None:
        text = _remote_container()

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeResponse(text.encode("utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            store = CCLGStore(Path(tmp))
            with mock.patch.dict("os.environ", {PULL_SECRET_ENV: "s3cr3t"}):
                with mock.patch("cclg.pull.urllib.request.urlopen", fake_urlopen):
                    summary = pull(store, remote="https://agent-hub.internal", tenant="acme", scope="core")

            self.assertEqual(summary.imported_nodes, 1)
            self.assertEqual(summary.tenant, "acme")
            self.assertEqual(summary.scope, "core")


if __name__ == "__main__":
    unittest.main()
