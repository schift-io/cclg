"""Value-grounding rule — dates / amounts / proper-noun pairs vs context.

Only evaluated when ``grounding_context`` is non-empty: with no context to
compare against this category is skipped entirely rather than guessed at
(a context-free layer blindly redacting every date/number would make normal
answers unusable — the v1 over-scrub guard, unchanged).

Round-2 scope for this module (see tmp/rail-round2/CONTRACT.md), from the
measured v3 over-scrub failures (GateMem Mode 1, utility 47.4%→45.5%):

1. **Canonical matching, not literal substring.** A span is grounded if it
   *means* the same value as something in the context/query, even when the
   surface form differs:

   - separator variants (space ↔ ``_``/``-``: "Project Maple" ↔
     "project_maple"), possessives ("Maple's" ↔ "Maple"), and case;
   - date representations ("2026-08-01" ↔ "2026년 8월 1일" ↔ "2026/8/1");
   - amount representations ("1,000,000원" ↔ "100만원" ↔ "1000000",
     "5억" ↔ "500,000,000원") — Korean numeral multipliers (억/만/천/백만)
     are expanded to their integer value for comparison.

   The approach mirrors ``cclg.patches._canonical_value``/``_value_category``
   (correction-detection round 3): bucket a span into a comparison category
   (date / amount / free-text), then compare *canonical* values within that
   category rather than raw characters. Ported here rather than imported --
   this module's unit list and comparison needs (grounding lookup, not
   turn-vs-node contradiction detection) differ from ``patches.py``'s.
2. **Query-echo grounding.** A distinctive span the *user's own query*
   contains is not a leak when echoed back ("What blockers remain for
   Project Maple?" → "Project Maple has no blockers"): ``query`` is an
   additional grounding source for THIS category only, using the same
   canonical-match logic as the context. PII/credential spans must NOT get
   query-echo grounding (echoing a value planted in the query is exactly the
   confirmation attack — that posture belongs to ``cclg.rails.pii`` /
   ``cclg.rails.confirmation``); this module never extracts credential-shaped
   spans in the first place, so the two never overlap.

This module never sets ``refuse`` — hallucinated dates/amounts/names are
neutralized by redaction, and the facade's ratio escalation (with a minimum
candidate count) covers the "answer is mostly fabricated" case.

Round-2 precision pass (post-adversarial-verification, see
tmp/rail-round2/CONTRACT.md rule-A acceptance criteria) additions, all
general-principle fixes rather than fixture-specific patches:

4. **Word/number-boundary-aware whole-text fallback.** The raw ``span in
   haystack`` substring checks (both the literal ``grounding_norm``/
   ``query_norm`` text and the separator-folded generic text) are replaced
   with a boundary-aware search: a candidate must not "ground" merely by
   being a right-/left-aligned digit or letter run *inside* a longer,
   unrelated token ("9,999,000" must not ground by being a tail substring of
   "29,999,000"; "project maple" must not ground by being a prefix substring
   of "project maplemark").
5. **Non-overlapping amount extraction.** Amount extraction now claims
   character spans in priority order (grouped → Korean-multiplier → unit →
   bare-digit) so a comma-grouped amount's own trailing digit group is never
   re-extracted as a second, phantom unit-suffixed candidate ("1,000,000원"
   no longer also yields a spurious "000원").
6. **Common-phrase exclusion.** A multi-word candidate is not a distinctive
   value when it fully collapses into ordinary discourse vocabulary
   (courtesy openers/closings, weekday+greeting combinations) or names a
   well-known public vendor+product-category pair (a major SaaS vendor name
   followed by a generic product-category noun) — common public or
   boilerplate knowledge, not a case-specific fact that could be fabricated.
7. **Script-mixing case-insensitive extraction.** A Latin-letter name run
   embedded directly in Korean prose is extracted as a candidate regardless
   of letter case (ALL-CAPS / lowercase / mixed) — case is not a reliable
   signal for a value inserted across a script boundary, unlike an
   English-language answer, where ``PROPER_NOUN_PAIR_RE``'s Title-Case
   requirement is a reasonable proxy. Deliberately scoped to spans directly
   adjacent to Hangul text (not any-case matching everywhere), so ordinary
   lowercase English prose in an all-English answer doesn't explode into a
   sea of ungrounded candidates.

Round-2, second precision pass (further adversarial verification against the
implementation above) additions -- see tmp/rail-round2/CONTRACT.md:

8. **Script-boundary-aware whole-text matching.** §4's boundary check
   originally rejected a match flanked by *any* alphanumeric/Hangul
   character, on either side, without regard to script. That is too broad
   for Korean prose: a topic/genitive postposition (은/는/이/가/을/를/의/...)
   attaches directly to the preceding noun with **no space**
   ("project_maple은", not "project_maple 은"), so a context-only
   identifier immediately followed by a postposition could never ground a
   Title-Case mention in the answer -- the exact over-scrub the fixture
   acceptance case exercises. The fix distinguishes *same-script*
   continuation (still rejected -- "project maple" must not ground inside
   "project maplemark", "5678" must not ground inside "45678") from
   *cross-script* adjacency (a Latin/digit span directly followed or
   preceded by a Hangul character is not a same-token continuation risk,
   since Korean postpositions/particles are the normal way this boundary
   occurs) -- see ``_char_class``/``_contains_bounded``.
9. **Invisible/zero-width character tolerance.** A zero-width space (ZWSP,
   U+200B) or similar invisible joiner inserted between two words is
   steganographic evasion, not a genuine word boundary difference:
   extraction patterns now accept these characters as a valid inter-word
   separator (so the matched span still preserves the exact source
   substring, for downstream redaction fidelity), the Hangul-adjacency
   boundary check (§7) ignores them when locating the truly-adjacent
   character, and canonical/whole-text comparison forms fold them to an
   ordinary space (so an invisible-character-obfuscated mention still
   canonically equals its plain-space rendering) -- see ``_fold_invisible``.
10. **Compound (chained) Korean numeral-multiplier amounts.** A single
    multiplier character was the only case handled (item 1's "5억"), but
    Korean amounts routinely chain multiple multiplier terms
    ("5억 2천만원" = 5억 + 2천만, "1억5천만원" with no internal space) --
    each multiplier character in a chain *compounds* with the ones after it
    (e.g. "2천만" = 2 × 천(1,000) × 만(10,000) = 20,000,000), and multiple
    space-separated terms *sum*. The extraction pattern and canonical
    parser both now handle this general chained/summed shape rather than
    only a single digit+multiplier pair.
11. **Meeting-idiom vocabulary.** Ordinary meeting-notes section headers and
    scheduling idioms ("Next Steps", "Action Items", "Quick Sync", "Follow
    Up") are, like the courtesy sign-offs and weekday greetings already
    excluded (§6), boilerplate vocabulary that never names a case-specific
    fact -- the same "fully collapses into ordinary vocabulary" test is
    extended with this word set rather than special-casing the exact
    phrases.
12. **Enumerated vendor-product allowlist + narrowly-scoped sign-off
    exemption (replacing the vendor⊗category cross-product).** §6's
    known-vendor + generic-product-category exclusion was a full cross
    product (any known vendor word followed by any generic category word),
    which swallowed *fabricated* entity names that merely reuse this
    vocabulary ("Slack Team", "Zoom Chat", "Adobe Docs" are not real
    product names, yet "slack"+"team"/"zoom"+"chat"/"adobe"+"docs" all
    satisfied the old cross product). A well-known public product name is a
    small, enumerable set, not a combinatorial space, so the exclusion is
    now an explicit allowlist of *specific* well-known product names.
    Separately, "team"/"support" are no longer treated as pure discourse
    vocabulary (that conflated a genuine closing-signature address with a
    factual claim, e.g. "escalated this to the Support Team" -- a
    fabricated, ungrounded claim -- vs. "Best Regards, the Support Team." --
    a boilerplate sign-off): a narrowly-scoped check now excludes a
    trailing "(the) <generic-team-word> Team"-style address only when it is
    both the last substantive content of the answer *and* preceded earlier
    in the text by an explicit courtesy sign-off marker, leaving an
    identical mid-sentence factual mention (no sign-off marker, more text
    follows) fully subject to grounding.
14. **Generic-classifier-stripped name matching.** An answer that says
    "Project Timber" when the context/query only ever writes the bare name
    "Timber" (measured GateMem v4 residual: 3 utility answers with every
    fact grounded lost only the project name to redaction) is a surface
    variation of the same referent, exactly like a possessive or separator
    variant: "Project"/"Team"/"프로젝트" etc. are *classifiers* that add no
    identifying information. Grounding therefore also tries the span with
    generic classifier words stripped from its edges — guarded so the
    stripped remainder must be non-empty AND contain at least one
    non-ordinary word (a fabricated "Project Best" can never ground itself
    off the common word "best" appearing incidentally in the context), and
    the remainder itself still goes through the same script-aware bounded
    containment (§8), so "Timber" never grounds inside "Timberlane".
15. **Fullwidth-digit/comma tolerance for grouped amounts.** Python's
    ``\\d`` already matches fullwidth Unicode digits, so most numeric
    patterns already tolerated a fullwidth-digit forgery -- except
    ``GROUPED_AMOUNT_RE``, whose thousands-separator was a literal ASCII
    ``,``. A fullwidth comma (``，``) defeated grouping entirely, so only a
    trailing fragment of a forged fullwidth amount was ever flagged/
    redacted while most of its digits passed through untouched. The pattern
    (and the canonical-amount parser) now accept the fullwidth comma/period
    as well.
"""

