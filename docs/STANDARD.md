# The `.cclg` Memory Interchange Standard

Status: **Normative, v1** (frozen 2026-07-10). Companion to
[`CCLG_CONTAINER.md`](CCLG_CONTAINER.md), which remains the byte-level source
of truth for the container format; this document is the publication-facing
standard that packages that spec, its fail-closed load semantics, its
conformance suite, and its MCP consumption contract into one artifact — the
same shape Anthropic used to publish Agent Skills as a standard rather than a
single implementation's internal doc.

Key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** in
this document are to be interpreted as described in
[RFC 2119](https://www.ietf.org/rfc/rfc2119.txt).

## Why this exists

Agent memory has no standard interchange format. Every framework — mem0, Zep
(Graphiti), Letta (MemGPT), MemPalace, and others — picks its own storage
model, and the honest industry self-assessment is "80k+ combined GitHub stars,
four different architectures, no standard schema." Proposals like PAM and
memorywire name the gap but ship no reference implementation and no
conformance suite. `.cclg` fills this gap the way `.gguf` filled it for model
weights: a single, portable, checksummed, self-describing file that any
runtime can load without adopting the producer's storage model.

The standard has four parts, mirroring how Agent Skills became a real
standard rather than a single vendor's internal convention:

1. **A format spec** (§1 below, full detail in `CCLG_CONTAINER.md`).
2. **A test-vector suite**: golden `.cclg` fixtures with expected effective
   views, committed and versioned (§3).
3. **More than one implementation** proving the spec is not a
   single-implementation's post-hoc description of its own code (§5).
4. **A consumption contract**: the `memory.*` MCP tool signatures every
   conformant runtime — local or hosted — exposes identically (§4).

## 1. Container format

A `.cclg` file is UTF-8 text, three parts in fixed order: a one-line magic +
version string, a single-line JSON header, then line-framed sections.

```text
CCLG\t0.1
{"container":"cclg.container.v0.1","format_id":"cclg.format.v0.1","versions":{...},"sections":[...],"counts":{...},"generated_at":"...","content_sha256":"..."}
@nodes
{"schema_version":"cclg.memory_node.v0.1","id":"mem_...", ...}
@patches
{"schema_version":"cclg.memory_patch.v0.1","id":"patch_...", ...}
@edges
{"schema_version":"cclg.edge.v0.1","id":"edge_...", ...}
@sessions
{"schema_version":"cclg.session.v0.1","id":"session_...", ...}
```

Four invariants, all mandatory for a conforming implementation:

- **Ledger-only**: the container carries raw `MemoryNode` / `MemoryPatch` /
  `MemoryEdge` / session records, never a precomputed effective view. Computing
  "what's active" is a read-time operation, analogous to a `.gguf` forward
  pass over raw tensors, not something a producer bakes into the file.
- **Auth-free**: the container MUST NOT contain Schift (or any host
  platform's) auth fields — `org_id`, `user_id`, `bucket`, `collection`,
  `api_key`/`apikey`, `token`, `access_token` — at any nesting depth, in the
  header or in any record. A conforming implementation MUST reject (raise, not
  silently strip) a pack or load attempt where any forbidden key appears. This
  is what lets a `.cclg` file move between organizations, hosts, and vendors
  without carrying a credential leak with it — the exact failure mode n8n/Dify
  workflow exports hit when a credential ID or vendor-specific field survives
  an "export."
- **Layout-independent**: the container is a typed header + line-framed
  sections, never a tar/zip of a backend's working directory. A loader MUST be
  able to reconstruct a full in-memory bundle with zero assumptions about how
  the producer stored records on disk.
- **Self-describing + integrity-checked**: the header declares container id,
  format id, per-record-kind schema versions, per-section counts (redundantly,
  see below), a generation timestamp, and a whole-body sha256. A reader can
  validate structural integrity without an external schema registry.

Full byte-level detail — header key table, section markers, checksum domain
(exact `\n`-join semantics, not `str.splitlines()`), redundant-count
cross-checks, version negotiation — is normative in `CCLG_CONTAINER.md` §2–§6
and is not duplicated here to avoid two documents disagreeing over time; where
this document and that one conflict, `CCLG_CONTAINER.md` wins.

## 2. Fail-closed load semantics

This is the standard's central claim against the rest of the field: **the
industry's default conflict-resolution heuristic — "most recent write wins" —
is a proven-defective policy for agent memory**, because vector-similarity
retrieval can surface a stale fact ahead of its correction when the stale
fact's phrasing happens to match the query more closely. `.cclg` replaces
"most recent wins" with a **deterministic patch ledger**.

A conforming loader MUST enforce all of the following, not as warnings but as
hard failures:

1. **A node's own `status` field is not sufficient proof of activity.** A
   `@nodes` record can read `status: "active"` while a `@patches` record
   targets it with a retiring operation, if the producer never baked that
   patch's effect back into the node (nothing in the base schema forbids
   this). A conforming reader MUST compute the effective view as the union
   of (a) the status-based keep-filter and (b) exclusion of every node id
   appearing in `target_ids` of a patch whose operation retires its target.
   Skipping (b) and trusting `status` alone degrades back to exactly the bug
   this rule exists to close.

2. **An unrecognized patch operation MUST raise, never fall through as
   non-retiring.** A patch's `operation` is drawn from a closed,
   exhaustively-classified set — retiring (`update`, `supersede`, `refine`,
   `expand`, `narrow`, `merge`, `split`, `resolve_conflict`, `expire`,
   `forget`, `deprecate`) or non-retiring (`create`, `rollback`). An operation
   outside this set MUST cause the effective-view computation to raise (the
   reference implementation's `UnknownPatchOperationError`), not silently
   pass through as if it were non-retiring. Silently ignoring an operation the
   reader doesn't recognize is exactly the class of bug this whole gate
   exists to prevent: a future format version, or a hand-edited/corrupted
   record, could add an operation that *should* retire its target, and a
   reader that guesses "unknown means safe" can resurrect a memory that was
   supposed to be superseded, expired, forgotten, or deprecated.

3. **A `format_id` mismatch is a hard failure, not an informational warning.**
   The header's `format_id` is a cross-check against the schema classification
   (including the retiring/non-retiring operation set above) this reader
   implements. A reader that cannot name the format it is reading MUST NOT
   assume its own operation classification still holds for a container
   produced against a different format version.

4. **Unknown *sections* are the one form of "more than expected" that
   passes through**, deliberately, as forward-compatible additive data — a
   `@something` marker this reader doesn't know is collected separately with
   a warning, and loading continues. This is not the same category as (2) or
   (3): an unknown section adds inert data; an unknown patch operation or a
   `format_id` mismatch changes how *already-understood* data must be
   interpreted.

### Deterministic conflict resolution

The patch ledger's conflict-resolution model, in full:

- **Create-then-retire**: applying a correction persists the *replacement*
  node (and the patch record carrying the new content) before retiring the
  target(s). A crash or a concurrent reader between the two writes sees a
  harmless transient duplicate — old and new both present, collapsed by scope
  precedence below — never a target retired with no replacement on disk,
  which would silently and permanently drop the fact.
- **`prior_states`**: every patch records the pre-patch `status` of each
  target, so a rollback restores the exact prior state rather than a blanket
  `"active"`.
- **Scope precedence**: when multiple nodes share the same `key`, the
  effective view keeps exactly one — the highest-precedence node by
  `session > project > workspace > global`, tie-broken by most recent
  `updated_at`. Nodes without a `key` are independent facts and are never
  collapsed.
- Nowhere in this model does "most recently written" alone decide which fact
  wins. Recency only breaks ties *within* the same scope-precedence tier,
  after the deterministic keep/retire decision above has already been made.

## 3. Conformance

Golden `.cclg` fixtures plus their expected effective-view JSON live in
`tests/conformance/` (generated deterministically by
`scripts/gen_conformance.py` — fixed ids and timestamps, so regeneration is
byte-identical). Eight fixtures, covering:

1. a supersede chain (A → B → C)
2. create-then-retire
3. scope-precedence collapse (session > project > workspace > global on the
   same key)
4. forget / expire / deprecate
5. `conflict_pending` → resolve
6. rollback
7. **negative**: a container carrying an unknown patch operation — loading and
   computing the effective view MUST raise
8. **negative**: a container carrying a forbidden auth field — MUST raise

**Conformance condition**: any implementation of this standard, in any
language, MUST reproduce the same expected effective view (or the same
raised-error behavior, for the two negative fixtures) over these exact files.
`tests/test_conformance.py` is the reference harness — it loads each fixture,
computes `ContainerBundle.effective_view()`, and diffs against the fixture's
expected view (sorted keys, id lists — deliberately language-neutral). A
second implementation passing all eight fixtures is what "`.cclg` v1
compatible" means; nothing less, and passing them is sufficient regardless of
internal implementation choices.

## 4. MCP binding — the consumption contract

The portable format alone does not make memory interchangeable across
agent hosts; the *tool surface* an agent calls also has to be identical
regardless of whether it is backed by a local `~/.cclg` store or a hosted
tenant memory service. `.cclg`'s MCP binding fixes that surface as
`memory.*` (aliased 1:1 from the reference implementation's native `cclg.*`
names, so either family of tool names is a conformant caller):

| Tool | Required args | Description |
|---|---|---|
| `memory.search` (`cclg.search`) | `query` | Search active memory. `mode`: `auto`\|`grep`\|`bm25`\|`dense`\|`graph`\|`temporal` (default `bm25`). |
| `memory.pack` (`cclg.pack`) | — | Compile an ActiveMemoryPack for a task (`query`, `max_nodes`, `max_chars`, `session_id`). |
| `memory.add` (`cclg.add`) | `content`, `source` | Add a source-grounded memory node. |
| `memory.patch` (`cclg.patch`) | `operation`, `target_ids`, `reason` | Apply a memory patch — the only way a node is retired or corrected. |
| `memory.recall` (`cclg.recall`) | `query` | Active memory plus provenance citations and recovered raw spans. |
| `memory.cite` (`cclg.cite`) | `memory_id` | Recover the source turn/span and quote for one active memory id. |
| `memory.conflicts` (`cclg.conflicts`) | — | List unresolved `conflict_pending` memory awaiting review. |
| `memory.resolve` (`cclg.resolve`) | `target_ids`, `new_content` | Resolve a conflict by superseding the conflicting node(s). |

(`memory.grep`, `memory.bm25`, `memory.audit` also exist as aliases of their
`cclg.*` counterparts; see `src/cclg/mcp_server.py::tool_definitions()` for
the full, canonical input-schema definitions.)

A pack, an agent, or a runtime that only ever calls these tool names by
signature — never a store-specific API — does not need to know whether it is
talking to a local CCLG MCP server or a hosted facade in front of tenant
memory. This is deliberately narrower than "MCP defines memory primitives" —
MCP itself only standardizes *transport*, not what a memory tool should be
named or take as arguments; this table is what fills that gap today, ahead of
any settled cross-vendor convention.

## 5. Reference implementations

A standard proven by one implementation is a single codebase's internal
documentation wearing a standard's clothes. `.cclg` v1 has two:

- **Python — `cclg` (this repository, canonical)**: the reference
  implementation. `container.py` (pack/load), `patches.py` (effective view,
  `RETIRING_PATCH_OPERATIONS`/`KNOWN_PATCH_OPERATIONS`), `schema.py`
  (per-record validators), `mcp_server.py` (`memory.*`/`cclg.*` tool surface).
  Published as `cclg` on PyPI.
- **TypeScript — `schift-ai-memory` loader** (`derivatives/schift-ai-memory`,
  `packages/core/src/cclg-effective-view.ts`): an independent port of the
  effective-view algorithm, consuming `.cclg` containers losslessly (verbatim
  container bytes preserved; patches and edges stay first-class records, not
  flattened away). Its `EXCLUDING_PATCH_OPERATIONS` set MUST stay identical
  to the Python reference's `RETIRING_PATCH_OPERATIONS` — the conformance
  fixtures in §3 are exactly the mechanism that catches a drift here instead
  of letting it surface as a silent cross-implementation behavior mismatch.
  A real drift was caught this way on first use: the TS port's
  `effectiveView` was silently ignoring unknown patch operations
  (fail-open) before the shared fixtures exposed it, and it was fixed to
  raise, matching §2 rule 2 above.

Editing `.cclg` v1's fixed invariants requires a new container version
(`CCLG_CONTAINER.md` §1's "0.x minor bumps are breaking" rule, inherited
unchanged) — this document describes v1 as it is frozen, not a living target.
