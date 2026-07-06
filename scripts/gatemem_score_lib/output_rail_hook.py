"""L4 output rail hook for the GateMem Office Mode 1 rescoring harness.

Wraps ``cclg.grounding_rail.apply_output_rail`` -- the same deterministic
grounding+PII rail applied in the schift-monorepo's production ReAct
pipeline (``services/agent-hub/src/agent_hub/output_rail.py``, gated by the
``output_rail`` feature flag there) -- so this harness can measure the
rail's effect on the documented GateMem Office Mode 1 failure mode:
governance/context-leak was already 0% (docs/GATEMEM_OFFICE.md), yet
answer-level privacy/deletion violations were still 3.5%/5.0% because the
answer model itself confirmed or reconstructed a value it was never given
("Yes, the deleted token began with rb_stg").

Default OFF: ``run_generation``'s ``apply_rail`` parameter (wired from the
script's ``--rail`` flag) defaults to ``False``, so existing v1/v2
predictions and any rerun without ``--rail`` stay byte-for-byte
reproducible. Turn it on for a dedicated rescore run (e.g. ``--tag _v3``
against a fresh ``--pred`` file) to measure the rail's effect -- same
rescore pattern already used for v1->v2 (see docs/GATEMEM_OFFICE.md).
"""

from __future__ import annotations

from typing import Any

_cclg_missing_warned = False


def _warn_cclg_missing_once() -> None:
    global _cclg_missing_warned
    if not _cclg_missing_warned:
        _cclg_missing_warned = True
        print("[output_rail_hook] cclg.grounding_rail not importable -- rail is a no-op this run")


def apply_rail_to_output(out: dict[str, Any], *, grounding_context: str, query: str = "") -> dict[str, Any]:
    """Scrub ``out["answer"]`` in place (mutates and returns the same dict)
    using the shared deterministic rail. No-op (with a one-time warning) if
    ``cclg`` is not importable in this venv -- a missing optional dependency
    should never crash a rescoring run.

    ``query`` (round-2 addition) is the checkpoint's user query text --
    passed straight through to ``cclg.grounding_rail.apply_output_rail`` for
    query-echo grounding and the confirmation-attack gate. Defaults to ""
    (same degrade-gracefully contract as the facade itself) for any caller
    that does not have it.

    Known gap: only the top-level ``answer`` string is scrubbed;
    ``answer_structured`` (a free-form dict GateMem's own scorer/judge may
    also read) is not -- deferred, since it typically mirrors the same facts
    already covered by ``answer``.
    """
    answer = out.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return out
    try:
        from cclg.grounding_rail import apply_output_rail
    except ImportError:
        _warn_cclg_missing_once()
        return out

    result = apply_output_rail(answer, grounding_context=grounding_context, query=query)
    out["answer"] = result.text
    out["output_rail_flagged"] = result.flagged
    out["output_rail_refused"] = result.refused
    return out