from __future__ import annotations

import re

from cclg.rails.base import RuleFinding, dedup_keep_order, normalize

# Zero-width/invisible Unicode characters usable as a steganographic word
# separator: ZWSP, ZWNJ, ZWJ, WORD JOINER, BOM/ZWNBSP -- see module docstring
# §9. Extraction regexes accept these directly (as an alternative to ``\s``)
# so a matched span still contains the exact source substring; every other
# comparison form in this module runs text through ``_fold_invisible`` first
# so an invisible-character-obfuscated mention canonically equals its
# plain-space rendering.
_INVISIBLE_CHARS = "​‌‍⁠﻿"
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")
_WORD_SEP = rf"[\s{_INVISIBLE_CHARS}]+"


def _fold_invisible(text: str) -> str:
    """Zero-width/invisible characters folded to an ordinary space -- used
    only for *comparison* forms (canonical keys, whole-text boundary
    search), never to rewrite a returned candidate span itself."""
    return _INVISIBLE_RE.sub(" ", text)


# English Title-Case proper-noun pairs ("Meridian Freight"). Known limitation:
# does not match Korean names/entities. A trailing possessive ("Freight's")
# is naturally excluded from the match already (the pattern only consumes
# letter runs), so no separate possessive-stripping is needed for this
# extraction step -- only for the whole-text fallback comparison below.
PROPER_NOUN_PAIR_RE = re.compile(rf"(?<![A-Za-z])[A-Z][a-z]+(?:{_WORD_SEP}[A-Z][a-z]+)+(?![A-Za-z])")

