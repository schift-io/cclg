# GateMem Office Evaluation Plan

GateMem should be evaluated in three separated modes before any combined score is
treated as meaningful.

## Scope

Use only the GateMem `office` domain first:

- `office_episodes`: 17 long-form office episodes.
- `office_checkpoints`: 547 checkpoints.
- checkpoint mix: utility, access control, and active forgetting.

Dataset source:

```text
https://huggingface.co/datasets/Ray368/GateMem
revision: b4304866ec8d9784fb77bebb1ce4660806abcded
paths:
  gatemem/data/office/episodes.jsonl
  gatemem/data/office/checkpoints.jsonl
```

## What GateMem Evaluates

GateMem is not a recall-only benchmark. It evaluates memory governance:

```text
Utility
  Authorized requesters should receive useful current answers.

Access control
  Unauthorized or over-scoped requesters should not receive protected details.

Active forgetting
  Deleted facts should not be recovered, confirmed, or reconstructed.
```

The released code keeps legacy names:

```text
utility -> Utility
privacy -> Access control
safety  -> Active forgetting
```

## Modes

### 1. CCLG local

Purpose: prove the CCLG kernel can represent governed shared memory.

Boundary:

- no Schift auth
- no hosted bucket
- no network after dataset download
- temporary CCLG store only

Expected adapter shape:

```text
GateMem turn
  -> raw evidence
  -> session event
  -> optional MemoryNode / MemoryPatch / MemoryEdge

GateMem checkpoint
  -> requester principal / role / relationship scope
  -> policy-aware ActiveMemoryPack
  -> prediction row
```

This mode tests CCLG schema, provenance, patch semantics, effective view, and
pack filtering. It should fail loudly if a protected, stale, or forgotten node is
available to the prompt context.

### 2. Schift hosted memory

Purpose: prove the product runtime can enforce tenant/user/bucket boundaries and
return CCLG-shaped metadata.

Boundary:

- uses Schift auth / org / user / bucket semantics
- uses Schift AI Memory upload queue and redaction policy
- uses hosted search/fetch or MCP retrieval
- does not mutate `~/.cclg`

Expected adapter shape:

```text
CCLG export or GateMem turn summary
  -> Schift AI Memory envelope
  -> Schift bucket document
  -> hosted search/fetch with org/user scope
  -> prediction row
```

This mode tests auth, upload policy, bucket isolation, redaction, hosted search,
and MCP result shape. It should fail loudly if a different principal can retrieve
another user's private office fact.

### 3. Combined

Purpose: prove the intended product architecture works end to end.

Boundary:

- CCLG remains the memory format and effective-view compiler.
- Schift Memory remains the product transport/runtime.
- scoring separates CCLG-context failures from Schift-runtime failures.

Expected adapter shape:

```text
GateMem office episode
  -> CCLG local governed state
  -> CCLG export
  -> Schift AI Memory envelope
  -> hosted retrieval
  -> CCLG-shaped result metadata
  -> answer generation
  -> GateMem predictions.jsonl
```

Combined mode is the product score. It should be reported only after local and
hosted modes have separate scores, otherwise root cause is ambiguous.

## Hidden Field Rule

The adapter must never show these checkpoint fields to the agent:

```text
query_type
expected_action
judge_spec
leak_targets
attack_type
```

Those fields are evaluator-only labels. Agent-facing checkpoint input should be
limited to:

```text
checkpoint_id
episode_id
as_of_turn_id
asker
query_text
```

## Office Probe

Run the local office probe:

```bash
python3 scripts/gatemem_office_probe.py \
  --out-dir tmp/gatemem-office \
  --write-empty-predictions
```

It downloads the office JSONL files, validates the row shape, writes a manifest,
writes agent-visible checkpoint inputs, and optionally writes an empty
`predictions.empty.jsonl` file for scorer plumbing tests.

The empty predictions file is intentionally not a benchmark result. It is only a
join/scoring smoke input.

## External Scorer Plumbing

After cloning the official GateMem repo:

```bash
git clone https://github.com/rzhub/GateMem.git tmp/GateMem
python3 tmp/GateMem/bench/scripts/score_predictions.py \
  --data_dir tmp/GateMem/bench/data/office \
  --predictions tmp/gatemem-office/predictions.empty.jsonl \
  --out_dir tmp/gatemem-office/eval-empty
```

For real scoring, replace `predictions.empty.jsonl` with the output from one of
the three modes above.

## Reporting

Report the three scores separately:

```text
CCLG local Office score
Schift hosted Office score
Combined Office score
```

Then report the GateMem metrics:

```text
utility_accuracy
privacy_leakage_rate
deletion_leakage_rate
compliance_utility_score / MGS
```

Do not collapse the three modes into one headline until failure root cause is
understood.

## Mode 1 Results (2026-07-05, cclg local + schift-local-a3b answerer)

547/547 checkpoints, deterministic adapter (byte-identical reruns), zero
generation errors. Answerer: schift-local-a3b. Judge: gemini-3.1-flash-lite
(GateMem's official judge prompt; 0% parse failures; every judge-positive leak
was manually audited against the raw answers and confirmed real).

```text
context-level (what CCLG lets into the prompt):
  privacy_context_leakage_rate    0.0   (0/171)
  deletion_context_leakage_rate   0.0   (0/222)

answer-level (full stack, judge-verified primary):
  privacy_leakage_rate            5.8%  (10/171, all confirmed real)
  deletion_leakage_rate           3.6%  (8/222, all confirmed real)
  utility (judge_effective)       17.5% (over_refusal_rate 71.4%)
```

Readings:

- CCLG governance is clean: nothing protected or forgotten ever reached the
  prompt context, including through the `.cclg` container round trip.
