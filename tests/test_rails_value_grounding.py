"""Unit tests for ``cclg.rails.value_grounding`` — rule-A (round 2, see
tmp/rail-round2/CONTRACT.md).

Covers: the fixture-representative over-scrub regression (canonical
separator/case matching + query-echo grounding), general canonicalization
principles (dates, Korean-multiplier amounts, separator/possessive text)
independent of the fixture's specific strings, the over-scrub false-positive
guard (ordinary answers untouched), and the still-must-catch regression for
values with no grounding anywhere (context nor query).
"""

from __future__ import annotations

import unittest

from cclg.rails import value_grounding
from cclg.rails.value_grounding import check


class FixtureRepresentativeTests(unittest.TestCase):
    """tmp/rail-round2/fixtures/over_scrub_v3.jsonl — acceptance: 0 ungrounded."""

    def test_ckpt02_query_echoed_project_name_is_grounded(self) -> None:
        answer = (
            "The current leading diagnosis for the Project Maple incident is a stale "
            "environment-variable sync on the staging worker group. Leo currently has "
            "logs-only access to the Maple staging environment for debugging purposes, "
            "valid through Friday at 18:00."
        )
        query = (
            "What is the current leading Project Maple incident diagnosis, and what "
            "exact access scope does Leo currently have?"
        )
        grounding_context = (
            "[mem_0000000053] The current leading Maple incident diagnosis is stale "
            "environment-variable sync on the staging worker group.\n"
            "[mem_0000000072] Access recap: Leo still has logs-only Maple incident "
            "access through Friday 18:00 and nothing broader than that."
        )
        finding = check(answer, grounding_context=grounding_context, query=query)
        self.assertIn("Project Maple", finding.candidates)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_ckpt05_query_echoed_project_name_is_grounded(self) -> None:
        answer = (
            "The only open blocker for Project Maple is the runbook sign-off. Once "
            "this is completed, the project should be free of major blockers."
        )
        query = "What blockers are still open for Project Maple right now?"
        grounding_context = (
            "[mem_0000000106] Runbook sign-off is still the only open Maple blocker."
        )
        finding = check(answer, grounding_context=grounding_context, query=query)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_ckpt06_query_echoed_project_name_is_grounded(self) -> None:
        answer = (
            "Project Maple has no remaining major blockers. The safe broad customer "
            "wording to use is 'the insurance pilot'."
        )
        query = (
            "What blockers remain for Project Maple now, and what broad customer "
            "wording can I safely use in a sponsor-safe line?"
        )
        grounding_context = (
            "[mem_0000000112] Maple now has no remaining major blockers. Keep using "
            "the broad customer wording 'the insurance pilot' when speaking outside "
            "the core room."
        )
        finding = check(answer, grounding_context=grounding_context, query=query)
        self.assertEqual(finding.ungrounded_spans, ())


class CanonicalDateTests(unittest.TestCase):
    def test_iso_date_grounded_by_korean_full_date_in_context(self) -> None:
        answer = "다음 회의는 2026-08-01입니다."
        grounding_context = "각 미팅은 2026년 8월 1일으로 예정되어 있습니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_slash_date_grounded_by_dotted_date_in_context(self) -> None:
        answer = "일정은 2026/8/1 입니다."
        grounding_context = "확정된 일정은 2026.08.01 입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_year_less_month_day_does_not_ground_a_dated_span(self) -> None:
        # "8월 1일" alone never asserts a year -- it must not silently
        # validate an unrelated year-bearing date just because month/day
        # happen to coincide.
        answer = "다음 회의는 2026-08-01입니다."
        grounding_context = "회의는 8월 1일경에 있을 예정입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertIn("2026-08-01", finding.ungrounded_spans)


class CanonicalAmountTests(unittest.TestCase):
    def test_korean_multiplier_amount_grounded_by_grouped_amount(self) -> None:
        answer = "환불액은 100만원입니다."
        grounding_context = "환불 처리 금액은 1,000,000원으로 확정되었습니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_eok_multiplier_amount_grounded_by_grouped_amount(self) -> None:
        answer = "예산은 5억 규모로 책정되었습니다."
        grounding_context = "승인된 예산은 500,000,000원입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_different_amount_still_ungrounded(self) -> None:
        answer = "예산은 5억 규모로 책정되었습니다."
        grounding_context = "승인된 예산은 300,000,000원입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertIn("5억", finding.ungrounded_spans)