# Dates: ISO/slash-separated and Korean "YYYY년 M월 D일" / "M월 D일".
DATE_RE = re.compile(
    r"(?<![0-9])\d{4}[-./]\d{1,2}[-./]\d{1,2}(?![0-9])"
    r"|\d{4}년\s*\d{1,2}월\s*\d{1,2}일"
    r"|\d{1,2}월\s*\d{1,2}일"
)

# Amounts: comma-grouped numbers, a bare number with a unit suffix, or a
# Korean numeral-multiplier amount ("100만원", "5억"). Bare digit runs
# shorter than 4 digits are deliberately excluded elsewhere (ordinary small
# counts like "3개"/"12명" are not distinctive values). The thousands
# separator/decimal point accept both ASCII and fullwidth forms (module
# docstring §15); ``\d``/lookaround already match fullwidth digits natively.
GROUPED_AMOUNT_RE = re.compile(r"(?<![\d,，])\d{1,3}(?:[,，]\d{3})+(?:[.．]\d+)?(?!\d)")
UNIT_AMOUNT_RE = re.compile(r"(?<![0-9])\d+(?:\.\d+)?\s?(?:원|달러|USD|%|퍼센트)(?![0-9])")

# Round-3 correction-detection fix, ported (see ``cclg.patches._MONEY_MULTIPLIER``):
# a Korean numeral multiplier compounds with a leading digit run into a
# genuine numeral multiplier, so "5억"/"500,000,000원" name the same amount
# despite sharing no literal substring. Round-2 second pass (module docstring
# §10): multiple multiplier characters *chain* (multiply) within one term
# ("2천만" = 2 × 천 × 만) and multiple space-separated terms *sum*
# ("5억 2천만원" = 5억 + 2천만) -- see ``_KOREAN_TERM_BODY``/
# ``KOREAN_MULTIPLIER_AMOUNT_RE``/``_parse_korean_compound_amount`` below.
_MONEY_MULTIPLIER = {"억": 100_000_000, "만": 10_000, "천": 1_000, "백만": 1_000_000}
_KOREAN_UNIT_ALT = "|".join(re.escape(u) for u in sorted(_MONEY_MULTIPLIER, key=len, reverse=True))
_CURRENCY_ALT = r"원|달러|USD|%|퍼센트"

