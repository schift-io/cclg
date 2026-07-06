"""Facade-level tests for ``cclg.grounding_rail.apply_output_rail`` — the
integration agent's slice of round 2 (see ``tmp/rail-round2/CONTRACT.md``,
"integration — query 배선").

Scope: this file tests the **composing facade's escalation policy** (redact
vs refuse, the min-candidate ratio guard) and its wiring contract (``query``
optional/pass-through, ``RailAction.kind`` mapping) — NOT any individual
rule's internal precision. ``cclg.rails.confirmation`` is being implemented
by a different agent in parallel and is a no-op skeleton as of this writing
(``RuleFinding(rule="confirmation")`` unconditionally) — tests here never
assume anything about its regex/marker behavior. Where the facade's handling
of a confirmation-style refusal needs covering, a fake ``RuleFinding`` is
monkeypatched in via ``confirmation.check`` so the assertion is purely about
facade wiring, not confirmation's own logic.
"""

from __future__ import annotations

from cclg.grounding_rail import RailAction, RailResult, apply_output_rail
from cclg.rails import confirmation
from cclg.rails.base import REFUSAL_TEXT, RuleFinding

# ---------------------------------------------------------------------------
# (a) escalation policy — rule refuse, min-candidate guard, ratio escalation.
# ---------------------------------------------------------------------------


def test_rule_refuse_escalates_to_full_refusal() -> None:
    """A rule reporting ``refuse=True`` (here: pii's digit-bearing credential
    default-deny, unchanged since v1) always wins regardless of candidate
    count/ratio — the whole answer is replaced with ``REFUSAL_TEXT``, not
    partially redacted (partial redaction of a confirmed credential still
    leaks that the value existed)."""
    answer = "Your API key is sk_live_12345."
    result = apply_output_rail(answer, grounding_context="", query="")
    assert result.refused is True
    assert result.flagged is True
    assert result.text == REFUSAL_TEXT
    assert "sk_live_12345" not in result.text


def test_below_min_candidates_distinctive_only_is_redacted_not_escalated() -> None:
    """Round-2 fix (the exact over-scrub failure measured in v3, utility
    47.4%→45.5%): a single ungrounded distinctive-value candidate (ratio
    1.0) must NOT alone trigger ratio escalation — ``_REFUSAL_MIN_CANDIDATES``
    (3) gates it. Below that floor the answer is redacted, not refused."""
    answer = "다음 회의는 2026-08-01입니다."
    result = apply_output_rail(answer, grounding_context="전혀 무관한 배경 컨텍스트입니다.", query="")
    assert result.refused is False
    assert result.flagged is True
    assert "2026-08-01" not in result.text
    assert "[REDACTED]" in result.text


def test_at_or_above_min_candidates_majority_ungrounded_escalates_to_refusal() -> None:
    """3 distinctive-value candidates, all ungrounded (ratio 1.0 >= 0.5,
    total 3 >= min-candidates 3) — the answer is "mostly fabricated content",
    so the facade refuses rather than serving redaction mush."""
    answer = "회의는 2026-08-01, 예산은 9,999,000원, 담당자는 Alice Wonderland입니다."
    result = apply_output_rail(answer, grounding_context="전혀 무관한 컨텍스트 텍스트입니다.", query="")
    assert result.refused is True
    assert result.flagged is True
    assert result.text == REFUSAL_TEXT


def test_ratio_below_threshold_with_enough_candidates_is_redacted_not_escalated() -> None:
    """3+ candidates but fewer than half ungrounded must NOT escalate --
    the ratio gate, not just the count gate, has to hold."""
    answer = (
        "회의는 2026-07-10, 예산은 1,000,000원, 담당자는 Jane Doe이고 "
        "다음 회의는 2026-08-01입니다."
    )
    grounding_context = "회의는 2026-07-10, 예산은 1,000,000원이며 담당자는 Jane Doe입니다."
    result = apply_output_rail(answer, grounding_context=grounding_context, query="")
    assert result.refused is False
    assert result.flagged is True
    assert "2026-07-10" in result.text
    assert "1,000,000" in result.text
    assert "Jane Doe" in result.text
    assert "2026-08-01" not in result.text


# ---------------------------------------------------------------------------
# (b) ``query`` is optional — callers on an older wiring path that never
# pass it must keep working exactly as before (round-1 call sites, or a
# safety-net layer with no query available).
# ---------------------------------------------------------------------------


