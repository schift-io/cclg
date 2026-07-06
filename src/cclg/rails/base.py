"""Shared primitives for the L4 output-rail rule modules (``cclg.rails``).

The rail is decomposed into independent rule modules (round 2 of the
GateMem Office Mode 1 precision work — see ``docs/GATEMEM_OFFICE.md`` and
the v1 module docstring history in ``cclg.grounding_rail``):

- ``cclg.rails.pii``             — email / phone / credential-shaped spans,
                                   default-deny posture.
- ``cclg.rails.value_grounding`` — dates / amounts / proper-noun pairs,
                                   only evaluated against a non-empty
                                   grounding context.
- ``cclg.rails.confirmation``    — query-aware confirmation-attack gate
                                   ("Yes, the deleted token began with
                                   rb_stg" / bare "Yes." to a protected
                                   yes-no probe). New in round 2: the v1
                                   value-matching rail measurably could NOT
                                   catch these (privacy answer-leak was
                                   unchanged at 3.51% because the leak is a
                                   relational confirmation, not a value
                                   string).

Every rule module exposes exactly one entry point::

    def check(answer: str, *, grounding_context: str = "", query: str = "") -> RuleFinding

and must stay pure/deterministic: no network, no LLM, no clock, no I/O.
The composing facade (``cclg.grounding_rail.apply_output_rail``) owns the
escalation policy (redact vs refuse) — rule modules only *report* findings
via :class:`RuleFinding`; they never rewrite the answer themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REDACTION_MARKER = "[REDACTED]"

REFUSAL_TEXT = (
    "요청하신 내용을 확인해 드릴 수 없습니다 — 제공된 자료에서 근거를 찾을 수 없는 "
    "값이 포함되어 있어 답변을 보류합니다."
)


@dataclass(frozen=True)
class RuleFinding:
    """One rule module's verdict on a single answer.

    ``candidates`` is every span the rule *extracted and evaluated* (grounded
    or not) — the facade uses the union of all rules' candidates as the
    denominator for its ratio-based escalation, exactly as the v1 monolith
    did. ``ungrounded_spans`` ⊆ ``candidates`` (except for query-side
    evidence spans a rule may report with no answer-side span at all, e.g.
    the confirmation rule flagging a bare "Yes." — those may name the echoed
    query value instead). ``refuse`` means this rule demands the whole answer
    be replaced with :data:`REFUSAL_TEXT` regardless of ratios; ``reason`` is
    a short machine-readable tag for logging/tests ("credential_ungrounded",
    "confirm_attack_deleted_referent", ...).
    """

    rule: str  # "pii" | "value_grounding" | "confirmation"
    candidates: tuple[str, ...] = field(default_factory=tuple)
    ungrounded_spans: tuple[str, ...] = field(default_factory=tuple)
    refuse: bool = False
    reason: str = ""


def normalize(text: str) -> str:
    """Whitespace-collapsed, lowercased comparison form (v1 ``_normalize``)."""
    return " ".join((text or "").strip().lower().split())


def dedup_keep_order(spans: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for span in spans:
        key = span.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(span)
    return out


def redact(text: str, spans: list[str]) -> str:
    """Replace every occurrence of each span with :data:`REDACTION_MARKER`.

    Longest-first so a shorter span that is a substring of a longer one never
    corrupts an already-redacted region (v1 ``_redact``, unchanged).
    """
    for span in sorted(set(spans), key=len, reverse=True):
        text = re.sub(re.escape(span), REDACTION_MARKER, text, flags=re.IGNORECASE)
    return text