# A single multiplier "term": a digit run followed by one-or-more chained
# multiplier characters (e.g. "5억", "2천만" == "2" + "천" + "만").
_KOREAN_TERM_BODY = rf"\d+(?:\.\d+)?(?:{_KOREAN_UNIT_ALT})+"

# The full compound amount: one-or-more terms, each pair optionally joined by
# a single space (a genuine second term must actually follow the space for it
# to be consumed -- a dangling trailing space before unrelated text is never
# absorbed into the match), plus an optional trailing currency word.
KOREAN_MULTIPLIER_AMOUNT_RE = re.compile(
    rf"(?<![0-9])"
    rf"{_KOREAN_TERM_BODY}(?:\s?{_KOREAN_TERM_BODY})*"
    rf"(?:\s?(?:{_CURRENCY_ALT}))?"
    rf"(?![0-9])"
)
BARE_DIGITS_RE = re.compile(r"(?<![0-9])\d{4,}(?![0-9])")

# Priority order for non-overlapping numeric extraction (most-specific/
# longest pattern first): once a pattern claims a character span, a later
# pattern's match that overlaps it is dropped rather than re-extracted as a
# second, phantom candidate -- see ``_extract_nonoverlapping`` (round-2 fix
# for the comma-grouped-amount + trailing-unit-word double extraction).
_NUMERIC_PATTERNS = (
    DATE_RE,
    GROUPED_AMOUNT_RE,
    KOREAN_MULTIPLIER_AMOUNT_RE,
    UNIT_AMOUNT_RE,
    BARE_DIGITS_RE,
)

# A Latin-letter name run (any case) is only extracted as a candidate when it
# sits directly next to Hangul text -- a script-mixing signal for "foreign
# name/value inserted into a Korean sentence" that doesn't depend on Title
# Case (round-2 fix for ALL-CAPS/lowercase/mixed-case hallucinated names
# otherwise being invisible to extraction entirely). Deliberately NOT applied
# unconditionally: matching every lowercase multi-word run in an English
# answer would explode the candidate count and reintroduce over-scrub.
_CASE_INSENSITIVE_NAME_RE = re.compile(rf"(?<![A-Za-z])[A-Za-z]{{2,}}(?:{_WORD_SEP}[A-Za-z]{{2,}}){{1,}}(?![A-Za-z])")
_HANGUL_RE = re.compile(r"[가-힣]")

# A multi-word candidate that fully collapses into ordinary discourse
# vocabulary (courtesy openers/closings, weekday+greeting combinations) is
# not a distinctive value -- it is boilerplate, not a fact that could be
# fabricated (round-2 fix for "Thank You" / "Best Regards" / "Happy Monday"
# being extracted and, with nothing to match in an unrelated context,
# escalating an otherwise-correct answer to full refusal). "team"/"support"
# are deliberately NOT in this set (round-2 second pass, module docstring
# §12) -- they are ordinary nouns that can also name a genuine (possibly
# fabricated) organizational entity ("the Support Team escalated this"),
# unlike a pure discourse marker; the narrower, position-aware
# ``_is_trailing_signoff_reference`` below covers the boilerplate
# closing-signature case instead.
_COMMON_DISCOURSE_WORDS = frozenset(
    {
        "thank", "thanks", "you", "please", "best", "regards", "sincerely",
        "dear", "warm", "wishes", "kind", "hello", "hi", "happy", "good",
        "morning", "afternoon", "evening", "night",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    }
)