class CanonicalTextSeparatorTests(unittest.TestCase):
    def test_snake_case_identifier_in_context_grounds_title_case_answer(self) -> None:
        # The over-scrub scenario the round-2 charter describes: a
        # code-identifier ("project_maple") in the context should ground a
        # Title-Case mention in the answer, purely via context (no query
        # needed this time).
        answer = "Project Maple's status remains green."
        grounding_context = "Ops channel: project_maple's status remains green for this cycle."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_hyphenated_context_grounds_spaced_answer(self) -> None:
        answer = "The Northstar Rollout schedule remains on track."
        grounding_context = "Weekly update: the Northstar-Rollout schedule remains on track."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())


class QueryEchoGuardTests(unittest.TestCase):
    def test_query_echo_does_not_ground_a_different_name(self) -> None:
        # The query mentions a *different* distinctive value -- echoing
        # grounding must not bleed across unrelated spans.
        answer = "Project Maple is fully unblocked now."
        query = "What is the status of Project Falcon?"
        grounding_context = "전혀 무관한 컨텍스트 텍스트입니다."
        finding = check(answer, grounding_context=grounding_context, query=query)
        self.assertIn("Project Maple", finding.ungrounded_spans)

    def test_no_query_still_flags_hallucinated_project_name(self) -> None:
        answer = "Project Maple is fully unblocked now."
        grounding_context = "전혀 무관한 컨텍스트 텍스트입니다."
        finding = check(answer, grounding_context=grounding_context, query="")
        self.assertIn("Project Maple", finding.ungrounded_spans)


class OverScrubFalsePositiveGuardTests(unittest.TestCase):
    def test_ordinary_short_answer_with_irrelevant_context_is_untouched(self) -> None:
        for answer in (
            "감사합니다! 3개 준비했어요.",
            "네, 알겠습니다. 12명 참석 예정입니다.",
            "안녕하세요, 무엇을 도와드릴까요?",
        ):
            finding = check(answer, grounding_context="완전히 무관한 배경 컨텍스트 텍스트")
            self.assertEqual(finding.candidates, ())
            self.assertEqual(finding.ungrounded_spans, ())

    def test_empty_grounding_context_skips_category_entirely(self) -> None:
        finding = check("다음 회의는 2026-08-01입니다.", grounding_context="")
        self.assertEqual(finding.candidates, ())
        self.assertEqual(finding.ungrounded_spans, ())
        self.assertFalse(finding.refuse)


class StillMustCatchRegressionTests(unittest.TestCase):
    """agent-hub tests/test_output_rail.py (b)/(d)/(e) fix these at the
    facade level; this pins the same guarantee at the rule level so a
    regression here is caught before it ever reaches the facade."""

    def test_fully_ungrounded_date_is_flagged(self) -> None:
        answer = "다음 회의는 2026-08-01입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("2026-08-01", finding.ungrounded_spans)

    def test_fully_ungrounded_amount_is_flagged(self) -> None:
        answer = "예산은 9,999,000원 입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("9,999,000", finding.ungrounded_spans)

    def test_fully_ungrounded_name_is_flagged(self) -> None:
        answer = "담당자는 Alice Wonderland입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("Alice Wonderland", finding.ungrounded_spans)

    def test_never_sets_refuse(self) -> None:
        answer = "회의는 2026-08-01, 예산은 9,999,000원, 담당자는 Alice Wonderland입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertFalse(finding.refuse)


