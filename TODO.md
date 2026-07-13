# CCLG TODO

## `.cclg` Container Format (v0.1)

See `docs/CCLG_CONTAINER_PLAN.md` for rationale and spec.

### P1 — Spec + library (CCLG repo)

- [x] Write `docs/CCLG_CONTAINER.md` (normative: magic, header, sections, invariants).
- [x] Add `src/cclg/container.py`:
  - [x] `pack_container(nodes, patches, edges, sessions, ...) -> str`
  - [x] `load_container(text, validate=True) -> ContainerBundle`
  - [x] `pack_from_store(store) -> str`
  - [x] `ContainerError`, magic/version/count/`content_sha256` checks
  - [x] auth-free guard (reject org_id/user_id/bucket/collection/api key/token)
  - [x] reuse `schema.py` validators (`validate_node/patch/edge/session`) with cross-refs
- [x] Wire CLI in `cli.py`:
  - [x] `cclg pack-file <out.cclg> [--session ...]`
  - [x] `cclg open <in.cclg>` (read-only: validate + print header/counts)
- [x] Add `format.py` constants: `CCLG_CONTAINER_ID = "cclg.container.v0.1"`, magic, version.
- [x] Tests:
  - [x] round-trip `store -> .cclg -> load` lossless
  - [x] `effective_view()` identical before/after round-trip
  - [x] reject bad magic / version / count mismatch / checksum mismatch
  - [x] reject auth fields in container metadata

### P2 — Producer wiring (close the pipeline)

- [x] `cclg export schift --session/--node` emits a `.cclg` payload (no Schift
      auth fields). Backed by `container.pack_for_export` (filters
      nodes/patches/edges/sessions to the requested session/node ids, reusing
      `pack_container`'s existing auth-free guard + schema validation — no
      separate packing path). `--pack-query` deferred: not yet needed by any
      producer; add when a caller needs an arbitrary-query slice instead of
      explicit session/node id selection.
- [ ] Local end-to-end smoke: `.cclg` -> `createCclgAiMemoryEvent` envelope.

### P3 — schift-memory as lossless loader (schift-ai-memory repo)

- [ ] Replace tag-degrade path with a `.cclg` loader that preserves patch/edge as
      first-class records.
- [ ] Searchable flat docs become a projection of the on-load effective view, not
      a second source of truth.
- [ ] Decide durable storage of patch/edge on the hosted side (dedicated
      collection vs container embed) — resolves the earlier A/B/C fork.

### P4 — GateMem Office

- [x] Mode 1 (CCLG local) adapter -> real `predictions.jsonl` via container round-trip. 547/547 checkpoints, deterministic (byte-identical reruns), 0 leak violations (internal + post-hoc leak-target). LLM judge scoring for utility deferred (no paid calls in Mode 1 harness).
- [ ] Mode 2 (Schift hosted) after P3.
- [ ] Mode 3 (Combined) once P2 + P3 land. Report the three scores separately.

## Correction Detection (precision track, 2026-07-05)

- [x] R1-R3 deterministic precision: weak/strong trigger split, conditional and
      reported-speech suppression, enumeration guard, money canonicalization,
      lexeme-boundary matching. Eval fixture 22 pos / 45 neg at P=1.0 R=1.0;
      recall 12/12 on novel corrections.
- [ ] Ceiling reached for keyword-only rules (adversarial R3: 아니라서/아니라도
      connectives, -대/-래/카더라 hearsay, ~할지 고민 중 deliberation,
      multi-sentence contamination, 2-item hedges). Next: demote the detector
      to a candidate generator and add an LLM-confirm gate before writing a
      supersession marker — a small local/self-hosted model suffices, fired
      only on candidates.

## GateMem follow-ups (2026-07-05)

- [ ] `MIN_RELEVANT_SCORE=4.0` absolute BM25 floor is corpus-dependent (BM25
      scale shifts with doc stats); after the query-scope fix it is only a
      secondary guard, but replace it with a relative criterion (e.g. fraction
      of the pool's top score) before running Mode 1 on a second domain.

## Known Open Threads (pre-existing)

- [x] `cclg export schift` did not exist in CCLG CLI (blocks CCLG-local / Combined). Resolved — see P2 above.
- [ ] Envelope-side adapter work continues in the schift-ai-memory wrapper.
- [ ] Hosted upload/search smoke still pending on the wrapper side.