# Ordinary meeting-notes section headers / scheduling idioms (round-2 second
# pass, module docstring §11) -- boilerplate vocabulary in the same sense as
# the discourse words above, just drawn from meeting-notes jargon rather than
# greetings/sign-offs.
_COMMON_MEETING_IDIOM_WORDS = frozenset({"next", "steps", "action", "items", "quick", "sync", "follow", "up"})

_ORDINARY_VOCABULARY = _COMMON_DISCOURSE_WORDS | _COMMON_MEETING_IDIOM_WORDS

# A small, *enumerated* set of genuinely well-known multi-word public product
# names (round-2 second pass, module docstring §12 -- replacing a
# vendor-word × generic-category-word cross product, which incorrectly
# excluded fabricated combinations like "Slack Team"/"Zoom Chat"/"Adobe
# Docs" just because they reuse a known vendor name and a generic category
# noun). A well-known product name is common public knowledge; an arbitrary
# vendor+category combination is not.
_KNOWN_VENDOR_PRODUCT_NAMES = frozenset(
    {
        "google calendar", "google drive", "google docs", "google sheets",
        "google slides", "google meet", "google forms",
        "microsoft teams", "microsoft office", "microsoft excel",
        "microsoft word", "microsoft powerpoint", "microsoft outlook",
    }
)

# A trailing "(the) <word> Team"-style address is boilerplate courtesy
# signature -- NOT a factual claim -- only when it is both the final
# substantive content of the answer and preceded earlier in the text by an
# explicit sign-off marker (round-2 second pass, module docstring §12).
# Scoped to generic organizational-unit nouns so a genuine name in the same
# signature slot ("Best Regards, Alice Wonderland.") is never suppressed.
_GENERIC_TEAM_REFERENCE_WORDS = frozenset({"team", "support", "desk", "department", "help"})
_SIGNOFF_MARKER_RE = re.compile(r"thank\s+you|thanks|regards|sincerely|warm\s+wishes", re.IGNORECASE)
_TRAILING_PUNCT_ONLY_RE = re.compile(r"^[\s.!?~]*$")

# --- canonicalization -------------------------------------------------

_ISO_DATE_RE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$")
_KOREAN_FULL_DATE_RE = re.compile(r"^(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일$")
_KOREAN_MD_DATE_RE = re.compile(r"^(\d{1,2})월\s*(\d{1,2})일$")

# Compound-amount validator/parser (module docstring §10): the full span must
# consist of one-or-more chained-multiplier terms (each pair optionally
# joined by a single space) plus an optional trailing currency word, with
# nothing else -- a genuine amount, not a coincidental multiplier substring
# inside unrelated text. ``_KOREAN_TERM_RE`` (named groups) is used only via
# ``finditer`` to sum each term's value; it is kept separate from the
# validator regex because a named group cannot be repeated within one
# pattern.
_KOREAN_COMPOUND_FULL_RE = re.compile(
    rf"^{_KOREAN_TERM_BODY}(?:\s?{_KOREAN_TERM_BODY})*(?:\s?(?:{_CURRENCY_ALT}))?$"
)
_KOREAN_TERM_RE = re.compile(rf"(?P<digits>\d+(?:\.\d+)?)(?P<units>(?:{_KOREAN_UNIT_ALT})+)")
_KOREAN_UNIT_TOKEN_RE = re.compile(_KOREAN_UNIT_ALT)

# Fallback amount parser for spans with no Korean multiplier at all (a plain
# comma-grouped number or bare digit run, optionally with a currency/percent
# suffix) -- fullwidth comma/period accepted (module docstring §13).
_AMOUNT_PARSE_RE = re.compile(rf"^(?P<digits>\d[\d,，\.．]*)\s?(?:{_CURRENCY_ALT})?$")

_POSSESSIVE_RE = re.compile(r"['’]s\b")
_SEPARATOR_RE = re.compile(r"[_\-]+")

