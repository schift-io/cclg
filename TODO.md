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

- [ ] `cclg export schift --session/--pack-query/--node` emits a `.cclg` payload
      (no Schift auth fields).
- [ ] Local end-to-end smoke: `.cclg` -> `createCclgAiMemoryEvent` envelope.

### P3 — schift-memory as lossless loader (schift-ai-memory repo)

- [ ] Replace tag-degrade path with a `.cclg` loader that preserves patch/edge as
      first-class records.
- [ ] Searchable flat docs become a projection of the on-load effective view, not
      a second source of truth.
- [ ] Decide durable storage of patch/edge on the hosted side (dedicated
      collection vs container embed) — resolves the earlier A/B/C fork.

### P4 — GateMem Office

- [ ] Mode 1 (CCLG local) adapter -> real `predictions.jsonl` via container round-trip.
- [ ] Mode 2 (Schift hosted) after P3.
- [ ] Mode 3 (Combined) once P2 + P3 land. Report the three scores separately.

## Known Open Threads (pre-existing)

- [ ] `cclg export schift` did not exist in CCLG CLI (blocks CCLG-local / Combined).
- [ ] Envelope-side adapter work continues in the schift-ai-memory wrapper.
- [ ] Hosted upload/search smoke still pending on the wrapper side.
