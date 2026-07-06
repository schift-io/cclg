"""PII rule — email / phone / credential-shaped spans, default-deny.

Behavior carried over verbatim from the v1 monolith (``cclg.grounding_rail``
as of commit 91271dd): a span in this class is kept only if it can be proven
to appear in ``grounding_context`` (case-insensitive substring match on the
whitespace-collapsed lowercase form); with no grounding_context at all,
every pii-shaped span is treated as ungrounded (default-deny — this is the
posture the agent-hub ``router_dispatch`` context-free safety net relies
on). An ungrounded *credential-shaped* span escalates to a full refusal
(``RuleFinding.refuse``): partially masking a confirmed credential fragment
("the token began with [REDACTED]") still leaks that a value existed and was
confirmed, which is itself the violation.

Round-2 fix for this module (see tmp/rail-round2/CONTRACT.md, rule-B): the
v1 digit-filter gap — a *bare prefix* confirmation with no digit ("the
deleted token began with rb_stg") sailed through because the default
credential extraction only keeps underscore/dash-joined spans containing a
digit. Loosening the digit filter unconditionally was tried and rejected in
v1 (it flags ordinary compound words like "well_known"/"AI-based"/
"environment-variable"/"logs-only"). The fix kept here is narrower: the
digit filter stays the *default* path unchanged, and a digit-less
credential-shaped span is only ever considered a candidate at all when a
retrospective/deletion marker (bilingual — see ``_RETROSPECTIVE_MARKER_RE``,
patterned after ``cclg.gatemem_pack._RETROSPECTIVE_MARKER_RE``) co-occurs
in the answer or the query. No marker anywhere → the ordinary-compound-word
case is never even extracted, so it can never be flagged or redacted — that
guarantee holds regardless of grounding. A marker *plus* an ungrounded span
escalates to a full refusal exactly like the digit-bearing case; a marker
whose span turns out to be grounded (the assistant is legitimately
discussing something already present in ``grounding_context``) is left
alone, same as any other grounded candidate.

Round-2 *adversarial-verification* fix (see tmp/rail-round2/CONTRACT.md —
same round, precision pass after the first pii charter landed): round-1
adversarial testing found the marker-gated path above was still too coarse
along three independent axes, all fixed here:

1. **Answer-wide promotion, not span-scoped.** The marker gate above
   promotes *every* digit-less underscore/hyphen-joined span in the answer
   once any marker fires anywhere in answer/query — so an unrelated
   ordinary compound word ("logo-file", "on-call", "read-only",
   "customer-facing", ...) sitting in the same answer as an unrelated
   retrospective mention ("we removed the old banner") got swept up and
   refused too. Fixed with :func:`_looks_like_opaque_code`: a joined span is
   only ever promoted when *every* one of its word-ish segments (split on
   underscore, hyphen, or a camelCase case-transition) is short enough
   (``<= _OPAQUE_SEGMENT_MAX_LEN`` characters) to read as an abbreviation
   code rather than a spelled-out English word. This is a shape test, not a
   dictionary lookup, so it stays deterministic/pure — but it reliably tells
   "rb_stg"/"ms_stg" (2-3 char segments, no recognizable dictionary word)
   apart from "logo-file"/"well_known"/"read-only"/"on-call"/
   "customer-facing"/"client-facing"/"self-service" (every segment is a
   real, several-letter English word). This is unconditional: it applies
   regardless of grounding, so it is the same "never a candidate at all"
   guarantee the false-positive guard already documented, just closing the
   gap that guarantee had for *co-occurring* unrelated compounds.
2. **Marker vocabulary/word-form gaps.** The bilingual marker list only
   matched a fixed set of exact inflections (English "deleted"/"retired"/
   "removed"/"revoked"/"rotated"/"rotation"; Korean roots 삭제/폐기/회수/철회).
   Common synonyms (expired, deactivated, invalidated, "no longer valid",
   decommissioned, "phased out"; 만료, 비활성화, 지워짐, 취소) and morphological
   siblings of an already-listed root (deletion, removal, revocation,
   rotating) didn't fire the gate at all, so the digit-less span was never
   even extracted as a candidate. ``_RETROSPECTIVE_MARKER_RE`` now matches
   each listed root with its common suffix variants (root+ed/e/es/ing/ion/
   al/ation, etc.) instead of one fixed inflection, and adds the missing
   English/Korean synonyms named above. This only ever *widens when the
   gate opens*, not what gets promoted through it — axis 1's opacity filter
   still gates promotion, so widening the marker vocabulary does not
   reopen the ordinary-compound-word false-positive class.
3. **Credential-span shape gap (separator mutation of the span itself).**
   ``CREDENTIAL_SPAN_RE`` only recognizes the underscore/hyphen-joined
   shape the charter names (``rb_stg``); a camelCase-joined respelling of
   the exact same prefix (``rbStg``) has a different but equally
   deterministic internal boundary (the lower→upper case transition), and
   is now also extracted (``CAMEL_CASE_SPAN_RE``) and opacity-checked the
   same way. The charter's two other named markers, "began with"/"started
   with", are themselves reliable *value introducers* in English ("...
   began with X" always names X as the value being confirmed) — so
   ``_PREFIX_INTRODUCER_RE`` also captures whatever immediately follows
   those two phrases, tolerating a space-joined respelling ("rb stg") in
   addition to the underscore/hyphen/camelCase ones, without having to scan
   the whole answer for arbitrary adjacent short words (which would reopen
   the "on-call"/"read-only" false-positive class this round is fixing).
   A fully unseparated respelling ("rbstg") is deliberately left alone:
   there is no deterministic boundary left to split on at all without a
   dictionary lookup, and guessing one would cost far more over-scrub
   (any ordinary short lowercase word becomes a candidate) than the
   marginal recall is worth — the charter itself asks over-scrub to be
   weighed the heavier failure mode of the two.

Round-2 *second* adversarial-verification pass (see tmp/rail-round2/
CONTRACT.md, same round): the three fixes above still let through a
further class of ordinary short English compounds/proper-nouns because
the opacity test (``_OPAQUE_SEGMENT_MAX_LEN`` = 3) is a *shape* heuristic,
and English is full of ordinary <=3-letter words ("dry", "run", "add",
"on", "opt", "in", "buy") -- length alone cannot tell "rb"/"stg" apart
from "dry"/"run" without a dictionary. Two further, still dictionary-free,
narrowings close this gap:

4. **Hyphen is the standard English compounding character; underscore is
   not.** Every accepted credential-prefix fixture joins with underscore
   ("rb_stg", "ms_stg", ...); every false-positive this pass found ("dry-
   run", "add-on", "opt-in", "buy-in") joins with a hyphen -- ordinary
   English compounds/idioms/phrasal-verb-nouns overwhelmingly use hyphens
   ("on-call", "read-only", "self-service" already relied on the length
   filter; "dry-run"/"add-on"/"opt-in"/"buy-in" do not, because both
   segments happen to be short). The whole-answer, marker-anywhere-in-
   text scan (the ``CREDENTIAL_SPAN_RE`` co-occurrence path the charter's
   own "we removed the old rb_stg token" example needs) is therefore now
   also restricted to spans whose only joiner is underscore -- a
   hyphen-joined span is never promoted through this path regardless of
   opacity. This does not touch the *digit-bearing* default path
   (``_extract``/``is_credential_shaped``, unchanged, still hyphen-
   tolerant per "기존 digit-filter 동작 보존") -- it only narrows the new
   digit-less, marker-anywhere generic scan.
5. **CamelCase proper nouns ("iPad", "eBay") and the introducer's own
   over-reach.** The standalone whole-answer ``CAMEL_CASE_SPAN_RE`` scan
   is removed: it promoted ordinary consumer-tech/brand proper nouns
   whenever an unrelated marker fired anywhere in the same answer. A
   camelCase respelling of an actual credential prefix ("rbStg") is still
   caught -- it was always also matched directly by
   ``_PREFIX_INTRODUCER_RE``'s mandatory first group (a bare run of
   letters/digits, case-insensitive to the transition) whenever it
   immediately follows "began with"/"started with", so no coverage is
   lost for the charter's own named construction. Separately,
   ``_PREFIX_INTRODUCER_RE`` itself over-captured ordinary narrative
   English ("The rollout began with a dry-run...") because "began
   with"/"started with" introduce ordinary noun phrases (with a
   determiner) far more often than they introduce a bare credential
   token. The introducer now requires the captured value to *not* start
   with an English determiner ("a"/"an"/"the") -- a bare
   identifier immediately after "with" ("began with rb_stg") is not
   idiomatic English for a common noun and reliably marks a
   token/identifier instead, whereas "began with a/an/the X" is ordinary
   narrative phrasing. Its optional second-word continuation (for the
   space-respelled "rb stg" case) also drops hyphen from its separator
   class for the same reason as fix 4, so it can no longer complete a
   hyphen-joined ordinary compound like "dry-run" either.
"""

