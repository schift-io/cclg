from __future__ import annotations

import unittest

from cclg.container import ContainerBundle
from cclg.gatemem_adapter import EpisodePolicyState, ProjectDirectory
from cclg.gatemem_pack import (
    AgentCheckpointView,
    FORGOTTEN_OVERRIDE_MIN_RATIO,
    build_policy_pack,
    _forgotten_veto_overridden,
)
from cclg.models import MemoryNode
from cclg.retrieval import SearchHit


def _hit(score: float) -> SearchHit:
    node = MemoryNode.create(content="placeholder content for a search hit", source="test:gatemem_pack")
    return SearchHit(node=node, score=score, reasons=[])


class ForgottenVetoOverriddenTests(unittest.TestCase):
    """Pins ``_forgotten_veto_overridden``'s exact two-condition contract (see
    the module comment above ``_RETROSPECTIVE_MARKER_RE`` in gatemem_pack.py
    for the full rationale/measurements): both retrospective-language absence
    *and* a >``FORGOTTEN_OVERRIDE_MIN_RATIO``x authorized/forgotten top-score
    ratio are required, or the safety-first veto stands."""

    def test_no_hits_never_overrides(self) -> None:
        self.assertFalse(_forgotten_veto_overridden("what is the current budget?", [], []))
        self.assertFalse(_forgotten_veto_overridden("what is the current budget?", [_hit(50.0)], []))
        self.assertFalse(_forgotten_veto_overridden("what is the current budget?", [], [_hit(5.0)]))

    def test_overrides_when_marker_absent_and_ratio_dominates(self) -> None:
        # authorized top (50) is 10x forgotten top (5) and the query names no
        # retrospective language -- the classic "incidental collision"
        # utility false positive this fix targets.
        self.assertTrue(_forgotten_veto_overridden("What is the current incident diagnosis?", [_hit(50.0)], [_hit(5.0)]))

    def test_retrospective_marker_blocks_override_even_with_huge_ratio(self) -> None:
        for marker_query in [
            "What was the old staging token before it was retired?",
            "Remind me what the previous customer name used to be.",
            "Has anything been deleted or removed from this project's history?",
        ]:
            self.assertFalse(
                _forgotten_veto_overridden(marker_query, [_hit(200.0)], [_hit(1.0)]),
                msg=f"marker query should never override: {marker_query!r}",
            )

    def test_ratio_at_or_below_threshold_does_not_override(self) -> None:
        forgotten = _hit(10.0)
        # Exactly at the threshold multiple: must NOT override (strict >).
        at_threshold = _hit(10.0 * FORGOTTEN_OVERRIDE_MIN_RATIO)
        self.assertFalse(_forgotten_veto_overridden("what is the current status?", [at_threshold], [forgotten]))
        # Just over the threshold: must override.
        over_threshold = _hit(10.0 * FORGOTTEN_OVERRIDE_MIN_RATIO + 0.01)
        self.assertTrue(_forgotten_veto_overridden("what is the current status?", [over_threshold], [forgotten]))


def _policy_state(project_id: str, principal_id: str) -> EpisodePolicyState:
    directory = ProjectDirectory(
        project_ids=[project_id],
        aliases={project_id: "harbor"},
        principals_by_project={project_id: {principal_id}},
        display_names={principal_id: "Test Principal"},
        all_principal_ids={principal_id},
        project_label_terms=frozenset({"harbor", "project harbor"}),
    )
    return EpisodePolicyState(directory=directory)


def _node(content: str, *, status: str = "active", project_ids: list[str] | None = None, speaker: str = "asker_1") -> MemoryNode:
    node = MemoryNode.create(content=content, source="test:gatemem_pack")
    node.status = status
    node.metadata = {
        "created_by": "gatemem_office_adapter",
        "review_status": "auto_applied",
        "privacy": "local_default",
        "gatemem": {
            "episode_id": "ep1",
            "turn_id": "t1",
            "speaker_principal_id": speaker,
            "speaker_role": "member",
            "project_ids": list(project_ids or []),
        },
    }
    return node


class BuildPolicyPackForgottenOverrideTests(unittest.TestCase):
    """End-to-end (``build_policy_pack``) pin for the utility false-positive
    this fix resolves, and for the two scenarios that must keep the
    safety-first veto: a genuine retrospective query, and a low-dominance
    forgotten hit."""

    PROJECT = "project_harbor"
    ASKER = "asker_1"

    def _pack(self, nodes: list[MemoryNode], query_text: str):
        bundle = ContainerBundle(header={}, nodes=[node.to_dict() for node in nodes])
        policy_state = _policy_state(self.PROJECT, self.ASKER)
        view = AgentCheckpointView(
            checkpoint_id="ckpt_1",
            episode_id="ep1",
            as_of_turn_id="t9",
            asker_principal_id=self.ASKER,
            asker_role="member",
            query_text=query_text,
        )
        return build_policy_pack(bundle, view, policy_state)

    def test_incidental_forgotten_collision_no_longer_blocks_a_clear_answer(self) -> None:
        nodes = [
            _node(
                "The current leading Harbor incident diagnosis is stale environment-variable sync on the staging worker group.",
                project_ids=[self.PROJECT],
            ),
            _node(
                "Incident bridge note: the staging token currently in use for the Harbor worker test path is hb_stg_1A2B-3C4D-5E6F. Treat it as sensitive operational material.",
                status="forgotten",
                project_ids=[self.PROJECT],
            ),
        ]
        pack = self._pack(nodes, "What is the current leading Harbor incident diagnosis?")
        self.assertEqual(pack.action, "answer")
        self.assertIn(nodes[0].id, pack.exposed_node_ids)

    def test_retrospective_query_still_yields_no_memory(self) -> None:
        nodes = [
            _node(
                "The current leading Harbor incident diagnosis is stale environment-variable sync on the staging worker group.",
                project_ids=[self.PROJECT],
            ),
            _node(
                "Incident bridge note: the staging token currently in use for the Harbor worker test path is hb_stg_1A2B-3C4D-5E6F. Treat it as sensitive operational material.",
                status="forgotten",
                project_ids=[self.PROJECT],
            ),
        ]
        pack = self._pack(nodes, "What was the previous, now-deleted Harbor staging token value?")
        self.assertEqual(pack.action, "no_memory")
        self.assertEqual(pack.exposed_node_ids, [])

    def test_low_dominance_forgotten_hit_still_vetoes(self) -> None:
        # Authorized content barely edges out the forgotten hit (well under
        # FORGOTTEN_OVERRIDE_MIN_RATIO) -- not confident enough to override.
        nodes = [
            _node("Harbor status chatter continues as scheduled this week.", project_ids=[self.PROJECT]),
            _node(
                "Harbor status update: the exact customer mapping should now be treated as unavailable and must not be retained.",
                status="forgotten",
                project_ids=[self.PROJECT],
            ),
        ]
        pack = self._pack(nodes, "Harbor status update, please.")
        self.assertEqual(pack.action, "no_memory")
        self.assertEqual(pack.exposed_node_ids, [])


if __name__ == "__main__":
    unittest.main()
