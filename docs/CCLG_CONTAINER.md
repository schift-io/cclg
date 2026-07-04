# `.cclg` Container Format (normative, v0.1)

Rationale and design history: `docs/CCLG_CONTAINER_PLAN.md`. This document is the
normative spec — the plan is background, this file is the contract implementers
follow. Where the two disagree, this file wins.

```text
.cclg          ~  .gguf        portable, self-contained, backend-agnostic artifact
schift-memory  ~  Ollama       runtime that loads / stores / serves the artifact
```

`~/.cclg/` (the directory store, see `docs/DATA_MODEL.md`) and `.cclg` (this
container) are different things. The directory is one backend's mutable working
state. The container is the portable, immutable-once-written artifact produced
from it.

## 1. Status

`cclg.container.v0.1` — draft, matching the `status = "draft"` of
`format/cclg.format.v0.1.toml`, whose `[compatibility]` rule this container
format inherits verbatim: **0.x minor version bumps are breaking.**

## 2. Shape

A `.cclg` file is UTF-8 text, line-framed, three parts in fixed order:

```text
CCLG\t0.1                         line 1: magic byte string + container_version
<header>                          line 2: single-line canonical JSON object
<sections>                        line 3..N: section markers + one record per line
```

Concretely:

```text
CCLG	0.1
{"container":"cclg.container.v0.1","format_id":"cclg.format.v0.1","versions":{...},"sections":[...],"counts":{...},"generated_at":"...","content_sha256":"..."}
@nodes
{"schema_version":"cclg.memory_node.v0.1","id":"mem_...", ...}
{"schema_version":"cclg.memory_node.v0.1","id":"mem_...", ...}
@patches
{"schema_version":"cclg.memory_patch.v0.1","id":"patch_...", ...}
@edges
{"schema_version":"cclg.edge.v0.1","id":"edge_...", ...}
@sessions
{"schema_version":"cclg.session.v0.1","id":"session_...", ...}
```

The file MUST end with a single trailing `\n`. Lines are joined with a plain
`\n` (not `str.splitlines()` semantics — see §6 on why this distinction matters
for the checksum).

### 2.1 Magic line

`line 1` is exactly `{CCLG_CONTAINER_MAGIC}\t{CCLG_CONTAINER_VERSION}`, i.e.
`CCLG\t0.1` today. A reader MUST reject any file whose line 1 does not split
into exactly `(magic, version)` on the first `\t`, where `magic != "CCLG"`, or
where `version` is not a container version the reader implements. Per §1, a
reader that only implements `0.1` MUST reject every version string except the
literal `"0.1"` — including `"0.2"` — as unsupported, not as forward-compatible.

### 2.2 Header line

`line 2` is a single JSON object, one line, no embedded newlines. Required keys:

| key | type | meaning |
| --- | --- | --- |
| `container` | string | `cclg.container.v0.1` (`CCLG_CONTAINER_ID`). Must match exactly. |
| `format_id` | string | `cclg.format.v0.1` (`CCLG_FORMAT_ID`) — the record-schema format this container's records were validated against. |
| `versions` | object | per-record-kind schema versions in effect: `{"memory_node": "...", "memory_patch": "...", "edge": "...", "session": "..."}`. |
| `sections` | array | `[{"name": "nodes", "count": N}, ...]` for every section written, in on-disk order. Redundant with `counts` on purpose (§5). |
| `counts` | object | `{"nodes": N, "patches": N, "edges": N, "sessions": N}` — canonical per-kind counts. |
| `generated_at` | string | ISO-8601 UTC timestamp of pack time. Informational only; not part of the checksum domain beyond being inside the header (the header itself is not checksummed — see §6). |
| `content_sha256` | string | lowercase hex sha256 of the exact section body text (§6). |

A reader MUST reject a header missing any required key, or whose `container`
field is not exactly `CCLG_CONTAINER_ID`. A reader MAY accept additional,
unrecognized header keys (forward-compatible: unknown top-level header keys are
ignored, only unknown *record* fields and unknown *sections* get the explicit
skip-with-warning treatment in §4/§7).

### 2.3 Sections

Four known sections, always emitted in this order, one marker line each,
followed by zero or more record lines:

| marker | record kind | schema version constant | id prefix |
| --- | --- | --- | --- |
| `@nodes` | `MemoryNode` | `MEMORY_NODE_SCHEMA` (`cclg.memory_node.v0.1`) | `mem_` |
| `@patches` | `MemoryPatch` | `MEMORY_PATCH_SCHEMA` (`cclg.memory_patch.v0.1`) | `patch_` |
| `@edges` | `MemoryEdge` | `MEMORY_EDGE_SCHEMA` (`cclg.edge.v0.1`) | `edge_` |
| `@sessions` | session state dict | `SESSION_SCHEMA` (`cclg.session.v0.1`) | `session_` |