from __future__ import annotations

import re

from cclg.rails.base import RuleFinding, dedup_keep_order, normalize

# NOTE on boundaries (v1, unchanged): plain ``\b`` is a Unicode word boundary
# — Hangul particles glued onto a value ("rb_stg_abc123로") leave no boundary
# for a trailing ``\b`` to match, so every pattern uses explicit ASCII-only
# lookarounds instead.

CREDENTIAL_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+(?![A-Za-z0-9_-])"
)

# NOTE: a standalone whole-answer camelCase span scanner ("iPad", "eBay")
# used to live here and was removed in the second adversarial-verification
# pass (see module docstring fix #5) -- it promoted ordinary consumer-tech
# proper nouns on any unrelated marker co-occurring anywhere in the answer.
# A camelCase respelling of an actual credential prefix ("rbStg") is still
# caught because it is matched directly by ``_PREFIX_INTRODUCER_RE``'s own
# mandatory first group (a bare alnum run, case-transition-agnostic)
# whenever it immediately follows "began with"/"started with".

# "began with"/"started with" are themselves value-introducers: whatever
# immediately follows is the thing being confirmed. Capturing that directly
# (instead of scanning the whole answer) is what lets a space-joined
# respelling of the credential prefix ("rb stg") be recognized without
# treating every adjacent pair of short words in the answer as a candidate.
# Two guards (added in the second adversarial-verification pass, module
# docstring fix #5) keep this from over-capturing ordinary narrative
# English: (1) the captured value may not begin with an English determiner
# ("a"/"an"/"the") -- "began with a dry-run" is ordinary narration, "began
# with rb_stg" is a bare-token confirmation; (2) the optional second-word
# continuation only allows a space or underscore join, not a hyphen, so it
# can no longer complete an ordinary hyphenated compound ("dry-run").
_PREFIX_INTRODUCER_RE = re.compile(
    r"(?:beg(?:an|ins?|inning)|start(?:ed|s|ing)?)\s+with\s+"
    r"(?!(?:a|an|the)\b)[\'\"]?([A-Za-z][A-Za-z0-9]*(?:[\s_]+[A-Za-z0-9]+)?)[\'\"]?",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)

