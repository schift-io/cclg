#!/usr/bin/env python3
"""GateMem Office Mode 1 -- 3-axis scoring (Utility / Access control / Active forgetting).

Pipeline (see docs referenced inline):
  1. generate  -- schift-local-a3b answers each of the 547 checkpoints using
                  the ALREADY policy-filtered `output.memory_audit.prompt_context.text`
                  from tmp/gatemem-out/predictions.jsonl as the retrieved-memory
                  block, via GateMem's own official prompt
                  (tmp/GateMem/bench/prompts/query_prompt.txt) and its own
                  generation pipeline (bench.agents.base.BaseMemoryAgent._run_llm
                  + bench.agents.utils_llm.generate_llm_output), so the LLM
                  itself chooses action (answer/refuse/answer_redacted/no_memory)
                  and produces the answer text -- exactly as GateMem's reference
                  agents do with a real provider. Written incrementally to
                  tmp/gatemem-out/answers.jsonl, keyed by checkpoint_id (resumable).
  2. score     -- deterministic rule-based scoring via GateMem's own
                  bench.eval.scorer.score_predictions (no network calls).
  3. judge     -- LLM-as-a-judge scoring via GateMem's own bench.eval.judge.run_llm_judge,
                  routed to gemini-3.1-flash-lite (judge only). Resumable via
                  tmp/gatemem-out/judgments.jsonl.
  4. combine   -- writes tmp/gatemem-out/scores_mode1.json with the 3 axes:
                  Utility (judge-primary), Access control / privacy (deterministic-
                  primary, judge secondary), Active forgetting / safety
                  (deterministic-primary, judge secondary).

Usage:
    python3 scripts/gatemem_office_score_mode1.py --stage all
    python3 scripts/gatemem_office_score_mode1.py --stage context-only   # no LLM calls at all
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEMEM_ROOT = REPO_ROOT / "tmp" / "GateMem"
DATA_DIR = REPO_ROOT / "tmp" / "gatemem-data"
OUT_DIR = REPO_ROOT / "tmp" / "gatemem-out"

for p in (str(GATEMEM_ROOT), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from bench.agents.base import BaseMemoryAgent, Checkpoint  # noqa: E402
from bench.eval.judge import run_llm_judge, load_jsonl as judge_load_jsonl  # noqa: E402
from bench.eval.scorer import score_predictions  # noqa: E402

from gatemem_score_lib.llm_clients import (  # noqa: E402
    A3BRouterAdapter,
    GeminiJudgeRouterAdapter,
    LLMCallError,
    gemini_judge_client,
    schift_a3b_client,
)
from gatemem_score_lib.output_rail_hook import apply_rail_to_output  # noqa: E402

JUDGE_TOKEN_BUDGET = 3_000_000
ANSWER_MAX_TOKENS = 512
JUDGE_MAX_TOKENS = 256
GENERATION_CONCURRENCY = 4
JUDGE_CONCURRENCY = 4


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def dump_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


# --------------------------------------------------------------------------
# Stage 1: generation (schift-local-a3b)
# --------------------------------------------------------------------------


def _checkpoint_from_row(pred: dict[str, Any], ckpt: dict[str, Any]) -> Checkpoint:
    asker = pred.get("asker") or {}
    return Checkpoint(
        checkpoint_id=str(pred["checkpoint_id"]),
        episode_id=str(pred.get("episode_id") or ckpt.get("episode_id")),
        as_of_turn_id=str(pred.get("as_of_turn_id") or ckpt.get("as_of_turn_id") or ""),
        asker_principal_id=str(asker.get("principal_id") or ""),
        asker_role=str(asker.get("role") or ""),
        query_type=str(ckpt.get("query_type") or pred.get("query_type") or ""),
        attack_type=ckpt.get("attack_type"),
        query_text=str(ckpt.get("query_text") or pred.get("query_text") or ""),
        expected_action=str(ckpt.get("expected_action") or pred.get("expected_action") or ""),
        judge_spec=ckpt.get("judge_spec") if isinstance(ckpt.get("judge_spec"), dict) else {},
        leak_targets=ckpt.get("leak_targets") if isinstance(ckpt.get("leak_targets"), list) else [],
    )


def _prompt_context_text(pred: dict[str, Any]) -> str:
    audit = ((pred.get("output") or {}).get("memory_audit") or {})
    ctx = audit.get("prompt_context") or {}
    return str(ctx.get("text") or "")


def run_generation(
    predictions: list[dict[str, Any]],
    checkpoints_by_id: dict[str, dict[str, Any]],
    episodes_by_id: dict[str, dict[str, Any]],
    answers_path: Path,
    *,
    limit: int | None = None,
    apply_rail: bool = False,
) -> dict[str, Any]:
    """Generate a3b answers for every checkpoint not already `status: ok` in
    answers.jsonl. Retries once on failure, then records an error row and
    continues (never aborts the whole run for one bad row).

    ``apply_rail`` (default False, wired from ``--rail``): when True, each
    answer is passed through the shared L4 output rail
    (``gatemem_score_lib.output_rail_hook``) before being written -- see that
    module's docstring. Default OFF keeps existing/rerun predictions
    byte-for-byte reproducible; only a dedicated rescore run should set this.
    """

    client = schift_a3b_client(max_tokens=ANSWER_MAX_TOKENS, temperature=0.0)
    router = A3BRouterAdapter(client)

    existing: dict[str, dict[str, Any]] = {}
    if answers_path.exists():
        for row in load_jsonl(answers_path):
            cid = str(row.get("checkpoint_id") or "")
            if cid:
                existing[cid] = row

    agents_by_episode: dict[str, BaseMemoryAgent] = {}

    def _agent_for(episode_id: str) -> BaseMemoryAgent | None:
        if episode_id in agents_by_episode:
            return agents_by_episode[episode_id]
        episode = episodes_by_id.get(episode_id)
        if not episode:
            return None
        agent = BaseMemoryAgent(
            llm_router=router,
            llm_mode="leaky",
            query_prompt_path=str(GATEMEM_ROOT / "bench" / "prompts" / "query_prompt.txt"),
            answer_protocol="standard",
        )
        agent.reset(episode)
        agents_by_episode[episode_id] = agent
        return agent

    todo = [
        p
        for p in predictions
        if existing.get(str(p["checkpoint_id"]), {}).get("status") != "ok"
    ]
    if limit is not None:
        todo = todo[:limit]

    lock = threading.Lock()
    done = 0
    errors = 0
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _one(pred: dict[str, Any]) -> dict[str, Any]:
        cid = str(pred["checkpoint_id"])
        ckpt = checkpoints_by_id.get(cid, {})
        episode_id = str(pred.get("episode_id") or ckpt.get("episode_id") or "")
        agent = _agent_for(episode_id)
        if agent is None:
            return {"checkpoint_id": cid, "status": "error", "error": f"missing episode {episode_id}"}

        checkpoint = _checkpoint_from_row(pred, ckpt)
        context_text = _prompt_context_text(pred)

        last_exc: Exception | None = None
        for attempt in range(2):  # 1 try + 1 retry
            try:
                out = agent._run_llm(
                    checkpoint=checkpoint,
                    retrieved_memory=[],
                    rendered_memory_block_override=context_text,
                )
                if apply_rail:
                    out = apply_rail_to_output(out, grounding_context=context_text)
                return {"checkpoint_id": cid, "status": "ok", "output": out}
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 0:
                    time.sleep(1.0)
                    continue
        return {"checkpoint_id": cid, "status": "error", "error": str(last_exc)}

    with ThreadPoolExecutor(max_workers=GENERATION_CONCURRENCY) as pool:
        futures = [pool.submit(_one, p) for p in todo]
        for fut in as_completed(futures):
            row = fut.result()
            append_jsonl(answers_path, row, lock)
            done += 1
            if row["status"] == "ok":
                usage = (row.get("output") or {}).get("llm_usage") or {}
                for k in total_usage:
                    total_usage[k] += int(usage.get(k) or 0)
            else:
                errors += 1
            if done % 20 == 0:
                print(f"[generate] {done}/{len(todo)} done ({errors} errors)", file=sys.stderr)

    return {
        "attempted": len(todo),
        "already_done": len(predictions) - len(todo),
        "errors": errors,
        "usage": total_usage,
    }


def merge_predictions_with_answers(
    predictions: list[dict[str, Any]], answers_path: Path
) -> list[dict[str, Any]]:
    answers: dict[str, dict[str, Any]] = {}
    if answers_path.exists():
        for row in load_jsonl(answers_path):
            cid = str(row.get("checkpoint_id") or "")
            if cid and row.get("status") == "ok":
                answers[cid] = row["output"]

    merged: list[dict[str, Any]] = []
    real_only: list[dict[str, Any]] = []
    for pred in predictions:
        cid = str(pred["checkpoint_id"])
        row = dict(pred)
        base_output = dict(pred.get("output") or {})
        gen = answers.get(cid)
        if gen is not None:
            for key in (
                "action",
                "answer",
                "answer_structured",
                "used_record_ids",
                "llm_usage",
                "llm_latency_s",
                "llm_provider",
                "llm_model",
                "answer_protocol",
            ):
                if key in gen:
                    base_output[key] = gen[key]
        row["output"] = base_output
        merged.append(row)
        if gen is not None:
            real_only.append(row)
    return merged, real_only


# --------------------------------------------------------------------------
# Stage 4: combine into the 3 axes
# --------------------------------------------------------------------------


def build_axis_summary(rule_summary: dict[str, Any], judge_summary: dict[str, Any] | None) -> dict[str, Any]:
    judge_summary = judge_summary or {}

    utility = {
        "denominator": rule_summary.get("n_utility", 0),
        "n_scored_by_judge": judge_summary.get("n_utility_scored", 0),
        "primary_metric": "judge_effective_utility_accuracy (utility_ok AND action_ok, gemini-3.1-flash-lite)",
        "judge_effective_utility_accuracy": judge_summary.get("judge_effective_utility_accuracy"),
        "judge_utility_accuracy": judge_summary.get("judge_utility_accuracy"),
        "rule_based_utility_accuracy_crosscheck": rule_summary.get("utility_accuracy"),
        "over_refusal_rate": rule_summary.get("over_refusal_rate"),
    }

    access_control = {
        "denominator": rule_summary.get("n_privacy", 0),
        "primary_metric": "deterministic string/value match (rule-based), judge is secondary corroboration",
        "privacy_answer_leakage_rate": rule_summary.get("privacy_answer_leakage_rate"),
        "privacy_context_leakage_rate": rule_summary.get("privacy_context_leakage_rate"),
        "privacy_e2e_leakage_rate": rule_summary.get("privacy_e2e_leakage_rate"),
        "judge_privacy_leakage_rate_secondary": judge_summary.get("judge_privacy_leakage_rate"),
        "n_scored_by_judge": judge_summary.get("n_privacy_scored", 0),
    }

    active_forgetting = {
        "denominator": rule_summary.get("n_safety", 0),
        "primary_metric": "deterministic string/value match (rule-based) against leak_targets, judge is secondary corroboration",
        "deletion_answer_leakage_rate": rule_summary.get("deletion_answer_leakage_rate"),
        "deletion_context_leakage_rate": rule_summary.get("deletion_context_leakage_rate"),
        "deletion_e2e_leakage_rate": rule_summary.get("deletion_e2e_leakage_rate"),
        "judge_deletion_leakage_rate_secondary": judge_summary.get("judge_deletion_leakage_rate"),
        "n_scored_by_judge": judge_summary.get("n_safety_scored", 0),
    }

    return {
        "utility": utility,
        "access_control_privacy": access_control,
        "active_forgetting_safety": active_forgetting,
        "action_accuracy_all_query_types": rule_summary.get("action_accuracy"),
        "compliance_utility_score": rule_summary.get("compliance_utility_score"),
        "compliance_utility_e2e_score": rule_summary.get("compliance_utility_e2e_score"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--stage",
        choices=["context-only", "generate", "score", "judge", "all"],
        default="all",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap rows processed in generate stage (debug).")
    parser.add_argument(
        "--pred",
        type=str,
        default="predictions.jsonl",
        help="Predictions filename within tmp/gatemem-out/ (default: predictions.jsonl).",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default="",
        help="Suffix appended to output filenames (answers{tag}.jsonl, judgments{tag}.jsonl, "
        "scores_ruled{tag}.jsonl, scores_mode1{tag}.json), so reruns against a different "
        "--pred don't clobber a prior run's outputs. E.g. --tag _v2.",
    )
    parser.add_argument(
        "--rail",
        action="store_true",
        default=False,
        help="Apply the shared L4 output rail (gatemem_score_lib.output_rail_hook, same "
        "deterministic grounding+PII rail as the hosted runtime's output-rail flag) to each generated "
        "answer before scoring. Default off -- existing/rerun predictions stay reproducible; "
        "use e.g. --rail --tag _v3 for a dedicated rescore run measuring the rail's effect.",
    )
    args = parser.parse_args()

    predictions = load_jsonl(OUT_DIR / args.pred)
    checkpoints = load_jsonl(DATA_DIR / "office_checkpoints.jsonl")
    episodes = load_jsonl(DATA_DIR / "office_episodes.jsonl")
    checkpoints_by_id = {str(c["checkpoint_id"]): c for c in checkpoints}
    episodes_by_id = {str(e["episode_id"]): e for e in episodes}

    answers_path = OUT_DIR / f"answers{args.tag}.jsonl"
    judgments_path = OUT_DIR / f"judgments{args.tag}.jsonl"
    scores_path = OUT_DIR / f"scores_mode1{args.tag}.json"

    result: dict[str, Any] = {
        "n_checkpoints": len(predictions),
        "blockers": [],
        "output_rail_applied": args.rail,
    }

    # Context-only pass: zero LLM calls, always safe/free to run. Uses the
    # ORIGINAL (pre-generation) predictions.jsonl, so it only tells us about
    # prompt-context exposure (Access control / Active forgetting), not
    # answer-level (e2e) leakage or Utility (those need a real answer).
    _, context_summary = score_predictions(
        episodes=episodes, checkpoints=checkpoints, predictions=predictions, gate_by_action=False
    )
    result["context_only_summary"] = {
        "note": "No LLM calls. Scores context-exposure only (prompt_context.text), using the "
        "as-shipped predictions.jsonl (answer text empty). Utility and answer-level (e2e) "
        "leak rates are NOT meaningful here.",
        "privacy_context_leakage_rate": context_summary.get("privacy_context_leakage_rate"),
        "deletion_context_leakage_rate": context_summary.get("deletion_context_leakage_rate"),
        "n_privacy": context_summary.get("n_privacy"),
        "n_safety": context_summary.get("n_safety"),
    }

    if args.stage == "context-only":
        dump_json(scores_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    gen_stats: dict[str, Any] | None = None
    if args.stage in ("generate", "all"):
        try:
            gen_stats = run_generation(
                predictions,
                checkpoints_by_id,
                episodes_by_id,
                answers_path,
                limit=args.limit,
                apply_rail=args.rail,
            )
            result["generation"] = gen_stats
        except LLMCallError as exc:
            result["blockers"].append(f"generation_blocked: {exc}")
            result["generation"] = {"attempted": 0, "already_done": 0, "errors": 0, "usage": {}}

    merged, real_only = merge_predictions_with_answers(predictions, answers_path)
    n_real = len(real_only)
    result["n_real_answers_available"] = n_real

    if n_real == 0:
        result["blockers"].append(
            "no real a3b answers available yet (answers.jsonl empty/missing); "
            "Utility axis and answer-level (e2e) leak rates cannot be computed. "
            "Access control / Active forgetting context-exposure rates above are still valid."
        )
        dump_json(scores_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.stage != "all" else 1

    if args.stage in ("score", "judge", "all"):
        # Score only the subset with a real a3b answer -- see
        # merge_predictions_with_answers. Once generation is complete for all
        # 547 rows, real_only == merged and denominators match n_checkpoints.
        scores_rows, rule_summary = score_predictions(
            episodes=episodes, checkpoints=checkpoints, predictions=real_only, gate_by_action=False
        )
        dump_jsonl_atomic(OUT_DIR / f"scores_ruled{args.tag}.jsonl", scores_rows)
        result["rule_based_summary"] = rule_summary

    judge_summary: dict[str, Any] | None = None
    if args.stage in ("judge", "all"):
        prior_tokens = 0
        if judgments_path.exists():
            prior_rows = judge_load_jsonl(str(judgments_path), ignore_errors=True)
            prior_tokens = sum(
                int(((r.get("llm") or {}).get("usage") or {}).get("total_tokens") or 0) for r in prior_rows
            )
        if prior_tokens >= JUDGE_TOKEN_BUDGET:
            result["blockers"].append(
                f"judge_token_budget_exceeded: prior judge usage {prior_tokens} >= {JUDGE_TOKEN_BUDGET}"
            )
        else:
            try:
                judge_client = gemini_judge_client(max_tokens=JUDGE_MAX_TOKENS, temperature=0.0)
                judge_router = GeminiJudgeRouterAdapter(judge_client)
                _, judge_summary = run_llm_judge(
                    episodes=episodes,
                    checkpoints=checkpoints,
                    predictions=real_only,
                    judge_router=judge_router,
                    prompt_path=str(GATEMEM_ROOT / "bench" / "prompts" / "judge_prompt.txt"),
                    out_path=str(judgments_path),
                    resume=True,
                    gate_by_action=False,
                    concurrency=JUDGE_CONCURRENCY,
                )
                result["judge_summary"] = judge_summary
            except LLMCallError as exc:
                result["blockers"].append(f"judge_blocked: {exc}")

    if "rule_based_summary" in result:
        result["axis_scores"] = build_axis_summary(result["rule_based_summary"], judge_summary)

    dump_json(scores_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def dump_json(path: Path, obj: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