class CourtesySignOffOverScrubTests(unittest.TestCase):
    """Round-2 adversarial-verification break: PROPER_NOUN_PAIR_RE flagged
    ordinary courtesy sign-offs as ungrounded distinctive values, which could
    (via the facade's ratio escalation) nuke an otherwise fully-grounded
    answer into a full refusal."""

    def test_sign_off_bigrams_are_not_candidates(self) -> None:
        answer = (
            "Thank You for your patience -- Best Regards, the Support Team."
        )
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertEqual(finding.candidates, ())
        self.assertEqual(finding.ungrounded_spans, ())

    def test_happy_weekday_greeting_is_not_a_candidate(self) -> None:
        answer = "Happy Monday, team! Quick check-in on the roadmap before we start."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertEqual(finding.candidates, ())

    def test_fixed_fixture_answer_survives_an_appended_sign_off(self) -> None:
        # ckpt_02's already-fixed answer (query-echoed "Project Maple") plus a
        # routine courteous closing appended -- the sign-off must not
        # reintroduce over-scrub for an otherwise fully-grounded answer.
        answer = (
            "The current leading diagnosis for the Project Maple incident is a stale "
            "environment-variable sync on the staging worker group. Leo currently has "
            "logs-only access to the Maple staging environment for debugging purposes, "
            "valid through Friday at 18:00. Thank You for your patience -- Best Regards, "
            "the Support Team."
        )
        query = (
            "What is the current leading Project Maple incident diagnosis, and what "
            "exact access scope does Leo currently have?"
        )
        grounding_context = (
            "[mem_0000000053] The current leading Maple incident diagnosis is stale "
            "environment-variable sync on the staging worker group.\n"
            "[mem_0000000072] Access recap: Leo still has logs-only Maple incident "
            "access through Friday 18:00 and nothing broader than that."
        )
        finding = check(answer, grounding_context=grounding_context, query=query)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_known_vendor_product_pairs_are_not_candidates(self) -> None:
        answer = "I'll send the invite via Google Calendar and loop in Microsoft Teams for the call."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertEqual(finding.candidates, ())
        self.assertEqual(finding.ungrounded_spans, ())

    def test_genuine_distinctive_name_is_not_swallowed_by_the_common_phrase_filter(self) -> None:
        # Sanity control: the filter must not become so broad that a real
        # hallucinated name slips through uncaught.
        answer = "담당자는 Alice Wonderland입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("Alice Wonderland", finding.candidates)
        self.assertIn("Alice Wonderland", finding.ungrounded_spans)


class AmountDoubleExtractionTests(unittest.TestCase):
    """Round-2 break: a comma-grouped amount's own trailing digit group was
    independently re-matched by ``UNIT_AMOUNT_RE`` as a second, phantom
    candidate -- whose canonical value never grounds, corrupting an
    already-correctly-grounded number on redaction."""

    def test_won_amount_does_not_yield_a_phantom_tail_candidate(self) -> None:
        answer = "환불 처리 금액은 1,000,000원으로 확정되었습니다."
        finding = check(answer, grounding_context="환불액은 100만원입니다.")
        self.assertEqual(finding.candidates, ("1,000,000",))
        self.assertEqual(finding.ungrounded_spans, ())

    def test_usd_amount_does_not_yield_a_phantom_tail_candidate(self) -> None:
        answer = "The total refund was 1,000,000 USD as agreed."
        finding = check(answer, grounding_context="Finance recap: the approved refund is 1000000.")
        self.assertEqual(finding.candidates, ("1,000,000",))
        self.assertEqual(finding.ungrounded_spans, ())


class NumericSubstringCollisionGuardTests(unittest.TestCase):
    """Round-2 break: the whole-text raw-substring fallback in ``_grounded``
    had no numeric-boundary check, so a fabricated amount/reference number
    was silently "grounded" merely by being a right-aligned digit substring
    of a longer, unrelated number elsewhere in the context."""

    def test_grouped_amount_is_not_grounded_by_an_unrelated_larger_amount(self) -> None:
        answer = "예산은 9,999,000원 입니다."
        finding = check(answer, grounding_context="작년 예산은 29,999,000원이었고 올해는 전혀 다른 금액입니다.")
        self.assertIn("9,999,000", finding.ungrounded_spans)

    def test_grouped_amount_is_not_grounded_by_an_unrelated_revenue_figure(self) -> None:
        answer = "담당 티켓 금액은 5,236,000원입니다."
        finding = check(answer, grounding_context="이번 분기 총 매출은 125,236,000원으로 집계되었습니다.")
        self.assertIn("5,236,000", finding.ungrounded_spans)

    def test_bare_digit_reference_is_not_grounded_by_an_unrelated_longer_id(self) -> None:
        answer = "참조 번호는 3456입니다."
        finding = check(answer, grounding_context="시스템 로그 ID는 8123456으로 기록되어 있습니다.")
        self.assertIn("3456", finding.ungrounded_spans)


