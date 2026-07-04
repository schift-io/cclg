from __future__ import annotations

import unittest

from cclg.models import MemoryNode, MemoryPatch
from cclg.patches import RETIRING_PATCH_OPERATIONS, SUPERSEDING_OPERATIONS, effective_view


def _active_node(content: str) -> MemoryNode:
    return MemoryNode.create(content=content, source="test:patches")


class RetiringPatchOperationsTests(unittest.TestCase):
    """docs/CCLG_CONTAINER.md §3.1.1 pins this set against the TS mirror's
    `EXCLUDING_PATCH_OPERATIONS`
    (derivatives/schift-ai-memory/packages/core/src/cclg-effective-view.ts) --
    the two MUST stay identical."""

    def test_matches_ts_mirror_excluding_patch_operations_exactly(self) -> None:
        self.assertEqual(
            RETIRING_PATCH_OPERATIONS,
            {
                "update",
                "supersede",
                "refine",
                "expand",
                "narrow",
                "merge",
                "split",
                "resolve_conflict",
                "expire",
                "forget",
                "deprecate",
            },
        )

    def test_is_superset_of_superseding_operations(self) -> None:
        self.assertTrue(SUPERSEDING_OPERATIONS.issubset(RETIRING_PATCH_OPERATIONS))
        self.assertEqual(RETIRING_PATCH_OPERATIONS - SUPERSEDING_OPERATIONS, {"expire", "forget", "deprecate"})

    def test_create_and_rollback_are_not_retiring(self) -> None:
        self.assertNotIn("create", RETIRING_PATCH_OPERATIONS)
        self.assertNotIn("rollback", RETIRING_PATCH_OPERATIONS)


class EffectiveViewPatchesParamTests(unittest.TestCase):
    """`effective_view(nodes, *, session_id=None, patches=None)` -- P3 finding
    repro (unbaked status + patches= closes the gap) plus a hard regression
    guard on the `patches=None` default."""

    def test_supersede_patch_excludes_target_even_when_status_unbaked(self) -> None:
        old = _active_node("old fact")
        new = _active_node("new fact")
        patch = MemoryPatch.create(operation="supersede", target_ids=[old.id], reason="corrected", new_content="new fact")

        # Status-only view (no patches=) still (wrongly) keeps the stale node --
        # this is the exact bug docs/CCLG_CONTAINER.md §3.1.1 now forbids for a
        # conforming *reader*, reproduced here at the pure-function level.
        status_only_ids = {node.id for node in effective_view([old, new])}
        self.assertEqual(status_only_ids, {old.id, new.id})

        patch_aware_ids = {node.id for node in effective_view([old, new], patches=[patch])}
        self.assertEqual(patch_aware_ids, {new.id})

    def test_forget_patch_excludes_target_even_when_status_unbaked(self) -> None:
        old = _active_node("secret note")
        patch = MemoryPatch.create(operation="forget", target_ids=[old.id], reason="user asked to forget")

        self.assertEqual({node.id for node in effective_view([old])}, {old.id})
        self.assertEqual(effective_view([old], patches=[patch]), [])

    def test_expire_and_deprecate_also_exclude(self) -> None:
        expired = _active_node("expiring fact")
        deprecated = _active_node("deprecated fact")
        expire_patch = MemoryPatch.create(operation="expire", target_ids=[expired.id], reason="ttl elapsed")
        deprecate_patch = MemoryPatch.create(operation="deprecate", target_ids=[deprecated.id], reason="no longer relevant")

        view = effective_view([expired, deprecated], patches=[expire_patch, deprecate_patch])
        self.assertEqual(view, [])

    def test_create_and_rollback_patches_do_not_exclude(self) -> None:
        node = _active_node("still active")
        create_patch = MemoryPatch.create(operation="create", target_ids=[], reason="new fact", new_content="still active")
        rollback_patch = MemoryPatch.create(operation="rollback", target_ids=[node.id], reason="undo")

        self.assertEqual({n.id for n in effective_view([node], patches=[create_patch])}, {node.id})
        self.assertEqual({n.id for n in effective_view([node], patches=[rollback_patch])}, {node.id})

    def test_patches_none_default_is_byte_for_byte_unchanged(self) -> None:
        """Regression: every pre-existing call site (agent-hub's
        cclg_grounding.py/pack.py, patches.active_nodes()) omits patches= and
        must see identical output before and after this change."""
        active = _active_node("untouched")
        superseded = _active_node("stale")
        superseded.status = "superseded"

        without_kwarg = effective_view([active, superseded])
        with_explicit_none = effective_view([active, superseded], patches=None)
        with_empty_list = effective_view([active, superseded], patches=[])

        self.assertEqual([n.id for n in without_kwarg], [n.id for n in with_explicit_none])
        self.assertEqual([n.id for n in without_kwarg], [n.id for n in with_empty_list])
        self.assertEqual({n.id for n in without_kwarg}, {active.id})

    def test_patches_param_does_not_affect_scope_precedence_ordering(self) -> None:
        """patches= only ever removes candidates before scope-precedence
        resolution runs; it must not change _resolve_scope_precedence's
        keyed-node winner logic for nodes it doesn't touch."""
        project = _active_node("project scoped")
        project.key = "k"
        project.scope["project"] = "r"
        global_node = _active_node("global scoped")
        global_node.key = "k"

        view = effective_view([project, global_node], patches=[])
        contents = {n.content for n in view}
        self.assertIn("project scoped", contents)
        self.assertNotIn("global scoped", contents)


if __name__ == "__main__":
    unittest.main()