- Remaining answer-level leaks are answerer behavior, not retrieval: the model
  confirms attacker-supplied values ("Yes, the deleted token began with
  rb_stg") or leaks by relational inference. An output-side guardrail layer is
  the fix, not a memory-layer change.
- Utility is retrieval-recall-bound: 489/547 checkpoints packed no relevant
  memory (sparse-only matching), so the answerer honestly refuses. Dense
  retrieval / better sparse keys is the lever; independent of governance.
- Caveat on the official rule-based scorer: generic "yes"/"no" leak_targets
  string-match harmless refusals ("No information available...") — inflates
  rule-based leak rates 20-25%. Judge-verified numbers above are authoritative;
  official-scorer numbers are kept in tmp/gatemem-out/scores_mode1_v1.json (this
  run is now the `v1` baseline; see v2 below) for comparability.

## Mode 1 Results v2 (2026-07-05, gatemem_pack.py forgotten-veto override fix)

Rescored `predictions_v2.jsonl` (output of `src/cclg/gatemem_pack.py`'s new
`_forgotten_veto_overridden` override — see the module comment above
`_RETROSPECTIVE_MARKER_RE`) through the identical schift-local-a3b answerer +
gemini-3.1-flash-lite judge pipeline:

```bash
python3 scripts/gatemem_office_score_mode1.py --stage all \
  --pred predictions_v2.jsonl --tag _v2
```

547/547 checkpoints, 0 generation errors, 0% judge parse failures. v1 artifacts
were preserved (not overwritten) as `tmp/gatemem-out/{answers,judgments,
scores_ruled,scores_mode1}_v1.*` before this run; v2 outputs are the `_v2`
siblings. Judge token spend: 502,762 for this run (499,487 for v1 — cumulative
~1.0M of the 3M judge budget).

**One-line summary: utility rose sharply as intended by the fix (17.5% -> 47.4%
judge-effective), but active-forgetting (deletion) answer-level leakage got
WORSE (3.6% -> 5.0% judge, 7.7% -> 9.9% rule-based) — flagged severity HIGH
below.**

### v1 vs v2

| Axis | Metric | v1 | v2 | delta |
|---|---|---|---|---|
| Utility (n=154) | judge_effective_utility_accuracy | 17.5% (27/154) | 47.4% (73/154) | **+29.9pp** |
| Utility | judge_utility_accuracy | 19.5% (30/154) | 48.7% (75/154) | +29.2pp |
| Utility | rule-based crosscheck | 17.5% (27/154) | 40.9% (63/154) | +23.4pp |
| Utility | over_refusal_rate | 71.4% (110/154) | 30.5% (47/154) | -40.9pp |
| Utility | action_accuracy (all query types) | 40.2% (220/547) | 52.3% (286/547) | +12.1pp |
| Utility | compliance_utility_score | 15.4% | 36.0% | +20.6pp |
| Access control / Privacy (n=171) | context_leakage_rate | 0.0% (0/171) | 0.0% (0/171) | none |
| Privacy | answer/e2e_leakage_rate (rule, primary) | 4.7% (8/171) | 2.3% (4/171) | -2.3pp (improved) |
| Privacy | judge_leakage_rate (secondary) | 5.8% (10/171) | 3.5% (6/171) | -2.3pp (improved) |
| Active forgetting / Safety (n=222) | context_leakage_rate | 0.0% (0/222) | 0.0% (0/222) | none |
| Forgetting | deletion answer/e2e_leakage_rate (rule, primary) | 7.7% (17/222) | 9.9% (22/222) | **+2.3pp (WORSE)** |
| Forgetting | judge_deletion_leakage_rate (secondary) | 3.6% (8/222) | 5.0% (11/222) | **+1.4pp (WORSE)** |

### Governance regression — severity HIGH

Active-forgetting (deletion) answer-level leakage increased on **both** scorers
(rule-based +5 checkpoints, judge +3 checkpoints), even though the memory-layer
invariant is untouched: `deletion_context_leakage_rate` stays 0/222 in both
runs — no forgotten record's content ever reaches the prompt context either
before or after the pack fix, and the structural governance gates the fix was
validated against (`internal_leak_violations`, `post_hoc_leak_target_hits`)
remain 0 -> 0. The leak is entirely in the LLM answerer's behavior, not in CCLG
retrieval/pack construction — the same failure mode already documented above
for v1 (model confirms attacker-supplied values / leaks by relational
inference). It got measurably worse in v2 because roughly 60-70 more
checkpoints now receive a real (non-`no_memory`) answer instead of refusing
outright (over_refusal_rate -40.9pp, consistent with the fix's own claimed
67/109 utility-miss resolution) — more answered checkpoints across the corpus
means more surface area for this pre-existing answerer-side failure mode, even
though the safety-type action-accuracy distribution itself did not move
(216/222 in both, per the fix author's own before/after).

Net read: the privacy axis improved as a side effect (fewer blanket `no_memory`
refusals also means fewer generic "I don't know" responses that the rule
scorer's broad yes/no matching had been miscounting), so the utility gain is
not free but also not purely a governance trade against privacy. It *is* a
real trade against active forgetting. Recommendation: do not treat this pack
fix as net-positive for production governance until an output-side guardrail
for deletion/staleness confirmations is added — track as a follow-up separate
from this rescoring exercise.

Full raw output: `tmp/gatemem-out/scores_mode1_v2.json` (also
`answers_v2.jsonl`, `judgments_v2.jsonl`, `scores_ruled_v2.jsonl`). v1 baseline
preserved at `tmp/gatemem-out/scores_mode1_v1.json` (`answers_v1.jsonl`,
`judgments_v1.jsonl`, `scores_ruled_v1.jsonl`).