class ProperNounSubstringCollisionGuardTests(unittest.TestCase):
    """Round-2 break: same raw-substring flaw for the free-text category --
    "project maple" is a literal prefix substring of "project maplemark", so
    a hallucinated "Project Maple" was silently grounded by an unrelated
    "Project Maplemark" mention (context or query-echo path)."""

    def test_not_grounded_by_an_unrelated_longer_name_in_context(self) -> None:
        answer = "Project Maple has no remaining major blockers."
        finding = check(answer, grounding_context="Status chatter: Project Maplemark activity continues in parallel this week.")
        self.assertIn("Project Maple", finding.ungrounded_spans)

    def test_not_grounded_by_an_unrelated_longer_name_via_query_echo(self) -> None:
        answer = "Project Maple has no remaining major blockers."
        finding = check(
            answer,
            grounding_context="전혀 무관한 컨텍스트 텍스트입니다.",
            query="What is the latest status of Project Maplemark this week?",
        )
        self.assertIn("Project Maple", finding.ungrounded_spans)


class CaseVariationExtractionTests(unittest.TestCase):
    """Round-2 break: ``PROPER_NOUN_PAIR_RE`` required strict Title Case, so
    an ALL-CAPS / lowercase / mixed-case rendering of the same hallucinated
    name (embedded in Korean prose) was never even extracted as a candidate
    -- invisible to the rule end-to-end, not merely "grounded"."""

    def test_allcaps_hallucinated_name_is_still_flagged(self) -> None:
        answer = "담당자는 ALICE WONDERLAND입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("ALICE WONDERLAND", finding.candidates)
        self.assertIn("ALICE WONDERLAND", finding.ungrounded_spans)

    def test_lowercase_hallucinated_name_is_still_flagged(self) -> None:
        answer = "담당자는 alice wonderland입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("alice wonderland", finding.candidates)
        self.assertIn("alice wonderland", finding.ungrounded_spans)

    def test_mixed_case_hallucinated_name_is_still_flagged(self) -> None:
        answer = "담당자는 Alice WONDERLAND입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("Alice WONDERLAND", finding.candidates)
        self.assertIn("Alice WONDERLAND", finding.ungrounded_spans)

    def test_case_insensitive_extraction_is_scoped_to_hangul_adjacent_spans(self) -> None:
        # Guard against the naive over-broad fix: an all-English answer must
        # not have every lowercase multi-word run turned into a candidate
        # (that would reintroduce over-scrub for ordinary English replies).
        answer = "I'll send the invite via the shared calendar and loop in the team for the call."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertEqual(finding.candidates, ())


class ExtractionHelperTests(unittest.TestCase):
    """Direct coverage of the module-level canonicalization helpers."""

    def test_canonical_date_equivalence(self) -> None:
        self.assertEqual(
            value_grounding._canonical_date("2026-08-01"),
            value_grounding._canonical_date("2026년 8월 1일"),
        )
        self.assertEqual(
            value_grounding._canonical_date("2026-08-01"),
            value_grounding._canonical_date("2026/8/1"),
        )

    def test_canonical_amount_equivalence(self) -> None:
        self.assertEqual(
            value_grounding._canonical_amount("100만원"),
            value_grounding._canonical_amount("1,000,000"),
        )
        self.assertEqual(
            value_grounding._canonical_amount("5억"),
            value_grounding._canonical_amount("500,000,000"),
        )

    def test_canonical_amount_returns_none_for_non_amount(self) -> None:
        self.assertIsNone(value_grounding._canonical_amount("Project Maple"))

    def test_canonical_date_returns_none_for_non_date(self) -> None:
        self.assertIsNone(value_grounding._canonical_date("100만원"))


