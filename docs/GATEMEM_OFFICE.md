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