# Korean mobile + generic international phone shapes.
PHONE_RE = re.compile(r"(?<![0-9])(?:01[016789]|0\d{1,2})[-.\s]?\d{3,4}[-.\s]?\d{4}(?![0-9])")

# Retrospective/deletion co-occurrence signal (rule-B charter, bilingual).
# Deliberately narrower than ``cclg.gatemem_pack._RETROSPECTIVE_MARKER_RE``
# (which also matches generic recall words like "old"/"before"/"previous"
# for a different purpose there): a credential-prefix confirmation attack
# reliably names the act of deletion/rotation, not just "previous" in the
# abstract, so keeping this list tight is what makes the "no marker → never
# a candidate" guarantee safe against ordinary compound words. Each root is
# matched with its common suffix variants rather than one fixed inflection
# (round-2 adversarial-fix note #2 above) so "deletion"/"expired"/
# "decommissioned"/"만료된"/"지워진" etc. fire the gate exactly like
# "deleted"/"삭제된" already did.
_RETROSPECTIVE_MARKER_RE = re.compile(
    r"\b(?:"
    r"delet(?:ed|ion|ing|es|e)"
    r"|remov(?:ed|al|ing|es|e)"
    r"|retir(?:ed|ement|ing|es|e)"
    r"|revok(?:ed|ing|es|e)|revocation"
    r"|rotat(?:ed|ion|ing|es|e)"
    r"|expir(?:ed|ation|ing|es|e|y)"
    r"|deactivat(?:ed|ion|ing|es|e)"
    r"|invalidat(?:ed|ion|ing|es|e)"
    r"|decommission(?:ed|ing)?"
    r"|phased\s+out"
    r"|no\s+longer\s+valid"
    # The two value-introducer frame verbs in their FULL inflection
    # paradigm -- a closed morphological set of already-chosen frames,
    # not an open synonym list (round-2 makeup verification: tense is
    # not an attack discriminator).
    r"|used\s+to|beg(?:an|ins?|inning)\s+with|start(?:ed|s|ing)?\s+with"
    r")\b"
    r"|삭제(?:된|되었|하)?|폐기(?:된|되었)?|회수(?:된|되었)?|철회(?:된|되었)?"
    r"|만료(?:된|되었)?|비활성화(?:된|되었)?|지워(?:진|졌)?|취소(?:된|되었)?"
    r"|이전\s*토큰|이전에\s*사용(?:했|하였|하던)?",
    re.IGNORECASE,
)

