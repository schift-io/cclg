# CCLG Container Format Plan (`.cclg` v0.1)

## Goal

Make `.cclg` a **single, portable, self-contained memory artifact** — the way
`.gguf` is a single portable model artifact.

```text
.cclg          ~  .gguf        portable, self-contained, backend-agnostic artifact
schift-memory  ~  Ollama       runtime that loads / stores / serves the artifact
APM            ~  the app       consumes it through schift-memory via hooks
```

Open-source boundary follows the analogy:

- `.cclg` format + reference library (CCLG) — **open**, must run locally with no
  Schift account or network (like the GGUF spec + llama.cpp).
- APM — already open.
- schift-memory — the product/hosted runtime wrapper (auth, redaction, bucket,
  hosted search), like Ollama's serving/registry side.

## Problem This Solves

Today CCLG has no container. It exists only as:

- a **directory store** (`~/.cclg/nodes/*.json`, `patches/*.json`, ...), and
- loose JSON records validated by `schema.py`.

The directory layout is **not** the format — it is one backend
(`store.py::CCLGStore`). Because there is no portable artifact, the two runtimes
diverged into two write models:

| | local `~/.cclg` | hosted schift `memory_repo` |
| --- | --- | --- |
| supersede/forget | `MemoryPatch` record | tag / marker memory |
| relation | `MemoryEdge` record | none (flat item + tag) |
| rollback / patch log | yes (append-only ledger) | tag-based approximation |

This is a correctness problem, not a cosmetic one: the same format is faithful
in one runtime and degraded (patch/edge collapsed to tags) in the other. In
GGUF terms, the hosted path is a loader that drops half the tensors. It also
blocks GateMem **Combined** mode and weakens active-forgetting auditability.

## Resolution

A single portable container **removes the fork**. schift-memory becomes a
**lossless loader** of `.cclg` (the Ollama contract): it receives the whole
ledger (nodes + patches + edges + sessions) and never degrades patch/edge to
tags. The searchable flat documents become a *projection* of the effective view
computed on load — not a second source of truth.

## Design Principles

1. **Ledger only; effective view is computed on load.** `.cclg` carries raw
   node + patch + edge + session records (like GGUF raw tensors). `effective_view()`
   / `compile_pack_from_nodes()` run at read time (like the forward pass). Keeps
   the artifact lossless and reproducible, and makes patch/edge non-optional —
   they are the artifact's body.
2. **Auth-free.** No `org_id` / `user_id` / `bucket` / `collection` / api keys in
   the container. Those live in the Schift envelope that *wraps* the container.
   Preserves the invariant: CCLG must not depend on Schift auth.
3. **Layout-independent.** The container is a typed header + sections, not a
   tar/zip of `~/.cclg/` — otherwise the directory layout (which we agreed is not
   the format) leaks into the artifact.
4. **Self-describing + integrity-checked.** Header declares format id, schema
   versions, per-section counts, and a `content_sha256`.

## Container v0.1 Shape

```text
CCLG\t0.1                         line 1: magic + container_version
<header: one-line JSON>           line 2: header
@nodes                            section marker
{node json}                       one record per line (sort_keys canonical)
{node json}
@patches
{patch json}
@edges
{edge json}
@sessions
{session json}
```

Header:

```json
{
  "container": "cclg.container.v0.1",
  "format_id": "cclg.format.v0.1",
  "versions": {
    "memory_node": "cclg.memory_node.v0.1",
    "memory_patch": "cclg.memory_patch.v0.1",
    "edge": "cclg.edge.v0.1",
    "session": "cclg.session.v0.1"
  },
  "sections": [{"name": "nodes", "count": 0}, ...],
  "counts": {"nodes": 0, "patches": 0, "edges": 0, "sessions": 0},
  "generated_at": "…",
  "content_sha256": "…"
}
```

Notes:

- Records are written as `json.dumps(record, sort_keys=True, ensure_ascii=False)`
  for deterministic diffs and a stable checksum.
- Section markers + header counts are redundant on purpose: a mismatch is a hard
  `ContainerError`.
- Byte-offset section index (mmap-style partial load) is deferred to v0.2; v0.1 is
  a streamable line-framed text container.

## Naming Split (source of prior confusion)

- `~/.cclg/` = the local runtime **store** (≈ `~/.ollama/`). Directory, mutable
  working state.
- `.cclg` = the portable **artifact** (≈ `.gguf`). Single file, ledger container.

## Code Surface

```text
src/cclg/container.py    pack_container(...) -> str
                         load_container(text) -> ContainerBundle   (magic/version/
                                                  count/checksum + schema validation)
                         pack_from_store(store) -> str
cli.py                   cclg pack-file <out.cclg> [--session ...]
                         cclg open <in.cclg>      (validate + print header/counts)
docs/CCLG_CONTAINER.md   normative spec (magic, header, sections, invariants)
tests/                   round-trip: store -> .cclg -> load -> effective_view equal
```

`cclg open` is read-only (validate + stats). Import-back into a store is a
separate, later concern (no auto-promote).

## schift-memory Loader Contract

- schift-memory reads a `.cclg`, preserves **all** sections (patches/edges as
  first-class records, never tags).
- The existing `createCclgAiMemoryEvent` envelope wraps the container; only its
  *input* changes to be a container.
- Effective view / search projection is recomputed after load, not stored as the
  source of truth.

## Phasing

- **P1 — Spec + library:** `docs/CCLG_CONTAINER.md`, `container.py`, CLI, tests.
- **P2 — Producer wiring:** `cclg export schift` emits a `.cclg` (closes the
  pipeline that was previously implemented only from the envelope side).
- **P3 — schift-memory loader:** replace the tag-degrade path with a lossless
  `.cclg` loader; searchable docs become projections.
- **P4 — GateMem:** run Office Mode 1 (CCLG local) through container round-trip,
  then Combined once P3 lands.

## Non-Goals

- No tar/zip-of-directory container.
- No auth/bucket fields inside the container.
- No storing of the computed effective view as the source of truth.
- No auto-promote of imported records into active memory.

## Acceptance Criteria

- `store -> pack_container -> load_container` round-trips node/patch/edge/session
  losslessly and `effective_view()` output is identical before and after.
- `load_container` rejects bad magic, unsupported version, count mismatch, and
  checksum mismatch.
- Container carries zero Schift auth fields.
- schift-memory can wrap a real `.cclg` without collapsing patch/edge to tags.