def test_query_kwarg_is_optional_old_call_style_still_works() -> None:
    answer = "다음 회의는 2026-08-01입니다."
    context = "전혀 무관한 배경 컨텍스트입니다."
    # No ``query=`` at all -- mirrors every pre-round-2 call site.
    no_query_result = apply_output_rail(answer, grounding_context=context)
    with_empty_query_result = apply_output_rail(answer, grounding_context=context, query="")
    assert no_query_result == with_empty_query_result


def test_query_kwarg_defaults_to_empty_string_and_is_a_no_op_when_absent() -> None:
    # An answer with no PII/distinctive-value spans and no context is a
    # complete no-op whether or not ``query`` is supplied.
    answer = "안녕하세요, 무엇을 도와드릴까요?"
    result = apply_output_rail(answer)
    assert result == RailResult(text=answer)


def test_query_echoes_do_not_crash_facade_wiring_regardless_of_rule_support() -> None:
    """Passing a non-empty ``query`` must never raise, whether or not the
    rule that would use it (value_grounding's query-echo grounding, still
    in progress under rule-A) has landed yet -- the facade always forwards
    ``query`` to every rule via the same ``check(..., query=query)`` call."""
    answer = "다음 회의는 2026-08-01입니다."
    result = apply_output_rail(
        answer,
        grounding_context="전혀 무관한 배경 컨텍스트입니다.",
        query="What is the date of Project Maple's next meeting, 2026-08-01?",
    )
    # Regardless of whether query-echo grounding is implemented yet, the
    # call must complete and return a well-formed RailResult.
    assert isinstance(result, RailResult)


# ---------------------------------------------------------------------------
# (c) RailAction.kind mapping — 3 rule names -> 3 action kinds.
# ---------------------------------------------------------------------------


def test_pii_finding_maps_to_pii_action_kind() -> None:
    # An ungrounded email is flagged (pii candidate) but not credential-shaped,
    # so it redacts rather than refuses -- and stays below the min-candidate
    # floor (1 candidate total) so ratio escalation never kicks in either.
    answer = "문의는 unknown@example.com 으로 보내주세요."
    result = apply_output_rail(answer, grounding_context="", query="")
    assert result.refused is False
    assert any(a.kind == "pii" and a.span == "unknown@example.com" for a in result.actions)


def test_value_grounding_finding_maps_to_grounding_action_kind() -> None:
    answer = "다음 회의는 2026-08-01입니다."
    result = apply_output_rail(answer, grounding_context="전혀 무관한 배경 컨텍스트입니다.", query="")
    assert any(a.kind == "grounding" and a.span == "2026-08-01" for a in result.actions)


def test_confirmation_finding_maps_to_confirmation_action_kind(monkeypatch) -> None:
    """confirmation.check's real behavior is owned by a different agent and
    still a no-op skeleton -- inject a fake ``RuleFinding`` so this test is
    purely about the facade's kind-mapping wiring, not confirmation's own
    detection logic."""

    def fake_check(answer: str, *, grounding_context: str = "", query: str = "") -> RuleFinding:
        return RuleFinding(
            rule="confirmation",
            candidates=("probed_value",),
            ungrounded_spans=("probed_value",),
            refuse=False,
            reason="fake_for_kind_mapping_test",
        )

    monkeypatch.setattr(confirmation, "check", fake_check)
    result = apply_output_rail("Yes.", grounding_context="", query="Was it probed_value?")
    assert any(a.kind == "confirmation" and a.span == "probed_value" for a in result.actions)


def test_confirmation_rule_refuse_is_honored_by_facade(monkeypatch) -> None:
    """The escalation policy's rule ①: ANY rule with ``refuse=True`` wins,
    independent of which rule reported it. Verified here via a monkeypatched
    fake confirmation finding (not the real, still-in-progress confirmation
    module) so this test cannot become stale/wrong when that module lands --
    it only pins the facade's contract that it *respects* ``RuleFinding.refuse``
    from whichever rule sets it."""

    def fake_refusing_check(
        answer: str, *, grounding_context: str = "", query: str = ""
    ) -> RuleFinding:
        return RuleFinding(
            rule="confirmation",
            candidates=("probed_value",),
            ungrounded_spans=("probed_value",),
            refuse=True,
            reason="fake_confirm_attack",
        )

    monkeypatch.setattr(confirmation, "check", fake_refusing_check)
    answer = "Yes, that is correct."
    result = apply_output_rail(answer, grounding_context="", query="Was the deleted account probed_value?")
    assert result.refused is True
    assert result.flagged is True
    assert result.text == REFUSAL_TEXT
    assert any(a.kind == "confirmation" for a in result.actions)


def test_action_dataclass_shape_unchanged() -> None:
    action = RailAction(kind="grounding", span="x")
    assert action.kind == "grounding"
    assert action.span == "x"