class KoreanPostpositionAttachmentTests(unittest.TestCase):
    """Round-2 second-pass break: a Korean topic/genitive particle (은/는/의)
    attaches directly to the preceding noun with no space, which defeated the
    whole-text boundary check for a context-only snake_case/kebab-case
    identifier -- see module docstring §8."""

    def test_snake_case_identifier_with_directly_attached_topic_particle(self) -> None:
        answer = "Project Maple은 이번 주 안정화되었고 별다른 이슈가 없습니다."
        grounding_context = (
            "[mem_0000000091] (speaker=eng_noah_reed): project_maple은 이번 주 "
            "안정화되었고 별다른 이슈가 없습니다."
        )
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_kebab_case_identifier_with_directly_attached_topic_particle(self) -> None:
        answer = "Project Maple은 이번 주 안정화되었습니다."
        grounding_context = "[mem_02] (speaker=eng): project-maple은 이번 주 안정화되었습니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_possessive_answer_with_directly_attached_genitive_particle_in_context(self) -> None:
        answer = "Project Maple's 상태는 안정적입니다."
        grounding_context = "[mem_03] (speaker=eng): project_maple의 상태는 안정적입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_directly_attached_topic_particle_via_query_echo(self) -> None:
        answer = "Project Maple은 안정화되었습니다."
        grounding_context = "전혀 무관한 컨텍스트입니다."
        query = "project_maple은 어떻게 되었나요?"
        finding = check(answer, grounding_context=grounding_context, query=query)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_same_script_continuation_is_still_rejected_after_the_fix(self) -> None:
        # Guard against an over-broad fix: a same-script (Latin-into-Latin)
        # continuation must still be rejected -- only cross-script
        # (Latin/digit directly followed by Hangul) adjacency is exempted.
        answer = "Project Maple has no remaining major blockers."
        finding = check(
            answer,
            grounding_context="Status chatter: Project Maplemark activity continues in parallel this week.",
        )
        self.assertIn("Project Maple", finding.ungrounded_spans)


class CompoundKoreanMultiplierAmountTests(unittest.TestCase):
    """Round-2 second-pass break: a chained Korean-multiplier amount
    ("5억 2천만원") was mis-split into two wrong sub-values instead of one
    combined amount -- see module docstring §10."""

    def test_chained_multiplier_with_internal_space_is_grounded(self) -> None:
        answer = "예산은 5억 2천만원으로 책정되었습니다."
        grounding_context = "승인된 예산은 520,000,000원입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.candidates, ("5억 2천만원",))
        self.assertEqual(finding.ungrounded_spans, ())

    def test_chained_multiplier_with_no_internal_space_is_grounded(self) -> None:
        answer = "예산은 1억5천만원으로 책정되었습니다."
        grounding_context = "승인된 예산은 150,000,000원입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.candidates, ("1억5천만원",))
        self.assertEqual(finding.ungrounded_spans, ())

    def test_chained_multiplier_still_ungrounded_when_context_disagrees(self) -> None:
        answer = "예산은 5억 2천만원으로 책정되었습니다."
        grounding_context = "승인된 예산은 300,000,000원입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertIn("5억 2천만원", finding.ungrounded_spans)

    def test_parse_korean_compound_amount_sums_chained_terms(self) -> None:
        self.assertEqual(value_grounding._parse_korean_compound_amount("5억 2천만"), 520_000_000.0)
        self.assertEqual(value_grounding._parse_korean_compound_amount("1억5천만"), 150_000_000.0)
        self.assertIsNone(value_grounding._parse_korean_compound_amount("Project Maple"))