# Generic *classifier* words that prefix/suffix an entity name without adding
# identifying information of their own: an answer saying "Project Timber" when
# the context/query only ever writes the bare name "Timber" is a surface
# variation of the same referent (like a possessive or separator variant --
# module docstring §14), not a new fact. Only edge words are stripped, the
# remainder must survive non-empty, and -- to keep this from becoming a bypass
# ("Project Best" grounding via the common word "best" appearing anywhere in
# the context) -- the remainder must contain at least one word that is NOT
# ordinary discourse/meeting vocabulary.
_GENERIC_CLASSIFIER_WORDS = frozenset(
    {
        "project", "team", "plan", "program", "initiative", "phase", "operation",
        "프로젝트", "팀",
    }
)


def _strip_generic_classifiers(generic: str) -> str:
    words = generic.split()
    while words and words[0] in _GENERIC_CLASSIFIER_WORDS:
        words = words[1:]
    while words and words[-1] in _GENERIC_CLASSIFIER_WORDS:
        words = words[:-1]
    return " ".join(words)


def _extract_nonoverlapping(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    """Position-based extraction across several regexes in priority order.

    Once a character span of ``text`` is claimed by an earlier (higher-
    priority) pattern's match, a later pattern's match that overlaps that
    span is dropped rather than re-extracted as a second, phantom candidate
    -- e.g. a comma-grouped amount ("1,000,000") and ``UNIT_AMOUNT_RE``
    independently re-matching that same amount's trailing digit group plus
    the following unit word ("000원") as a spurious second span.
    """
    claimed: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for pattern in patterns:
        for m in pattern.finditer(text):
            start, end = m.span()
            if any(start < claimed_end and claimed_start < end for claimed_start, claimed_end in claimed):
                continue
            claimed.append((start, end))
            found.append((start, m.group(0)))
    found.sort(key=lambda item: item[0])
    return [span for _, span in found]


def _bordered_by_hangul(text: str, start: int, end: int) -> bool:
    before = _fold_invisible(text[:start]).rstrip()
    after = _fold_invisible(text[end:]).lstrip()
    return bool((before and _HANGUL_RE.match(before[-1])) or (after and _HANGUL_RE.match(after[0])))


def _case_insensitive_name_candidates(text: str) -> list[tuple[str, int, int]]:
    """Catch a Latin-script name span regardless of letter case when it is
    inserted directly into Korean prose -- see module docstring §7. Returns
    ``(span, start, end)`` so callers can apply position-aware filters
    (e.g. the trailing sign-off check) without re-searching the text."""
    out: list[tuple[str, int, int]] = []
    for m in _CASE_INSENSITIVE_NAME_RE.finditer(text):
        start, end = m.span()
        if _bordered_by_hangul(text, start, end):
            out.append((m.group(0), start, end))
    return out


def _is_common_phrase(span: str) -> bool:
    """``True`` when every word of ``span`` is ordinary discourse/meeting-
    idiom vocabulary (a greeting/sign-off/section-header word -- module
    docstring §6, §11) or the span is one of a small enumerated set of
    well-known public product names (§12). Such a span is not a distinctive
    value worth grounding; a genuine proper noun never fully collapses into
    either list."""
    words = [w.strip(".,!?;:'\"").lower() for w in _fold_invisible(span).split()]
    words = [w for w in words if w]
    if not words:
        return False
    if all(w in _ORDINARY_VOCABULARY for w in words):
        return True
    if " ".join(words) in _KNOWN_VENDOR_PRODUCT_NAMES:
        return True
    return False


def _is_trailing_signoff_reference(text: str, span: str, start: int, end: int) -> bool:
    """``True`` when ``span`` is a trailing "(the) <generic team/dept word>"
    closing-signature address rather than a factual claim -- module
    docstring §12. Requires ALL of: the span's last word names a generic
    organizational unit (not a specific proper name), nothing but trailing
    punctuation follows it in the text, and an explicit courtesy sign-off
    marker appears earlier in the text. A mid-sentence factual mention of
    the identical words ("escalated this to the Support Team, who...") has
    more text after it and so is never exempted."""
    words = span.split()
    if not words:
        return False
    last_word = words[-1].strip(".,!?;:'\"").lower()
    if last_word not in _GENERIC_TEAM_REFERENCE_WORDS:
        return False
    if not _TRAILING_PUNCT_ONLY_RE.match(text[end:]):
        return False
    return bool(_SIGNOFF_MARKER_RE.search(text[:start]))


def _extract(text: str) -> list[str]:
    numeric = _extract_nonoverlapping(text, _NUMERIC_PATTERNS)
    proper_noun_matches: list[tuple[str, int, int]] = [
        (m.group(0), m.start(), m.end()) for m in PROPER_NOUN_PAIR_RE.finditer(text)
    ] + _case_insensitive_name_candidates(text)
    proper_nouns = [
        span
        for span, start, end in proper_noun_matches
        if not _is_common_phrase(span) and not _is_trailing_signoff_reference(text, span, start, end)
    ]
    return dedup_keep_order(numeric + proper_nouns)


def _canonical_date(span: str) -> str | None:
    """ISO ``YYYY-MM-DD`` key for any supported date span, or ``None`` if
    ``span`` is not date-shaped. A year-less "M월 D일" span gets a
    ``????-MM-DD`` key -- comparable to other year-less mentions of the same
    month/day, but never to a dated (year-bearing) span, since the year is
    genuinely unknown rather than assumed equal."""
    for pattern in (_ISO_DATE_RE, _KOREAN_FULL_DATE_RE):
        m = pattern.match(span)
        if m:
            year, month, day = (int(part) for part in m.groups())
            return f"{year:04d}-{month:02d}-{day:02d}"
    m = _KOREAN_MD_DATE_RE.match(span)
    if m:
        month, day = (int(part) for part in m.groups())
        return f"????-{month:02d}-{day:02d}"
    return None


def _parse_korean_compound_amount(span: str) -> float | None:
    """Sum every chained Korean-multiplier term in ``span`` (e.g.
    "5억 2천만원" -> 500,000,000 + 20,000,000 -- module docstring §10), or
    ``None`` if ``span`` is not (entirely) a compound Korean-multiplier
    amount. A single term ("5억") is simply the one-term case of the same
    rule."""
    if not _KOREAN_COMPOUND_FULL_RE.match(span):
        return None
    total = 0.0
    for m in _KOREAN_TERM_RE.finditer(span):
        digit_value = float(m.group("digits"))
        multiplier = 1
        for token in _KOREAN_UNIT_TOKEN_RE.findall(m.group("units")):
            multiplier *= _MONEY_MULTIPLIER[token]
        total += digit_value * multiplier
    return total


def _canonical_amount(span: str) -> str | None:
    """Plain-integer (or decimal) key for any supported amount span, or
    ``None`` if ``span`` is not amount-shaped: thousands separators are
    stripped and a (possibly chained) Korean numeral multiplier is expanded,
    so "100만원", "1,000,000원", "1000000", and "5억 2천만원" all key to
    their respective plain values, comparable across notations."""
    span = span.strip()
    compound = _parse_korean_compound_amount(span)
    if compound is not None:
        if compound.is_integer():
            compound = int(compound)
        return str(compound)
    m = _AMOUNT_PARSE_RE.match(span)
    if not m:
        return None
    digits_part = m.group("digits").replace(",", "").replace("，", "").replace("．", ".")
    try:
        base: float = int(digits_part) if digits_part.isdigit() else float(digits_part)
    except ValueError:
        return None
    if isinstance(base, float) and base.is_integer():
        base = int(base)
    return str(base)


def _canonical_generic(text: str) -> str:
    """Separator/possessive/case-folded comparison form for free text: a
    ``project_maple`` code-identifier, a ``Maple's`` possessive, and
    ``Project Maple`` all fold to the same "project maple" string. Applied
    both to a single extracted span and to a whole context/query text (as a
    substring-search haystack) -- see ``check`` below. Invisible/zero-width
    characters are folded to a space first (module docstring §9)."""
    folded = _fold_invisible(text)
    folded = _POSSESSIVE_RE.sub("", folded)
    folded = _SEPARATOR_RE.sub(" ", folded)
    return normalize(folded)


def _canonical(span: str) -> str:
    """Category-tagged canonical key: same-category spans compare equal only
    to each other (a money value never accidentally equals a percentage or a
    date just because the digits coincide)."""
    date_key = _canonical_date(span)
    if date_key is not None:
        return f"date::{date_key}"
    amount_key = _canonical_amount(span)
    if amount_key is not None:
        return f"amount::{amount_key}"
    return f"text::{_canonical_generic(span)}"


_TEXT_PREFIX = "text::"

# A word/number boundary for the whole-text substring fallback below --
# round-2 second pass (module docstring §8): only *same-script* adjacency
# indicates a same-token continuation risk. A digit needle flanked by
# another digit, or a Latin-letter needle flanked by another Latin letter,
# is rejected (the classic "9,999,000" inside "29,999,000" / "project maple"
# inside "project maplemark" collision); a Latin/digit needle flanked by a
# directly-attached Hangul character is NOT rejected, since that is simply
# how a Korean postposition/particle attaches to a foreign-script word or
# code-identifier ("project_maple은") -- not a same-token continuation.


def _char_class(ch: str) -> str | None:
    if not ch:
        return None
    if ch.isdigit():
        return "digit"
    if _HANGUL_RE.match(ch):
        return "hangul"
    if ch.isalpha():
        return "latin"
    return None


def _contains_bounded(haystack: str, needle: str) -> bool:
    """Script-aware substring containment: ``needle`` must not be flanked,
    on either side, by a character of the *same* script/digit class as its
    own edge character there -- see module docstring §8."""
    if not needle:
        return False
    first_class = _char_class(needle[0])
    last_class = _char_class(needle[-1])
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return False
        end = idx + len(needle)
        before_class = _char_class(haystack[idx - 1]) if idx > 0 else None
        after_class = _char_class(haystack[end]) if end < len(haystack) else None
        left_ok = before_class is None or before_class != first_class
        right_ok = after_class is None or after_class != last_class
        if left_ok and right_ok:
            return True
        start = idx + 1


def _grounded(span: str, *, grounding_norm: str, query_norm: str, context_canonical: set[str],
              query_canonical: set[str], context_generic: str, query_generic: str) -> bool:
    canon = _canonical(span)
    if canon in context_canonical or (query_canonical and canon in query_canonical):
        return True
    span_norm = normalize(_fold_invisible(span))
    if span_norm and (
        _contains_bounded(grounding_norm, span_norm) or (query_norm and _contains_bounded(query_norm, span_norm))
    ):
        return True
    if canon.startswith(_TEXT_PREFIX):
        generic = canon[len(_TEXT_PREFIX):]
        if generic and (
            _contains_bounded(context_generic, generic) or (query_generic and _contains_bounded(query_generic, generic))
        ):
            return True
        # Generic-classifier-stripped variant ("Project Timber" vs a context/
        # query that only writes the bare "Timber") -- see the comment above
        # ``_GENERIC_CLASSIFIER_WORDS``. The remainder must contain at least
        # one non-ordinary word so a fabricated "<classifier> <common word>"
        # pair can never ground itself off incidental vocabulary.
        stripped = _strip_generic_classifiers(generic)
        if (
            stripped
            and stripped != generic
            and any(w not in _ORDINARY_VOCABULARY for w in stripped.split())
            and (
                _contains_bounded(context_generic, stripped)
                or (query_generic and _contains_bounded(query_generic, stripped))
            )
        ):
            return True
    return False


def check(answer: str, *, grounding_context: str = "", query: str = "") -> RuleFinding:
    grounding_norm = normalize(_fold_invisible(grounding_context))
    if not grounding_norm:
        return RuleFinding(rule="value_grounding")

    candidates = _extract(answer)
    if not candidates:
        return RuleFinding(rule="value_grounding")

    query_norm = normalize(_fold_invisible(query))
    context_canonical = {_canonical(span) for span in _extract(grounding_context)}
    query_canonical = {_canonical(span) for span in _extract(query)} if query_norm else set()
    context_generic = _canonical_generic(grounding_context)
    query_generic = _canonical_generic(query) if query_norm else ""

    ungrounded = [
        span
        for span in candidates
        if not _grounded(
            span,
            grounding_norm=grounding_norm,
            query_norm=query_norm,
            context_canonical=context_canonical,
            query_canonical=query_canonical,
            context_generic=context_generic,
            query_generic=query_generic,
        )
    ]
    return RuleFinding(
        rule="value_grounding",
        candidates=tuple(candidates),
        ungrounded_spans=tuple(ungrounded),
    )
