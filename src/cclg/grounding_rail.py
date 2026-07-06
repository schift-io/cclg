"""L4 output rail: deterministic answer/context grounding + PII scrub.

Pure Python, zero network calls, zero LLM calls — this module is a rule-based
gate applied *after* an answer has already been generated. Its job is narrow:
does every distinctive value the answer states (a credential-shaped token, a
date, an amount, a proper-noun pair, an email/phone) actually trace back to
the ``grounding_context`` string that was fed to the model? If not, the value
is either (a) not truly derived from the source material (hallucination), or
(b) an unauthorized confirmation of something the model should not be
restating (e.g. echoing back a deleted/retired credential fragment from the
attacker's own query premise). Either way, the safe action is to redact it —
or, if the answer is mostly ungrounded content, refuse outright.

This exists because of a documented real-world failure mode (GateMem Office
Mode 1 eval, see ``docs/GATEMEM_OFFICE.md`` in the schift-monorepo): the
governance/context-leak rate was already 0% (the retrieval layer never
surfaced protected memories), yet answer-level privacy/deletion violations
were still 3.5%/5.0% because the *answer model itself* confirmed or
reconstructed a value it was never given ("Yes, the deleted token began with
rb_stg"). A context-leak rail can't catch that — only an answer<->context
invariant can.

Two independent value classes are checked, with different default postures:

- ``pii_spans`` (email / phone / credential-shaped token): treated as
  default-deny. A span in this class is kept only if it can be proven to
  appear in ``grounding_context`` (case-insensitive substring match); with no
  grounding_context at all, every pii-shaped span is redacted. This is the
  right posture for a downstream safety-net layer that has no access to the
  original retrieval context (see ``check_grounding`` behavior below) — a
  reply should essentially never be reciting a bare credential/phone/email
  unless the source material actually said so.
- ``distinctive_spans`` (dates / amounts / proper-noun pairs): only evaluated
  when ``grounding_context`` is non-empty. With no context to compare
  against, this category is skipped entirely rather than guessed at — a
  context-free layer blindly redacting every date or number in a reply would
  make normal answers unusable (see the over-scrub guard tests).

Escalation: when any *credential-shaped* span is found ungrounded, or when
half or more of the extracted candidate spans are ungrounded, the whole
answer is replaced with a fixed refusal string instead of a partial
redaction. Partial redaction of a single confirmed credential fragment ("the
token began with [REDACTED]") still leaks the fact that a value existed and
was confirmed — which is itself the violation this rail exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Span extraction — deterministic regexes only, no ML/heuristic scoring.
# --------------------------------------------------------------------------- #

# NOTE on boundaries: plain ``\b`` is a *Unicode* word boundary in Python's
# ``re`` -- Hangul syllables are ``\w`` just like ASCII letters/digits, so a
# value directly followed by a Korean grammatical particle with no space
# ("rb_stg_abc123로", "2026-08-01입니다", "1,000,000원입니다") has NO boundary
# between the value and the particle and a trailing ``\b`` silently fails to
# match at all. Korean routinely glues particles onto values/dates/amounts
# with zero separator, so this is not an edge case -- it is the common case
# for this rail's actual production traffic. Every pattern below therefore
# uses an explicit ASCII-only lookaround (``(?<![A-Za-z0-9...])`` /
# ``(?![A-Za-z0-9...])``) instead of ``\b`` wherever the token must not be
# swallowed by a longer ASCII/digit run, while still allowing a Hangul
# particle to sit directly against it.

# Credential/token/code-like spans: "rb_stg_abc123", "mp_stg_4R2N-K8QM-7T1C".
# Filtered to spans containing at least one digit (bare identifiers like
# "some_var_name" are not distinctive enough to be a "value").
#
# Known gap: a *bare prefix* confirmation with no digit at all (e.g. "the
# token began with rb_stg", stopping short of the digit-bearing suffix) is
# NOT caught by this filter -- loosening it to fire on any underscore/dash
# join regardless of digits was tried and rejected: it also matches ordinary
# compound words ("AI-based", "well_known"), which is exactly the over-scrub
# failure mode this rail must avoid. A precise fix would need a second,
# narrower signal (e.g. co-occurrence with retrospective language --
# "deleted"/"removed"/"used to be" -- see cclg.gatemem_pack's
# ``_RETROSPECTIVE_MARKER_RE`` for the same idea applied elsewhere) rather
# than loosening this filter; deferred rather than risking new false
# positives under time pressure.
_CREDENTIAL_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+(?![A-Za-z0-9_-])"
)

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)

# Korean mobile + generic international phone shapes.
_PHONE_RE = re.compile(r"(?<![0-9])(?:01[016789]|0\d{1,2})[-.\s]?\d{3,4}[-.\s]?\d{4}(?![0-9])")

# English Title-Case proper-noun pairs ("Meridian Freight"). Known limitation:
# does not match Korean names/entities — see module docstring in callers.
_PROPER_NOUN_PAIR_RE = re.compile(r"(?<![A-Za-z])[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+(?![A-Za-z])")

# Dates: ISO/slash-separated and Korean "YYYY년 M월 D일" / "M월 D일". The
# Korean alternatives end in a literal Hangul character ("일") that is itself
# the terminator -- no trailing lookaround needed/possible there (the next
# character, if any, is almost always another Hangul particle glued on).
_DATE_RE = re.compile(
    r"(?<![0-9])\d{4}[-./]\d{1,2}[-./]\d{1,2}(?![0-9])"
    r"|\d{4}년\s*\d{1,2}월\s*\d{1,2}일"
    r"|\d{1,2}월\s*\d{1,2}일"
)

# Amounts: comma-grouped numbers ("1,234,567") or a bare number with a unit
# suffix. Bare digit runs shorter than 4 digits are deliberately excluded
# below (see _BARE_DIGITS_RE) to avoid flagging ordinary small counts
# ("3개", "12명") as "distinctive values" — the over-scrub guard this rail
# must respect. ``_UNIT_AMOUNT_RE`` ends in a literal Korean/ASCII unit token
# that is its own terminator, same reasoning as the Korean date alternatives.
_GROUPED_AMOUNT_RE = re.compile(r"(?<![0-9,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![0-9])")
_UNIT_AMOUNT_RE = re.compile(r"(?<![0-9])\d+(?:\.\d+)?\s?(?:원|달러|USD|%|퍼센트)")
_BARE_DIGITS_RE = re.compile(r"(?<![0-9])\d{4,}(?![0-9])")

_REDACTION_MARKER = "[REDACTED]"
_REFUSAL_TEXT = (
    "요청하신 내용을 확인해 드릴 수 없습니다 — 제공된 자료에서 근거를 찾을 수 없는 "
    "값이 포함되어 있어 답변을 보류합니다."
)

# Escalate a partial redaction to a full refusal once at least this share of
# extracted candidate spans turn out to be ungrounded.
_REFUSAL_UNGROUNDED_RATIO = 0.5


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _dedup_keep_order(spans: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for span in spans:
        key = span.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(span)
    return out


def _extract_pii_spans(text: str) -> list[str]:
    spans: list[str] = []
    spans.extend(_EMAIL_RE.findall(text))
    spans.extend(_PHONE_RE.findall(text))
    spans.extend(span for span in _CREDENTIAL_SPAN_RE.findall(text) if any(ch.isdigit() for ch in span))
    return _dedup_keep_order(spans)


def _extract_distinctive_spans(text: str) -> list[str]:
    dates = _DATE_RE.findall(text)
    amounts = _GROUPED_AMOUNT_RE.findall(text) + _UNIT_AMOUNT_RE.findall(text)
    # A bare digit run that is merely a substring of an already-extracted
    # date/amount (e.g. the "2026" inside "2026-07-10") would otherwise be
    # double-counted as a second, independent ungrounded candidate for the
    # same underlying value -- skip those.
    already = dates + amounts
    bare_digits = [d for d in _BARE_DIGITS_RE.findall(text) if not any(d in span for span in already)]
    proper_nouns = _PROPER_NOUN_PAIR_RE.findall(text)
    return _dedup_keep_order(dates + amounts + bare_digits + proper_nouns)


def _is_credential_shaped(span: str) -> bool:
    return bool(_CREDENTIAL_SPAN_RE.fullmatch(span)) and any(ch.isdigit() for ch in span)


def _redact(text: str, spans: list[str]) -> str:
    # Longest-first so a shorter span that is a substring of a longer one
    # (e.g. a date fragment inside a longer amount string) never corrupts an
    # already-redacted region.
    for span in sorted(set(spans), key=len, reverse=True):
        text = re.sub(re.escape(span), _REDACTION_MARKER, text, flags=re.IGNORECASE)
    return text


# --------------------------------------------------------------------------- #
# Public result shape.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RailAction:
    kind: str  # "pii" | "grounding"
    span: str


@dataclass(frozen=True)
class RailResult:
    text: str
    flagged: bool = False
    refused: bool = False
    actions: tuple[RailAction, ...] = field(default_factory=tuple)


def apply_output_rail(answer: str, *, grounding_context: str = "") -> RailResult:
    """Deterministic grounding + PII rail. See module docstring for the rule.

    ``grounding_context`` is the raw text the model actually had available
    when it produced ``answer`` (tool-result observations / RAG context /
    prompt context — whatever was fed in). An empty string means "no context
    available at this layer" (e.g. a downstream safety-net policy that only
    sees the final reply string): in that case the PII-shaped category still
    applies (default-deny), but the broader distinctive-value category is
    skipped entirely rather than guessed at.
    """
    if not answer or not answer.strip():
        return RailResult(text=answer)

    grounding_norm = _normalize(grounding_context)
    has_context = bool(grounding_norm)

    pii_candidates = _extract_pii_spans(answer)
    ungrounded_pii = [span for span in pii_candidates if _normalize(span) not in grounding_norm]

    distinctive_candidates: list[str] = []
    ungrounded_distinctive: list[str] = []
    if has_context:
        distinctive_candidates = _extract_distinctive_spans(answer)
        ungrounded_distinctive = [
            span for span in distinctive_candidates if _normalize(span) not in grounding_norm
        ]

    ungrounded = _dedup_keep_order(ungrounded_pii + ungrounded_distinctive)
    if not ungrounded:
        return RailResult(text=answer)

    actions = tuple(
        RailAction(kind="pii" if span in ungrounded_pii else "grounding", span=span) for span in ungrounded
    )

    total_candidates = len(_dedup_keep_order(pii_candidates + distinctive_candidates)) or len(ungrounded)
    ungrounded_ratio = len(ungrounded) / total_candidates if total_candidates else 1.0
    escalate = any(_is_credential_shaped(span) for span in ungrounded) or ungrounded_ratio >= _REFUSAL_UNGROUNDED_RATIO

    if escalate:
        return RailResult(text=_REFUSAL_TEXT, flagged=True, refused=True, actions=actions)

    return RailResult(text=_redact(answer, ungrounded), flagged=True, refused=False, actions=actions)
