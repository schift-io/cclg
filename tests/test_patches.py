from __future__ import annotations

import json
import unittest
from pathlib import Path

from cclg.models import MemoryNode, MemoryPatch
from cclg.patches import (
    RETIRING_CANDIDATE_OPERATIONS,
    RETIRING_PATCH_OPERATIONS,
    SUPERSEDING_OPERATIONS,
    classify_patch,
    detect_patch_candidates,
    effective_view,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


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


class WeakSignalClassificationTests(unittest.TestCase):
    """Weak temporal adverbs (이제/지금) must not, by themselves, retire an
    unrelated target on generic-noun overlap alone -- the real-corpus false
    positive documented in agent-hub tests/test_cclg_grounding.py
    ``test_apply_corrections_false_positive_neutral_temporal_utterance``."""

    def test_bare_weak_temporal_word_alone_is_not_enough_to_retire(self) -> None:
        old = _active_node("회의는 내일 3시다")
        candidates = detect_patch_candidates("이제 회의는 다른 걸로 잡자", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_classify_patch_still_labels_weak_temporal_as_update(self) -> None:
        """classify_patch() itself is a context-free text classifier and is
        unchanged -- the gating lives in detect_patch_candidates(), which
        knows about specific target nodes."""
        self.assertEqual(classify_patch("이제 회의는 다른 걸로 잡자"), "update")

    def test_weak_temporal_with_concrete_value_overlap_still_detected(self) -> None:
        """A weak-only turn is not blanket-suppressed -- if it names a
        concrete value shared with a specific node, it is still promoted."""
        target = _active_node("회의는 오늘 3시다")
        candidates = detect_patch_candidates("이제 3시 회의를 4시로 옮기자", [target])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], target.id)

    def test_weak_temporal_question_with_value_still_suppressed(self) -> None:
        """Hardest case: a weak-only turn with a concrete value overlap that
        is phrased as a question must still be suppressed."""
        target = _active_node("회의는 내일 3시다")
        candidates = detect_patch_candidates("지금 3시인데 회의 언제 시작해?", [target])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_strong_correction_trigger_does_not_require_concrete_overlap(self) -> None:
        """A genuinely explicit correction must not regress just because it
        doesn't restate the old value verbatim -- only weak-only turns are
        held to the concrete-overlap bar."""
        old = _active_node("매출은 5억원이다")
        distractor = _active_node("회의는 내일 3시다")
        candidates = detect_patch_candidates("정정할게, 매출은 12억이야", [old, distractor])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)
        self.assertNotIn(distractor.id, {c["target_id"] for c in retiring})

    def test_strong_temporal_trigger_is_not_weak_only(self) -> None:
        """'앞으로'/'더 이상' etc. are forward-looking policy-change language,
        distinct from the bare 'now' filler adverbs -- they must not be
        held to the concrete-overlap bar either."""
        old = _active_node("회의는 매주 월요일이다")
        candidates = detect_patch_candidates("앞으로 회의는 매주 화요일로 바꿔", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)