A section marker MUST be written even when the section is empty (a marker line
immediately followed by the next marker line, or EOF). This keeps `sections`
and `counts` in the header structurally complete and makes the file diffable —
adding the first record of a previously-empty section is a pure addition, not a
section-marker insertion.

Each record line is exactly one JSON object, written as
`json.dumps(record, ensure_ascii=False)` over the dict `to_dict()` already
produced — i.e. in that record kind's own field order
(`format/cclg.format.v0.1.toml`'s per-record `canonical_order` table), **not**
re-sorted alphabetically. Records reuse the existing `to_dict()` / `from_dict()`
contract of `models.py` verbatim; the container format does not define a new
serialization for node/patch/edge/session bodies — not the fields, and not
their order — only the envelope around them.

## 3. Invariants

These are the non-negotiable properties of a valid `.cclg` file. A conforming
implementation MUST enforce all four.

### 3.1 Ledger-only

The container carries **only** raw `MemoryNode` / `MemoryPatch` / `MemoryEdge` /
session records — the full history, including `superseded` / `expired` /
`forgotten` / `conflict_pending` nodes. It never carries a precomputed
`effective_view()` or `ActiveMemoryPack` as a section or as the source of
truth. Computing the effective view (`patches.effective_view()`) and compiling
a prompt pack (`pack.compile_pack_from_nodes()`) are **read-time** operations a
loader performs *after* opening the container — analogous to a GGUF forward
pass over raw tensors. A `.cclg` file with an `@active_memory_pack` or
`@effective_view` section is not violating this format (unknown sections are
legal, §7), but no conforming producer emits one, and no conforming consumer
should treat one as authoritative if it appears.

#### 3.1.1 Load semantics: status filtering alone is not sufficient

A container's `@nodes` section can be schema-valid while still being
*inconsistent* with its own `@patches` section: a node's `status` field can
still read `active` even though a `@patches` record targets that node with an
operation that retires it (e.g. a producer appends a
`MemoryPatch(operation="supersede", target_ids=[...])` record without also
flipping the target node's `status` to `superseded`). Nothing in §2/§8 forbids
this — record-level schema validation checks each record in isolation, not
cross-record consistency between `@nodes` and `@patches`.

**A conforming reader MUST exclude nodes targeted by a retiring patch from the
effective view, regardless of the node's baked `status`.** Baked status is an
optimization a producer MAY apply (a live `CCLGStore` always does — see below)
— it is not required for correctness, and a reader MUST NOT treat "the node's
own `status` says `active`" as sufficient proof that the node is actually
active. Concretely: the effective view over a loaded container is the
**union** of (a) the `status`-based filter (`patches.effective_view`'s
existing `active`/`active_session` keep-rule) and (b) exclusion of every node
id appearing in `target_ids` of a patch whose `operation` is one of:

```text
update, supersede, refine, expand, narrow, merge, split,
resolve_conflict, expire, forget, deprecate
```

(this is `patches.RETIRING_PATCH_OPERATIONS` in the Python implementation, and
the TS mirror's `EXCLUDING_PATCH_OPERATIONS` in
`cclg-effective-view.ts` — the two lists MUST stay identical). `create` and
`rollback` are deliberately excluded from this list: neither retires a target.

This is why a bare `patches.effective_view(nodes)` call (status filter only)
is correct for a **live** `CCLGStore` — `apply_patch` always bakes a patch's
effect into `node.status` *before* the node is ever read back out, so status
alone is authoritative there — but is not sufficient as the canonical
container-load path, where that baking is not guaranteed to have happened
before the container was produced. `container.ContainerBundle.effective_view()`
is the canonical reader-side entry point that applies both (a) and (b); a
reader implementing this format directly (not through that helper) MUST
still perform the union above.

### 3.2 Auth-free

The container MUST NOT contain Schift platform-auth fields, in the header or
in any record, at any nesting depth: `org_id`, `user_id`, `bucket`,
`collection`, `api_key` / `apikey`, `token`, `access_token`. These live in the
Schift envelope that wraps the container (e.g. `createCclgAiMemoryEvent`), not
in the container itself. This is distinct from CCLG's own local scope model —
`MemoryNode.scope` legitimately carries `user` / `org` / `workspace` /
`project` / `agent` / `session` labels (e.g. `scope.user = "user_local"`,
`scope.org = null`) as local addressing, not platform credentials. The guard
targets the literal Schift auth field names above, not every key that happens
to be named `user` or `org`.

A conforming implementation MUST reject (raise, not silently strip) pack or
load attempts where any of the forbidden keys appear as a dict key anywhere in
the header or in any record.

### 3.3 Layout-independent

The container is a typed header + line-framed sections — never a tar/zip of
`~/.cclg/`. The directory layout (`nodes/*.json`, `patches/*.json`, ...) is one
backend's storage detail (`store.py::CCLGStore`) and MUST NOT leak into the
artifact. A loader must be able to reconstruct a full in-memory bundle without
any assumption about how the producer stored records on disk.

### 3.4 Self-describing + integrity-checked

The header declares the container id, the format id, the per-record-kind
schema versions in effect, per-section counts (twice — see §5), a generation
timestamp, and a whole-body checksum. A reader can validate a `.cclg` file's
structural integrity without consulting any external schema registry.

## 4. Version negotiation

- Container version (`line 1`, second field): exact-match only. `0.1` accepts
  `0.1` and rejects everything else, per §1's inherited breaking-bump rule.
- `header.format_id`: informational cross-check against `CCLG_FORMAT_ID`. A
  reader SHOULD warn (not hard-fail) on a mismatch here if the container
  version itself matched, since the record-level schema versions in
  `header.versions` and on each record are the authoritative check (record
  validation already asserts `schema_version` equality per `schema.py`).
  `load_container` implements this by appending a message to
  `ContainerBundle.warnings` (the same mechanism §7 uses for unknown sections)
  and loading normally — a `format_id` mismatch never raises `ContainerError`.
- `header.versions.<kind>`: each record's own `schema_version` field is
  re-validated per-record against `schema.py`'s validators regardless of what
  the header claims — the header value is a manifest for quick inspection
  (`cclg open`), not a substitute for per-record validation.

## 5. Redundant counts

`header.sections[i].count` and `header.counts[header.sections[i].name]` MUST
agree, and both MUST equal the number of record lines actually present under
that section's marker. A mismatch between any of these three sources (header
`sections[]`, header `counts{}`, actual body record count) is a hard
`ContainerError` — this redundancy exists specifically to catch truncated or
hand-edited files where only one of the two header fields was updated.

A section's marker is always written, even when empty (§2.3), so a section
that is actually present in the body (its `@name` marker appears at all, with
zero or more records under it) MUST have a corresponding entry in *both*
`header.counts` and `header.sections`. A reader MUST NOT treat an entirely
missing entry as "nothing to compare, skip the check": that is indistinguishable
from the truncated/hand-edited case this redundancy exists to catch, and MUST
raise `ContainerError` exactly like a numeric disagreement would. (A section
that is absent from the body *and* from both header sources — nobody ever
wrote it — is not this case and is not an error.)

## 6. Checksum

`content_sha256` = lowercase hex `sha256` over the exact UTF-8 bytes of every
line **after** the header line (`line 2`), i.e. every section-marker line and
every record line, joined with `\n` — the same body text that is written to
disk, before the header is prepended and before the trailing file `\n` is
appended.

Implementers MUST split/join the container using literal `\n` (`text.split("\n")`
/ `"\n".join(...)`), not `str.splitlines()`, when locating the header/body
boundary and when recomputing the checksum. `str.splitlines()` also splits on
Unicode line-separator code points (`\x0b`, `\x0c`, `\x1c`–`\x1e`, `\x85`,
` `, ` `); since `json.dumps(..., ensure_ascii=False)` does not escape
those code points inside string values, a `splitlines()`-based reader could
mis-locate record boundaries for content containing them, while `split("\n")`
cannot, because only `\n` itself is ever treated as JSON's own escaped line
break (`\n` is always escaped to the two characters `\` `n` by `json.dumps`,
never emitted as a literal byte inside a JSON string).

A checksum mismatch on load is a hard `ContainerError`: the file has been
truncated, hand-edited, or corrupted in transit and must not be trusted.

## 7. Forward compatibility

- **Unknown sections**: a marker line (`@something`) that is not one of
  `nodes` / `patches` / `edges` / `sessions` is not an error. A conforming
  reader collects its record lines separately (not merged into a known
  section), emits a warning identifying the section name and record count, and
  continues loading the rest of the file normally. This lets a future v0.1.x
  producer add e.g. an experimental section without breaking older readers
  (skip, don't fail) — consistent with `format/cclg.format.v0.1.toml`'s
  general validation philosophy of explicit, enumerable structure.
- **Unknown header keys**: ignored (§2.2).
- **Breaking changes**: any change to the magic byte sequence, the line-framing
  shape (§2), the required header keys (§2.2), or the checksum domain (§6)
  requires a new container version — per §1, even a `0.1` → `0.2` bump is
  breaking and MUST ship with a migration note and a validator update, exactly
  as `format/cclg.format.v0.1.toml`'s `[compatibility]` table already requires
  for record schemas.

## 8. Validation

Beyond the structural checks above (magic, version, header shape, counts,
checksum, auth-free guard), every record is re-validated against the existing
`schema.py` validators with cross-reference checks:

- `validate_node(node, known_ids=...)` — every node in `@nodes`, cross-checked
  against the full node id set in the same container (relation targets must
  resolve within the container).
- `validate_patch(patch, known_ids=...)` — every patch in `@patches`.
- `validate_edge(edge, known_ids=..., known_patch_ids=...)` — every edge in
  `@edges`.
- `validate_session(session)` — every session in `@sessions`.

A container that fails any of these is rejected with a `ContainerError`
aggregating every problem found (not just the first), matching `cclg doctor`'s
existing behavior of reporting all problems in one pass.

## 9. Non-goals (v0.1)

- No tar/zip-of-directory container (§3.3).
- No byte-offset section index for partial/mmap-style loads — deferred to a
  future minor version; v0.1 is a streamable line-framed text container meant
  to be read start-to-finish.
- No auto-promotion of a loaded container's records into an active local store
  — `cclg open` is read-only (validate + report). Importing a container's
  records back into a `CCLGStore` is a separate, later concern.
- No compression. `.cclg` is plain UTF-8 text; callers that want smaller
  artifacts on disk or over the wire compress the whole file externally
  (e.g. gzip), the same way a `.jsonl.gz` is still "a JSONL file."

## 10. Constants (canonical source: `src/cclg/format.py`)

```python
CCLG_CONTAINER_MAGIC = "CCLG"
CCLG_CONTAINER_VERSION = "0.1"
CCLG_CONTAINER_ID = "cclg.container.v0.1"
```

## 11. Code surface

```text
src/cclg/container.py
  pack_container(nodes, patches=(), edges=(), sessions=(), *, validate=True) -> str
  load_container(text, *, validate=True) -> ContainerBundle
  pack_from_store(store, *, session_ids=None, validate=True) -> str
  ContainerBundle.effective_view(*, session_id=None) -> list[MemoryNode]
  ContainerError(ValueError)

src/cclg/patches.py
  effective_view(nodes, *, session_id=None, patches=None) -> list[MemoryNode]
  RETIRING_PATCH_OPERATIONS: set[str]
```

`ContainerBundle.effective_view()` is the canonical way to get "what's active"
for a *loaded* container (§3.1.1): it passes the bundle's own `@patches`
records through to `patches.effective_view()`'s `patches=` argument so a
retiring patch excludes its target even when the node's `status` was never
baked to the retired value. `patches.effective_view(nodes)` — called with no
`patches=` — keeps its original store-only semantics (status filter only) and
is unaffected; every pre-existing caller (`active_nodes()` in this module,
agent-hub's `cclg_grounding.py`/`pack.py`) keeps this original behavior
unchanged because `patches` defaults to `None`.

```text
cclg pack-file <out.cclg> [--session ID ...] [--store PATH]
cclg open <in.cclg> [--json]
```

`cclg open` is read-only: it validates the file and prints the header plus
per-section counts (and any unknown-section / forward-compat warnings). It
never writes to a store.

## 12. Acceptance criteria

- `store -> pack_container -> load_container` round-trips node / patch / edge /
  session records losslessly, and `effective_view()` computed over the loaded
  nodes is identical to `effective_view()` computed over the original store's
  nodes.
- A container whose `@nodes` section has NOT baked a retiring patch's effect
  into target `status` (still `active`) still yields the correct, patch-aware
  effective view when read via `ContainerBundle.effective_view()` (§3.1.1) —
  the target is excluded regardless of its unbaked `status`.
- `patches.effective_view(nodes)` called without `patches=` (i.e. every
  pre-existing call site) is byte-for-byte unchanged: same signature default,
  same output, for any input that previously worked.
- `load_container` rejects bad magic, unsupported container version, header/body
  count mismatch (including a header `counts`/`sections` entry missing for a
  section present in the body, §5), and checksum mismatch — each as a distinct
  `ContainerError`.
- `load_container` accepts an unknown section with a warning and still loads
  the known sections (forward compatibility, §7), and likewise accepts a
  `header.format_id` mismatch with a warning rather than a hard failure (§4).
- The container carries zero Schift auth fields (§3.2) in either direction
  (`pack_container` refuses to emit one, `load_container` refuses to accept
  one).