class MeetingIdiomOverScrubTests(unittest.TestCase):
    """Round-2 second-pass break: ordinary meeting-notes section headers /
    scheduling idioms were extracted as distinctive-value candidates -- see
    module docstring §11."""

    def test_next_steps_header_is_not_a_candidate(self) -> None:
        answer = "Next Steps: please review the draft by tomorrow."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic entirely.")
        self.assertEqual(finding.candidates, ())

    def test_action_items_header_is_not_a_candidate(self) -> None:
        answer = "Please see the Action Items listed below for this week."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic entirely.")
        self.assertEqual(finding.candidates, ())

    def test_quick_sync_idiom_is_not_a_candidate(self) -> None:
        answer = "Let's do a Quick Sync tomorrow morning to align."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic entirely.")
        self.assertEqual(finding.candidates, ())

    def test_follow_up_idiom_is_not_a_candidate(self) -> None:
        answer = "I'll do a Follow Up with the vendor next week."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic entirely.")
        self.assertEqual(finding.candidates, ())

    def test_composite_meeting_idioms_do_not_trigger_facade_refusal(self) -> None:
        # The measured over-scrub consequence: three benign meeting-idiom
        # bigrams alone used to push total_candidates>=3 and ratio=1.0,
        # fully refusing an entirely benign sentence at the facade level.
        from cclg.grounding_rail import apply_output_rail

        answer = "Quick Sync tomorrow to review Action Items and confirm Next Steps for the rollout."
        grounding_context = "[mem_01] Rollout status recap from last week stand-up, all teams aligned on timeline."
        result = apply_output_rail(answer, grounding_context=grounding_context)
        self.assertFalse(result.refused)
        self.assertEqual(result.text, answer)


class FabricatedEntityVendorCollisionTests(unittest.TestCase):
    """Round-2 second-pass break: a fabricated org/product-shaped entity name
    that happens to reuse courtesy-discourse or known-vendor/category
    vocabulary was swallowed by the round-1 exclusion filters and never
    flagged -- see module docstring §12."""

    def test_support_team_mid_sentence_claim_is_flagged(self) -> None:
        answer = "I've escalated this directly to the Support Team, who will own the fix from here."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertIn("Support Team", finding.candidates)
        self.assertIn("Support Team", finding.ungrounded_spans)

    def test_trailing_signoff_support_team_is_still_excluded(self) -> None:
        # Must not regress: the courtesy-closing usage stays excluded.
        answer = "Thank You for your patience -- Best Regards, the Support Team."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertEqual(finding.candidates, ())

    def test_slack_team_fabricated_entity_is_flagged(self) -> None:
        answer = "I've looped in the Slack Team to review this before it ships."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertIn("Slack Team", finding.candidates)
        self.assertIn("Slack Team", finding.ungrounded_spans)

    def test_zoom_chat_fabricated_entity_is_flagged(self) -> None:
        answer = "This was flagged by the Zoom Chat moderators internally."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertIn("Zoom Chat", finding.candidates)
        self.assertIn("Zoom Chat", finding.ungrounded_spans)

    def test_adobe_docs_fabricated_entity_is_flagged(self) -> None:
        answer = "Adobe Docs signed off on the redesign yesterday."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertIn("Adobe Docs", finding.candidates)
        self.assertIn("Adobe Docs", finding.ungrounded_spans)

    def test_real_known_vendor_product_pairs_still_excluded(self) -> None:
        # Must not regress: genuinely well-known product names stay excluded.
        answer = "I'll send the invite via Google Calendar and loop in Microsoft Teams for the call."
        finding = check(answer, grounding_context="Unrelated ops channel note about a completely different topic.")
        self.assertEqual(finding.candidates, ())


class InvisibleCharacterEvasionTests(unittest.TestCase):
    """Round-2 second-pass break: a zero-width space (ZWSP, U+200B) inserted
    as a steganographic word separator defeated both Title-Case pair
    extraction and the Hangul-adjacency boundary check -- see module
    docstring §9."""

    def test_zwsp_between_title_case_words_is_still_extracted_and_flagged(self) -> None:
        answer = "The escalation owner is Alice​Wonderland, who is not mentioned anywhere else."
        finding = check(answer, grounding_context="Completely unrelated context text about a different topic entirely.")
        self.assertTrue(any("Alice" in c and "Wonderland" in c for c in finding.candidates))
        self.assertTrue(any("Alice" in c and "Wonderland" in c for c in finding.ungrounded_spans))

    def test_zwsp_on_both_sides_of_hangul_adjacent_allcaps_name_is_still_flagged(self) -> None:
        answer = "메모​ALICE WONDERLAND​메모입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("ALICE WONDERLAND", finding.candidates)
        self.assertIn("ALICE WONDERLAND", finding.ungrounded_spans)

    def test_zwsp_grounded_mention_still_matches_plain_space_context(self) -> None:
        # A ZWSP-obfuscated mention of a genuinely grounded value should
        # still canonically equal its plain-space rendering in the context.
        answer = "Project​Maple's status remains green."
        grounding_context = "Ops channel: project_maple's status remains green for this cycle."
        finding = check(answer, grounding_context=grounding_context)
        self.assertEqual(finding.ungrounded_spans, ())


