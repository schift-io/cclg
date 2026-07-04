"""Generic lexical matching helpers backing the GateMem Office adapter (P4
Mode 1). Split out from ``cclg.gatemem_adapter`` to keep that module under
the repo's 500-line god-file guideline; unlike that module, this one has no
GateMem-domain state (no ``ProjectDirectory``/``EpisodePolicyState``) and
only depends on ``cclg.retrieval``/``cclg.models``, so it is reusable from
both the ingestion side (``gatemem_adapter``) and the checkpoint side
(``gatemem_pack``).

Every function here exists because this benchmark's turns are dense,
formulaic corporate prose where two turns about genuinely *unrelated* facts
routinely share half a dozen incidental terms (the same project name,
"incident", "bridge", ...). Plain BM25 term-frequency scoring alone is not
precise enough to reliably link a value's introduction to its later
deletion/correction in that setting -- see each function's docstring for the
specific failure mode it addresses.
"""

from __future__ import annotations

import copy
import re

from .models import MemoryNode
from .retrieval import SearchHit, search_nodes


def matches_any(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def normalize_content(text: str) -> str:
    return " ".join(text.strip().lower().split())


# ``cclg.retrieval``'s tokenizer includes '.', ':', '/' in its token character
# class (so path-like strings such as "src/foo.py" tokenize as one token).
# That means a code/value span immediately followed by sentence punctuation
# ("...is mp_stg_4R2N-K8QM-7T1C. Treat...") glues onto it and tokenizes to a
# *different* string than the same span with no trailing punctuation
# ("...token mp_stg_4R2N-K8QM-7T1C should..."). Left alone, this silently
# breaks the one high-confidence lexical signal (an exact rare token/value
# match) that should unambiguously link a value's introduction to its later
# deletion or correction. cclg.retrieval is not ours to edit, so this
# pre-normalizes text before handing it to `search_nodes` instead.
_GLUED_PUNCT_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_/:-]{3,})([.,;:])(?=\s|$)")


def _detach_glued_punctuation(text: str) -> str:
    return _GLUED_PUNCT_RE.sub(r"\1 \2", text)


# Two turns about unrelated facts routinely share half a dozen stopwords
# ("the", "a", "that", "keep", ...), which is enough to outweigh one exact
# rare-value match under plain term-frequency scoring. Stripping stopwords
# from the *query* side only (never node content, so each candidate's own
# idf/content scoring is untouched) lets content-bearing terms -- and
# especially an exact code/value match -- decide target resolution instead
# of incidental filler-word overlap.
_STOPWORDS = frozenset(
    "a an the and or but if is are was were be been being to of for in on "
    "at by with as that this these those it its we you they i he she may "
    "might will would should could do does did not no so than then until "
    "after before still just only also both note".split()
)


def _strip_stopwords(text: str) -> str:
    return " ".join(word for word in text.split() if word.strip(".,;:!?'\"()[]{}-").lower() not in _STOPWORDS)


def search_nodes_normalized(query: str, nodes: list[MemoryNode], *, limit: int = 10) -> list[SearchHit]:
    """``cclg.retrieval.search_nodes``, but both sides are pre-normalized:
    punctuation glued onto code/value tokens is detached (both query and
    candidate content), and query-side stopwords are stripped (candidate
    content is left untouched, so each candidate's own scoring is
    unaffected). Returned hits reference the original ``nodes`` instances
    (never the shadow copies used only for tokenization), so callers see
    unmodified content/ids."""
    if not query or not nodes:
        return []
    originals_by_id = {node.id: node for node in nodes}
    shadows: list[MemoryNode] = []
    for node in nodes:
        shadow = copy.copy(node)
        shadow.content = _detach_glued_punctuation(node.content)
        shadows.append(shadow)
    normalized_query = _strip_stopwords(_detach_glued_punctuation(query))
    hits = search_nodes(normalized_query, shadows, limit=limit)
    return [SearchHit(node=originals_by_id[hit.node.id], score=hit.score, reasons=hit.reasons) for hit in hits]


def confident_top_hit(hits: list[SearchHit], *, min_score: float = 8.0, min_margin: float = 1.15) -> SearchHit | None:
    """Accept a BM25 top hit as an unambiguous forget/supersede target only if
    it clears an absolute floor *and* leads the runner-up by a margin. When
    candidates are near-tied the safer choice is "no confident target" (the
    turn is still recorded as raw evidence, just without mutating an
    unrelated node) rather than guessing and forgetting/superseding the
    wrong fact."""
    if not hits:
        return None
    top = hits[0]
    if top.score < min_score:
        return None
    if len(hits) > 1 and top.score < hits[1].score * min_margin:
        return None
    return top


# Deletion turns in this corpus reliably restate the *exact* value being
# deleted (a credential/label code, or a proper-noun customer name), even
# when they omit the project name a BM25 query needs for a confident lexical
# match against the turn that first introduced it. An exact substring match
# on one of these value spans is a far stronger, more specific signal than
# cumulative term-frequency overlap, and BM25's own scoring can bury it under
# incidental shared filler words. This is a targeted literal-match
# short-circuit, not a replacement for BM25.
_VALUE_SPAN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+\b")
_PROPER_NOUN_PAIR_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")


def _first_content_match(spans: list[str], pool: list[MemoryNode]) -> MemoryNode | None:
    lowered_spans = [span.lower() for span in spans]
    for node in pool:
        lower_content = node.content.lower()
        if any(span in lower_content for span in lowered_spans):
            return node
    return None


def extract_proper_noun_pairs(text: str) -> list[str]:
    return _PROPER_NOUN_PAIR_RE.findall(text)


def find_exact_value_target(text: str, pool: list[MemoryNode], *, exclude_terms: frozenset[str] = frozenset()) -> MemoryNode | None:
    """First (i.e. earliest-established, since ``pool`` is id-ordered) active
    node whose content contains one of ``text``'s extracted value spans as a
    case-insensitive substring, or ``None`` if it names no such span or none
    matches.

    Code-like spans (credential/label values) are tried first and, if any
    matches, returned immediately -- they are the most specific signal this
    corpus offers. Proper-noun-pair spans (customer names) are only
    considered when no code span matched anything. ``exclude_terms``
    (lower-cased) should include the episode's own project name(s): "Project
    Harbor"/"Harbor" are two-word-capitalized like any other proper noun, but
    they are the *project's own name* -- present in nearly every node about
    it, not a distinguishing value. Without excluding them, a code-bearing
    deletion turn that also happens to say "Project Harbor" would report
    that phrase as a second "value span", and whichever node happens to sort
    first among the many that also say "Project Harbor" would win over the
    one true match.
    """
    codes = [span for span in _VALUE_SPAN_RE.findall(text) if any(ch.isdigit() for ch in span)]
    match = _first_content_match(codes, pool)
    if match is not None:
        return match
    names = [span for span in extract_proper_noun_pairs(text) if span.lower() not in exclude_terms]
    return _first_content_match(names, pool)