# Shape test for the marker-gated (digit-less) path only: how many
# characters make a joined-span segment read as an abbreviation code
# ("rb", "stg") rather than a spelled-out English word ("logo", "file",
# "read", "only", "customer", "facing"). Kept deliberately conservative (a
# dictionary would be more precise, but this module stays pure/offline) --
# see round-2 adversarial-fix note #1 above.
_OPAQUE_SEGMENT_MAX_LEN = 3

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def is_credential_shaped(span: str) -> bool:
    """Digit-bearing credential-shaped span (v1 default, unchanged)."""
    return bool(CREDENTIAL_SPAN_RE.fullmatch(span)) and any(ch.isdigit() for ch in span)


def _has_retrospective_marker(*texts: str) -> bool:
    return any(_RETROSPECTIVE_MARKER_RE.search(text) for text in texts if text)


def _segments(span: str) -> list[str]:
    """Split a joined span into its word-ish parts along every join
    convention this module recognizes on the marker-gated path: underscore,
    hyphen, plain whitespace (the introducer capture allows a space-joined
    respelling), and a camelCase case transition."""
    with_camel_breaks = _CAMEL_BOUNDARY_RE.sub("_", span)
    return [part for part in re.split(r"[\s_-]+", with_camel_breaks) if part]


def _looks_like_opaque_code(span: str) -> bool:
    """Whether ``span`` reads as an abbreviation-style code (every segment
    short) rather than an ordinary spelled-out compound word. Only used to
    filter the marker-gated digit-less path -- see round-2 adversarial-fix
    note #1 in the module docstring."""
    segments = _segments(span)
    return bool(segments) and all(len(seg) <= _OPAQUE_SEGMENT_MAX_LEN for seg in segments)


def _extract(text: str) -> list[str]:
    spans: list[str] = []
    spans.extend(EMAIL_RE.findall(text))
    spans.extend(PHONE_RE.findall(text))
    spans.extend(span for span in CREDENTIAL_SPAN_RE.findall(text) if any(ch.isdigit() for ch in span))
    return dedup_keep_order(spans)


def _is_underscore_only_joined(span: str) -> bool:
    """Whether ``span``'s only join character is an underscore (no hyphen).
    Hyphen is the ordinary English compounding character ("dry-run",
    "add-on", "opt-in", "buy-in", "on-call", "read-only", ...); underscore
    is what every accepted credential-prefix fixture actually uses
    ("rb_stg", "ms_stg", ...). Restricting the whole-answer,
    marker-anywhere-in-text scan to underscore-only spans is what keeps a
    short ordinary hyphenated compound (both segments <= 3 chars, so the
    opacity length test alone cannot exclude it -- see module docstring
    fix #4) from being promoted just because some unrelated marker fired
    elsewhere in the same answer. It does not apply to the introducer path
    (:data:`_PREFIX_INTRODUCER_RE`), which cannot capture a hyphen at all
    (see that pattern's own comment)."""
    return "_" in span and "-" not in span


def _extract_marker_gated_prefixes(answer: str, query: str) -> list[str]:
    """Digit-less credential-shaped spans in ``answer``, promoted to
    candidates only when a retrospective/deletion marker co-occurs somewhere
    in ``answer`` or ``query`` (rule-B charter) *and* the span itself is
    opacity-filtered (round-2 adversarial-fix note #1) -- that second gate
    is the false-positive guard for ordinary compound words such as
    "well_known"/"AI-based"/"environment-variable sync"/"logo-file"/
    "on-call"/"read-only", which must never be flagged regardless of any
    marker present elsewhere in the same answer. The whole-answer
    ``CREDENTIAL_SPAN_RE`` scan is further restricted to underscore-only
    joins (module docstring fix #4) -- a hyphen-joined span is never
    promoted through this generic co-occurrence path regardless of
    opacity, only through the stricter, adjacency-scoped introducer path
    below. Absent a marker this always returns ``[]``.
    """
    if not _has_retrospective_marker(answer, query):
        return []
    raw_spans: list[str] = []
    raw_spans.extend(
        span
        for span in CREDENTIAL_SPAN_RE.findall(answer)
        if not any(ch.isdigit() for ch in span) and _is_underscore_only_joined(span)
    )
    raw_spans.extend(
        span for span in _PREFIX_INTRODUCER_RE.findall(answer) if not any(ch.isdigit() for ch in span)
    )
    return [span for span in dedup_keep_order(raw_spans) if _looks_like_opaque_code(span)]