class Round2FalsePositiveFamilyTests(unittest.TestCase):
    """Round-2 additions: conditional/hypothetical suppression (계열A),
    value-coincidence gating (계열B), and bare '아니' hedge demotion
    (계열C) -- see the ``_looks_like_conditional``/``_has_value_contradiction``/
    ``WEAK_NEGATION_TRIGGERS`` docstrings in ``cclg.patches`` for the full
    rationale. The aggregate precision/recall gate lives in
    ``CorrectionsEvalSetTests``; these pin the individual mechanisms by name
    so a future change that breaks one in isolation fails close to the cause."""

    def test_conditional_marker_suppresses_even_a_weak_trigger_with_differing_value(self) -> None:
        """'혹시 ... 넘으면' names a genuinely different value ('10억' vs.
        the node's '5억') that the value-contradiction gate alone would
        wrongly pass -- only the conditional gate stops it."""
        old = _active_node("매출은 5억원이다")
        candidates = detect_patch_candidates("혹시 이제 매출이 10억을 넘으면 인센티브 얘기하자", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_mid_clause_conditional_without_explicit_marker_still_suppresses(self) -> None:
        """No '만약'/'혹시' marker at all -- only the mid-clause '-면'
        connective ('앞당겨지면') signals this is a hypothetical, not an
        assertion; also names a differing value ('2시' vs. '3시')."""
        old = _active_node("회의는 오늘 3시다")
        candidates = detect_patch_candidates("이제 회의 시간이 앞당겨지면 2시로 확정하자", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_conditional_marker_does_not_suppress_when_explicit_correction_idiom_present(self) -> None:
        """The conditional gate has a carve-out: a turn that also carries an
        explicit correction idiom (CORRECTION_TRIGGERS) is not suppressed
        just because it happens to contain '혹시'."""
        old = _active_node("예산은 2천만원이다")
        candidates = detect_patch_candidates("혹시나 해서 정정할게, 예산은 3천만원이야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)

    def test_value_coincidence_alone_does_not_retire(self) -> None:
        """The turn's only concrete value ('3시') is the SAME as the node's
        -- coincidental repetition, not a contradiction."""
        old = _active_node("회의는 내일 3시다")
        candidates = detect_patch_candidates("이제 3시 다 됐네, 슬슬 준비하자", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_differing_value_in_same_category_still_retires(self) -> None:
        """Sanity check that the value-coincidence gate doesn't overcorrect:
        a genuinely differing value in the same category ('2시' -> '3시')
        must still retire."""
        old = _active_node("발표는 오늘 2시다")
        candidates = detect_patch_candidates("발표 2시 아니지, 3시로 바뀌었어", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)

    def test_bare_ani_hedge_with_no_value_does_not_retire(self) -> None:
        """Bare '아니야' hedges a confirmation status, not the underlying
        value -- demoted out of CORRECTION_TRIGGERS, gated like a weak
        temporal adverb, and correctly suppressed here (no concrete value in
        the turn at all)."""
        old = _active_node("예산은 1200만원이다")
        candidates = detect_patch_candidates("이번 예산은 확정된 게 아니야, 아직 논의 중이야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_anira_construction_stays_a_strong_trigger(self) -> None:
        """'A가 아니라 B' is structurally different from a bare sentence-final
        '아니' hedge (it requires a following replacement clause) and stays a
        strong, ungated CORRECTION_TRIGGERS entry."""
        old = _active_node("회의는 오늘 3시다")
        candidates = detect_patch_candidates("회의 3시가 아니라 4시야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)

    def test_reported_speech_particle_recall_backstop(self) -> None:
        """'이철수라고' and the node's '이철수이다' share no lexical token at
        all under retrieval's tokenizer -- the patches.py-only supplemental
        matcher bridges the reported-speech particle."""
        old = _active_node("담당자 이름은 이철수이다")
        candidates = detect_patch_candidates("정정할게, 이철수라고 알려줬었는데 실제로는 박영희야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)

    def test_month_day_vs_iso_date_recall_backstop(self) -> None:
        """'7월 3일' and the node's ISO '2026-07-03' share no lexical token
        at all -- the patches.py-only supplemental matcher normalizes both to
        a comparable month-day key."""
        old = _active_node("마감일: 2026-07-03")
        candidates = detect_patch_candidates("정정할게, 마감일이 7월 3일이라고 했는데 사실 7월 10일이야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)


class Round3FalsePositiveFamilyTests(unittest.TestCase):
    """Round-3 additions: the "아니라" lexeme-boundary fix for the fused
    conditional/concessive and reported-speech lexemes (기계적 버그 1),
    third-party reported-speech suppression (기계적 버그 2), the enumeration
    (나열) guard on value-contradiction (기계적 버그 3), and Korean-numeral/
    comma canonicalization before value comparison (기계적 버그 4) -- see the
    ``_true_anira_correction``/``_contains_correction_trigger``/
    ``_looks_like_reported_speech``/``_has_value_contradiction``/
    ``_canonical_value`` docstrings in ``cclg.patches`` for the full
    rationale. The aggregate precision/recall gate lives in
    ``CorrectionsEvalSetTests``; these pin the individual mechanisms by name
    so a future change that breaks one in isolation fails close to the
    cause."""

    # --- 기계적 버그 1: "아니라" lexeme boundary -----------------------------

    def test_anira_inside_mid_clause_conditional_does_not_retire(self) -> None:
        """The exact reported false positive: '아니라면' is the fused
        conditional connective, not the 'A가 아니라 B' correction
        construction. Plain substring matching on '아니라' used to make the
        conditional-gate's own "has an explicit correction idiom" exception
        self-satisfying, defeating `_looks_like_conditional`'s suppression."""
        old = _active_node("회의는 오늘 3시다")
        candidates = detect_patch_candidates("회의가 3시가 아니라면 그냥 그대로 진행하자", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_anira_inside_reported_speech_does_not_retire(self) -> None:
        """'아니라고' is the reported-speech particle ('said that it's not
        X'), a third-party attribution, not a first-person correction."""
        old = _active_node("매출은 5억원이다")
        candidates = detect_patch_candidates("그가 5억이 아니라고 했어", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_anira_myeo_and_ani_variants_do_not_retire(self) -> None:
        """'아니라며'/'아니라니' -- fused suffixes distinct from '아니라면',
        also excluded by the same lexeme-boundary fix."""
        old = _active_node("예산은 3천만원이다")
        candidates = detect_patch_candidates("김대리가 예산은 3천만원이 아니라며 되물었다", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_anira_construction_with_trailing_comma_still_retires(self) -> None:
        """Boundary sanity check: a genuine 'A가 아니라 B' correction directly
        followed by a comma (no space) must still retire -- the lexeme fix
        only excludes fused Hangul continuations (면/며/니/면서/고), never
        ordinary punctuation."""
        old = _active_node("발표는 오늘 2시다")
        candidates = detect_patch_candidates("발표는 2시가 아니라, 3시야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)

    # --- 기계적 버그 2: 전달화법 억제 ----------------------------------------

    def test_third_party_report_with_independent_weak_negation_does_not_retire(self) -> None:
        """The reported-speech marker here is independent of the '아니라'
        lexeme fix -- it co-occurs with bare WEAK_NEGATION_TRIGGERS '아니고',
        which names a genuinely differing value that would otherwise clear
        `_has_value_contradiction` and wrongly retire."""
        old = _active_node("예산은 1200만원이다")
        candidates = detect_patch_candidates("철수가 예산은 1200만원 아니고 1500만원이라고 하더라", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_third_party_report_with_independent_weak_temporal_does_not_retire(self) -> None:
        """WEAK_TEMPORAL_TRIGGERS' '이제' plus a reported-speech marker
        attributing a genuinely differing value to a third party -- the
        value-contradiction gate alone would wrongly pass this."""
        old = _active_node("회의는 오늘 3시다")
        candidates = detect_patch_candidates("김대리가 이제 회의는 4시라고 전했어", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_first_person_correction_overrides_reported_speech_suppression(self) -> None:
        """The reported-speech suppression only ever gates weak-only
        candidates (`weak_only` in `detect_patch_candidates`); an explicit
        first-person correction idiom is a strong trigger and is never held
        to it."""
        old = _active_node("매출은 5억원이다")
        candidates = detect_patch_candidates("아까 내가 매출을 5억이라고 했는데 잘못 말했어, 사실 12억이야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)

    # --- 기계적 버그 3: 나열 가드 --------------------------------------------

    def test_enumeration_of_three_candidates_does_not_retire(self) -> None:
        """Three money candidates listed for a still-undecided figure --
        none singled out as 'the' new value."""
        old = _active_node("예산은 1500만원이다")
        candidates = detect_patch_candidates("이제 예산 후보가 1200만원, 1500만원, 1800만원 중에 정해야해", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_enumeration_where_one_candidate_matches_node_still_does_not_retire(self) -> None:
        """Even when one enumerated candidate happens to equal the node's
        current value, 2+ *differing* candidates remain -- still ambiguous,
        still not a contradiction. Proves the 나열 가드 counts differing
        values, not raw turn-value count."""
        old = _active_node("참석 인원은 5명이다")
        candidates = detect_patch_candidates("이제 참석 인원이 3명, 5명, 7명 중에 결정될 예정이야", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_two_value_swap_is_not_treated_as_enumeration(self) -> None:
        """Sanity check the 나열 가드 doesn't overcorrect: an old-vs-new
        value swap (2 values, one matching the node) still retires -- must
        not regress round-2's
        ``test_weak_temporal_with_concrete_value_overlap_still_detected``."""
        old = _active_node("회의는 오늘 3시다")
        candidates = detect_patch_candidates("이제 3시 회의를 4시로 옮기자", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)

    # --- 기계적 버그 4: 수치 정규화 ------------------------------------------

    def test_comma_grouped_value_equal_to_eok_unit_does_not_retire(self) -> None:
        """'500,000,000원' and '5억원' name the exact same amount but share
        no literal substring -- canonicalization must recognize them as
        equal, not a contradiction."""
        old = _active_node("매출은 5억원이다")
        candidates = detect_patch_candidates("이제 매출은 500,000,000원 정도 되려나", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_comma_grouped_value_equal_to_man_unit_does_not_retire(self) -> None:
        """'12,000,000원' (comma-grouped, bare 원 unit) canonicalizes to the
        same integer as '1200만원' (만-multiplier form)."""
        old = _active_node("매출은 1200만원이다")
        candidates = detect_patch_candidates("이제 매출이 12,000,000원 근처인 것 같아", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertEqual(retiring, [])

    def test_canonicalization_does_not_mask_genuine_money_contradiction(self) -> None:
        """Sanity check: canonicalization must not accidentally equate two
        genuinely different amounts -- a 1-won difference from the node's
        canonical value must still be detected as a contradiction."""
        old = _active_node("매출은 5억원이다")
        candidates = detect_patch_candidates("이제 매출은 500,000,001원 정도인 것 같아", [old])
        retiring = [c for c in candidates if c["operation"] in RETIRING_CANDIDATE_OPERATIONS]
        self.assertTrue(retiring)
        self.assertEqual(retiring[0]["target_id"], old.id)


class CorrectionsEvalSetTests(unittest.TestCase):
    """Precision/recall gate over tests/fixtures/corrections_eval.jsonl (22
    positive corrections + 45 hard negatives, including the original reported
    false-positive repro, the round-2 conditional/value-coincidence/
    negation-hedge false-positive families, and the round-3 anira-lexeme/
    reported-speech/enumeration/numeral-canonicalization false-positive
    families -- see module docstrings on ``_looks_like_conditional``,
    ``_has_value_contradiction``, ``WEAK_NEGATION_TRIGGERS``,
    ``_contains_correction_trigger``, ``_looks_like_reported_speech``, and
    ``_canonical_value`` in ``cclg.patches``). Gate: precision == 1.0 on
    negatives (zero retiring candidates), recall >= 0.9 on positives."""

    @classmethod
    def setUpClass(cls) -> None:
        path = FIXTURES_DIR / "corrections_eval.jsonl"
        with path.open(encoding="utf-8") as handle:
            cls.cases = [json.loads(line) for line in handle if line.strip()]
        cls.positives = [c for c in cls.cases if c["label"] == "positive"]
        cls.negatives = [c for c in cls.cases if c["label"] == "negative"]

    def _retiring_candidates(self, case: dict) -> tuple[list[dict], list[MemoryNode]]:
        nodes = [MemoryNode.create(content=content, source=f"eval:{case['id']}") for content in case["nodes"]]
        candidates = detect_patch_candidates(case["text"], nodes, limit=5)
        retiring = [c for c in candidates if c.get("operation") in RETIRING_CANDIDATE_OPERATIONS]
        return retiring, nodes

    def test_fixture_has_minimum_required_case_counts(self) -> None:
        self.assertGreaterEqual(len(self.positives), 22, "need >=22 positive corrections in the eval set")
        self.assertGreaterEqual(len(self.negatives), 40, "need >=40 hard negatives in the eval set")

    def test_precision_is_perfect_on_hard_negatives(self) -> None:
        """No hard negative may produce even one retiring-family candidate."""
        false_positives = []
        for case in self.negatives:
            retiring, _ = self._retiring_candidates(case)
            if retiring:
                false_positives.append((case["id"], case["text"], retiring))
        self.assertEqual(false_positives, [], f"hard negatives wrongly promoted to retiring candidates: {false_positives}")

    def test_real_reported_false_positive_is_fixed(self) -> None:
        """neg_01 is the literal repro from agent-hub's xfail -- assert it by
        id specifically, not just as part of the aggregate negative sweep."""
        case = next(c for c in self.negatives if c["id"] == "neg_01")
        retiring, _ = self._retiring_candidates(case)
        self.assertEqual(retiring, [])

    def test_recall_at_least_90_percent_on_positives(self) -> None:
        """Each positive must produce a retiring candidate targeting the
        expected node; recall (fraction detected) must be >= 0.9."""
        missed = []
        for case in self.positives:
            retiring, nodes = self._retiring_candidates(case)
            expected_id = nodes[case["expect_target_index"]].id
            if not any(c["target_id"] == expected_id for c in retiring):
                missed.append((case["id"], case["text"]))
        recall = 1.0 - (len(missed) / len(self.positives))
        self.assertGreaterEqual(recall, 0.9, f"recall {recall:.2f} below 0.9 gate; missed: {missed}")


if __name__ == "__main__":
    unittest.main()
