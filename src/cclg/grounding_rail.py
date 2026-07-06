"""L4 output rail — composing facade over the ``cclg.rails`` rule modules.

Pure Python, zero network/LLM calls: a rule-based gate applied *after* an
answer has already been generated. Round 1 (v1, commit 91271dd) was a single
monolithic module; round 2 split it into independent rule modules so each
rule can be developed, unit-tested, and adversarially attacked in isolation
(measured v1 verdict: weak effect — deletion answer-leak 4.95%→4.50%,
privacy unchanged at 3.51%, utility −1.9pp over-scrub; see
``docs/GATEMEM_OFFICE.md`` and ``tmp/rail-round2/CONTRACT.md``):

- ``cclg.rails.pii``             — email/phone/credential spans, default-deny.
- ``cclg.rails.value_grounding`` — dates/amounts/proper-noun pairs vs context.
- ``cclg.rails.confirmation``    — query-aware confirmation-attack gate (new).

This facade owns the **escalation policy** — rule modules only report
:class:`cclg.rails.base.RuleFinding`; the decision to redact vs refuse is
made in one place:

1. Any rule with ``refuse=True`` (ungrounded credential-shaped span, or a
   confirmed protected-referent probe) → whole answer replaced with the
   fixed refusal text. Partial redaction of a confirmed credential/relation
   still leaks that the value existed and was confirmed.
2. Otherwise, ratio escalation: when **at least 3** candidate spans were
   evaluated across all rules and **≥ 50%** of them are ungrounded, the
   answer is mostly fabricated content — refuse rather than serve
   "[REDACTED] [REDACTED]" mush. The minimum-candidate guard is a round-2
   change: v1 escalated on ratio alone, so a single ungrounded proper noun
   (candidate count 1, ratio 1.0) nuked an otherwise-grounded answer — the
   exact over-scrub failure measured in v3 (utility 47.4%→45.5%).
3. Otherwise, redact just the ungrounded spans.

``query`` (round-2 addition) is the end-user message that produced
``answer``. It feeds the confirmation rule (attack detection) and — for the
distinctive-value category only — query-echo grounding. Callers that cannot
supply it pass ""; every rule degrades gracefully to answer+context only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cclg.rails import confirmation, pii, value_grounding
from cclg.rails.base import REFUSAL_TEXT, RuleFinding, dedup_keep_order, redact

_REFUSAL_UNGROUNDED_RATIO = 0.5
# Ratio escalation needs a broad evidence base — see module docstring §2.
_REFUSAL_MIN_CANDIDATES = 3

_ACTION_KIND_BY_RULE = {
    "pii": "pii",
    "value_grounding": "grounding",
    "confirmation": "confirmation",
}


@dataclass(frozen=True)
class RailAction:
    kind: str  # "pii" | "grounding" | "confirmation"
    span: str


@dataclass(frozen=True)
class RailResult:
    text: str
    flagged: bool = False
    refused: bool = False
    actions: tuple[RailAction, ...] = field(default_factory=tuple)


def apply_output_rail(answer: str, *, grounding_context: str = "", query: str = "") -> RailResult:
    """Deterministic grounding + PII + confirmation rail (public API).

    ``grounding_context`` is the raw text the model actually had available
    when it produced ``answer`` (tool-result observations / RAG context /
    prompt context). An empty string means "no context available at this
    layer" (e.g. a downstream safety-net policy that only sees the final
    reply): the PII category still applies (default-deny) but the
    distinctive-value category is skipped rather than guessed at. ``query``
    is the end-user message; empty when the caller doesn't have it.
    """
    if not answer or not answer.strip():
        return RailResult(text=answer)

    findings: list[RuleFinding] = [
        pii.check(answer, grounding_context=grounding_context, query=query),
        value_grounding.check(answer, grounding_context=grounding_context, query=query),
        confirmation.check(answer, grounding_context=grounding_context, query=query),
    ]

    ungrounded_by_rule = [(f.rule, span) for f in findings for span in f.ungrounded_spans]
    ungrounded = dedup_keep_order([span for _, span in ungrounded_by_rule])
    rule_refusals = [f for f in findings if f.refuse]

    if not ungrounded and not rule_refusals:
        return RailResult(text=answer)

    first_rule_for_span: dict[str, str] = {}
    for rule, span in ungrounded_by_rule:
        first_rule_for_span.setdefault(span.lower(), rule)
    actions = tuple(
        RailAction(kind=_ACTION_KIND_BY_RULE[first_rule_for_span[span.lower()]], span=span)
        for span in ungrounded
    )

    total_candidates = len(dedup_keep_order([c for f in findings for c in f.candidates]))
    ratio_escalate = (
        total_candidates >= _REFUSAL_MIN_CANDIDATES
        and len(ungrounded) / total_candidates >= _REFUSAL_UNGROUNDED_RATIO
    )

    if rule_refusals or ratio_escalate:
        return RailResult(text=REFUSAL_TEXT, flagged=True, refused=True, actions=actions)

    # Answer-side spans only: a rule may report query-side evidence (e.g. the
    # confirmation rule naming the echoed probe value) without demanding
    # refusal — redacting a span that never occurs in ``answer`` is a no-op,
    # which is exactly right.
    return RailResult(text=redact(answer, ungrounded), flagged=True, refused=False, actions=actions)
