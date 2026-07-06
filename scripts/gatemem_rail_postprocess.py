#!/usr/bin/env python3
"""Post-process an existing GateMem Office Mode 1 answers.jsonl through the
shared L4 output rail (``cclg.grounding_rail`` via
``gatemem_score_lib.output_rail_hook``), without re-running the a3b answerer.

This is the "rescore, don't regenerate" path noted in
``scripts/gatemem_office_score_mode1.py``'s ``--rail`` flag docstring: the L4
rail is a pure post-hoc transform of the answer text against the *same*
grounding context that was already used to produce it (predictions file's
``output.memory_audit.prompt_context.text``), so re-running the LLM answerer
adds cost/noise without changing what the rail measures. Regenerating from
scratch with ``--rail`` would also non-deterministically resample the
answerer (temperature 0 helps but isn't a byte-identical guarantee across
runs), which would confound "did the rail help" with "did the answerer
happen to answer differently this time".

Usage:
    python3 scripts/gatemem_rail_postprocess.py \
        --pred predictions_v2.jsonl --answers answers_v2.jsonl --tag _v3

Writes tmp/gatemem-out/answers{tag}.jsonl (e.g. answers_v3.jsonl), suitable
as input to:
    python3 scripts/gatemem_office_score_mode1.py --stage judge \
        --pred predictions_v2.jsonl --tag _v3
(score_mode1's ``answers_path`` is derived from ``--tag`` the same way, and
``--stage judge`` also runs the rule-based ``score`` step first -- see that
script's ``main()``.)
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "tmp" / "gatemem-out"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gatemem_office_score_mode1 import (  # noqa: E402
    _prompt_context_text,
    dump_jsonl_atomic,
    load_jsonl,
)
from gatemem_score_lib.output_rail_hook import apply_rail_to_output  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pred", type=str, default="predictions_v2.jsonl")
    parser.add_argument("--answers", type=str, default="answers_v2.jsonl")
    parser.add_argument("--tag", type=str, required=True, help="e.g. _v3 -> writes answers_v3.jsonl")
    args = parser.parse_args()

    predictions = load_jsonl(OUT_DIR / args.pred)
    context_by_id = {str(p["checkpoint_id"]): _prompt_context_text(p) for p in predictions}
    # Round-2: query-echo grounding + the confirmation-attack gate need the
    # user's own query text, not just the prompt context -- join the same way
    # context_by_id already does, by checkpoint_id.
    query_by_id = {str(p["checkpoint_id"]): str(p.get("query_text") or "") for p in predictions}

    answers = load_jsonl(OUT_DIR / args.answers)

    n_ok = 0
    n_flagged = 0
    n_refused = 0
    n_missing_context_row = 0
    out_rows = []
    for row in answers:
        row = copy.deepcopy(row)
        if row.get("status") != "ok":
            out_rows.append(row)
            continue
        cid = str(row.get("checkpoint_id") or "")
        if cid not in context_by_id:
            n_missing_context_row += 1
        context_text = context_by_id.get(cid, "")
        query_text = query_by_id.get(cid, "")
        output = row.get("output") or {}
        before_refused = bool(output.get("output_rail_refused"))
        output = apply_rail_to_output(output, grounding_context=context_text, query=query_text)
        row["output"] = output
        out_rows.append(row)
        n_ok += 1
        if output.get("output_rail_flagged"):
            n_flagged += 1
        if output.get("output_rail_refused") and not before_refused:
            n_refused += 1

    out_path = OUT_DIR / f"answers{args.tag}.jsonl"
    dump_jsonl_atomic(out_path, out_rows)

    print(
        f"wrote {out_path} ({len(out_rows)} rows): "
        f"n_ok={n_ok} n_flagged={n_flagged} n_refused={n_refused} "
        f"n_missing_context_row={n_missing_context_row}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
