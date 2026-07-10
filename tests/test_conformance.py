"""Golden conformance suite for the `.cclg` v1 container format
(docs/CCLG_CONTAINER.md §13).

Fixtures under tests/conformance/ are committed, deterministically generated
artifacts (scripts/gen_conformance.py) -- this file only reads them, never
regenerates them. Any implementation of this spec (this repo's Python, or the
TypeScript port in derivatives/schift-ai-memory) MUST reproduce the same
effective-view ids (positive fixtures) or the same error behavior (negative
fixtures) over these exact files to be considered conformant.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cclg.container import ContainerError, load_container
from cclg.patches import UnknownPatchOperationError

FIXTURES_DIR = Path(__file__).resolve().parent / "conformance"


def _load_fixture(name: str) -> tuple[str, dict]:
    text = (FIXTURES_DIR / f"{name}.cclg").read_text(encoding="utf-8")
    expected = json.loads((FIXTURES_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
    return text, expected


class PositiveConformanceFixtureTests(unittest.TestCase):
    """Fixtures 1-6: load + compute effective view, compare against the
    committed expectation."""

    def _assert_effective_view_matches(self, name: str) -> None:
        text, expected = _load_fixture(name)
        bundle = load_container(text)
        view = bundle.effective_view(session_id=expected["session_id"])
        actual_ids = sorted(node.id for node in view)
        self.assertEqual(actual_ids, sorted(expected["effective_view_node_ids"]), expected["description"])

    def test_01_supersede_chain(self) -> None:
        self._assert_effective_view_matches("01_supersede_chain")

    def test_02_create_then_retire(self) -> None:
        self._assert_effective_view_matches("02_create_then_retire")

    def test_03_scope_precedence(self) -> None:
        self._assert_effective_view_matches("03_scope_precedence")

    def test_04_forget_expire_deprecate(self) -> None:
        self._assert_effective_view_matches("04_forget_expire_deprecate")

    def test_05_conflict_pending_resolve(self) -> None:
        self._assert_effective_view_matches("05_conflict_pending_resolve")

    def test_06_rollback_non_retiring(self) -> None:
        self._assert_effective_view_matches("06_rollback_non_retiring")


class NegativeConformanceFixtureTests(unittest.TestCase):
    """Fixtures 7-8: the container must be refused, not silently degraded."""

    def test_07_unknown_patch_operation_errors_at_default_load(self) -> None:
        text, expected = _load_fixture("07_unknown_patch_operation")
        self.assertEqual(expected["expect_error"], "load_default_and_effective_view")

        # validate=True (the default) -- schema validation rejects the
        # unrecognized operation before effective_view is ever reached.
        with self.assertRaises(ContainerError):
            load_container(text)

    def test_07_unknown_patch_operation_errors_at_effective_view_when_validate_false(self) -> None:
        text, _expected = _load_fixture("07_unknown_patch_operation")

        # Bypassing schema validation (as a caller might for a partially
        # untrusted or forward-versioned container) must still fail closed --
        # patches.UnknownPatchOperationError is the last line of defense
        # docs/CCLG_CONTAINER.md §3.1.2 requires.
        bundle = load_container(text, validate=False)
        with self.assertRaises(UnknownPatchOperationError):
            bundle.effective_view()

    def test_08_forbidden_auth_field_errors_at_load_regardless_of_validate(self) -> None:
        text, expected = _load_fixture("08_forbidden_auth_field")
        self.assertEqual(expected["expect_error"], "load_always")

        with self.assertRaises(ContainerError) as ctx_default:
            load_container(text)
        self.assertIn("auth", str(ctx_default.exception))

        with self.assertRaises(ContainerError) as ctx_no_validate:
            load_container(text, validate=False)
        self.assertIn("auth", str(ctx_no_validate.exception))


if __name__ == "__main__":
    unittest.main()