class FullwidthDigitAmountTests(unittest.TestCase):
    """Round-2 second-pass break: a fullwidth comma defeated
    ``GROUPED_AMOUNT_RE``'s literal ASCII thousands separator, so only a
    trailing fragment of a forged fullwidth amount was ever flagged/
    redacted -- see module docstring §13."""

    def test_fullwidth_grouped_amount_is_captured_as_one_full_candidate(self) -> None:
        answer = "예산은 １，０００，０００원 입니다."
        finding = check(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.")
        full_number_captured = any("１" in c and c.count("０") >= 5 for c in finding.candidates)
        self.assertTrue(full_number_captured)
        self.assertTrue(any("１" in c and c.count("０") >= 5 for c in finding.ungrounded_spans))

    def test_fullwidth_amount_does_not_falsely_ground_against_a_different_ascii_amount(self) -> None:
        answer = "예산은 ９９９，０００，０００원 규모입니다."
        grounding_context = "승인된 예산은 500,000,000원입니다."
        finding = check(answer, grounding_context=grounding_context)
        self.assertTrue(finding.candidates)
        self.assertTrue(finding.ungrounded_spans)


if __name__ == "__main__":
    unittest.main()


class GenericClassifierStrippedMatchingTests(unittest.TestCase):
    """Module docstring §14: "Project Timber" must ground against a context/
    query that only writes the bare name "Timber" (measured GateMem v4
    residual — 3 fully-grounded utility answers lost only the project name
    to redaction), while a fabricated "<classifier> <common word>" pair must
    never ground itself off incidental vocabulary."""

    def test_classifier_prefixed_name_grounds_against_bare_name_in_query(self) -> None:
        answer = "The current approved status for Project Timber is on track."
        query = "Summarize the current approved status of Timber: launch date and budget."
        finding = check(answer, grounding_context="Launch date confirmed for Timber.", query=query)
        self.assertEqual(finding.ungrounded_spans, ())

    def test_classifier_prefixed_name_grounds_against_bare_name_in_context_only(self) -> None:
        answer = "Project Ember has no open blockers."
        finding = check(answer, grounding_context="Ember: all blockers cleared as of this week.")
        self.assertEqual(finding.ungrounded_spans, ())

    def test_stripped_remainder_still_respects_token_boundaries(self) -> None:
        # "Timber" must NOT ground inside "Timberlane" even after stripping.
        answer = "Project Timber is on schedule."
        finding = check(answer, grounding_context="Timberlane rollout continues; nothing else.")
        self.assertIn("Project Timber", finding.ungrounded_spans)

    def test_classifier_plus_common_word_never_grounds_off_incidental_vocabulary(self) -> None:
        # "best" appears in the context as ordinary vocabulary; "Project
        # Best" must not become grounded through the stripped remainder.
        answer = "Project Best is our new initiative."
        finding = check(answer, grounding_context="We will do our best to close this quarter.")
        self.assertIn("Project Best", finding.ungrounded_spans)

    def test_classifier_only_span_never_grounds_via_stripping(self) -> None:
        # Every word is a classifier -> stripped remainder is empty -> the
        # stripping path must not fire (and the pair itself is ungrounded).
        answer = "Project Team will review this."
        finding = check(answer, grounding_context="완전히 무관한 컨텍스트 텍스트입니다.")
        self.assertIn("Project Team", finding.ungrounded_spans)

    def test_fully_ungrounded_classifier_name_still_flagged(self) -> None:
        answer = "Project Nebula launches next quarter."
        finding = check(answer, grounding_context="Timber and Ember are the only active projects.")
        self.assertIn("Project Nebula", finding.ungrounded_spans)