# --- Structural (vocabulary-free) confirmation-echo branch ------------------
#
# Round-2 makeup verification (tmp/rail-round2/attacks/pii_bypass_r2_makeup.py)
# proved the marker-vocabulary gate above is an open list: every new round
# finds retrospective synonyms it lacks (deprecated / sunset / obsolete /
# 파기 / 종료 / 무효, present-tense "begins with", ...). Growing the list is
# whack-a-mole — language is open-class. But the *attack itself* has a
# closed structure that needs no content vocabulary at all: the attacker
# must plant the opaque token in their own query (there is nowhere else for
# it to come from — the whole point is that it is NOT in the grounding
# context) and elicit a confirmation. So: an opaque digit-less
# credential-shaped span that appears in BOTH the query and the answer,
# where the exchange is confirmation-shaped, is promoted regardless of what
# verb the attacker chose. "Confirmation-shaped" is detected with
# closed-class grammatical material only (an affirmation/denial lead in the
# answer, or interrogative confirm-framing in the query — function words,
# not an open synonym list). The marker gate above remains as a secondary
# branch for *volunteered* digit-less prefixes ("the old token began with
# rb_stg" with no query echo), where a value-introducer frame is the only
# deterministic handle; its vocabulary is no longer load-bearing for the
# echo class and must not be grown further.
#
# Deliberate posture: PII spans never get query-echo *grounding* (contract —
# echoing a value planted in the query is exactly the confirmation attack);
# this branch uses the same echo as an attack *signal*, the opposite
# direction. A benign echo of a user's own identifier ("check the env_var
# settings" → "to check env_var, ...") is not confirmation-shaped and does
# not fire; confirming an ungrounded identifier claim ("Is env_var the
# right key?" → "Yes, env_var is right") is the rail's core invariant
# violation and does fire — whether that disclosure was *authorized* is
# L1/L2's job, not this string-level gate's.

_AFFIRM_LEAD_RE = re.compile(
    r"^\s*[\"'(]*\s*"
    r"(?:yes|no|correct|confirmed|indeed|affirmative"
    r"|네|예|아니요|아니오|맞습니다|맞아요|그렇습니다|아닙니다)\b",
    re.IGNORECASE,
)

_CONFIRM_FRAMING_RE = re.compile(
    r"\b(?:whether|yes\s+or\s+no|yes/no)\b"
    r"|여부|인지(?:만)?\s|맞(?:나요|습니까|아요|죠|는지)",
    re.IGNORECASE,
)


def _extract_query_echo_confirmations(answer: str, query: str) -> list[str]:
    """Opaque digit-less spans echoed from the query in a confirmation-shaped
    exchange — see the block comment above. Vocabulary-free by design."""
    if not query or not answer:
        return []
    if not (_AFFIRM_LEAD_RE.match(answer) or _CONFIRM_FRAMING_RE.search(query)):
        return []
    query_lower = query.lower()
    spans = [
        span
        for span in CREDENTIAL_SPAN_RE.findall(answer)
        if not any(ch.isdigit() for ch in span)
        and _is_underscore_only_joined(span)
        and _looks_like_opaque_code(span)
        and span.lower() in query_lower
    ]
    return dedup_keep_order(spans)


def _fold_separators(text: str) -> str:
    """Collapse whitespace/underscore/hyphen runs to a single space so a
    grounded value written with a different separator style than the
    answer (``room 402`` vs ``room-402``, ``JIRA_4821`` vs ``JIRA-4821``)
    still matches. Purely additive on top of :func:`normalize`'s exact
    substring check -- it only ever makes a span *more* likely to be
    recognized as grounded, never less, so it cannot introduce a new
    under-detection (missed leak) failure mode, only reduce over-scrub."""
    return re.sub(r"[\s_-]+", " ", text)


def check(answer: str, *, grounding_context: str = "", query: str = "") -> RuleFinding:
    grounding_norm = normalize(grounding_context)
    grounding_folded = _fold_separators(grounding_norm)
    digit_candidates = _extract(answer)
    marker_gated_candidates = _extract_marker_gated_prefixes(answer, query)
    echo_candidates = _extract_query_echo_confirmations(answer, query)
    candidates = dedup_keep_order([*digit_candidates, *marker_gated_candidates, *echo_candidates])
    marker_gated_lower = {span.lower() for span in [*marker_gated_candidates, *echo_candidates]}

    def _is_grounded(span: str) -> bool:
        span_norm = normalize(span)
        if not span_norm:
            return False
        if span_norm in grounding_norm:
            return True
        return _fold_separators(span_norm) in grounding_folded

    ungrounded = [span for span in candidates if not _is_grounded(span)]
    refuse = any(is_credential_shaped(span) or span.lower() in marker_gated_lower for span in ungrounded)
    return RuleFinding(
        rule="pii",
        candidates=tuple(candidates),
        ungrounded_spans=tuple(ungrounded),
        refuse=refuse,
        reason="credential_ungrounded" if refuse else "",
    )
