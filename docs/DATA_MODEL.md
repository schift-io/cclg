# CCLG 데이터 모델과 참조 흐름

기준 문서: `format/cclg.format.v0.1.toml`.

이 문서는 CCLG가 무엇을 어디에 저장하고, Hook/MCP/CLI가 언제 어떤
레코드를 읽는지 설명한다. API 모드는 이 로컬 MVP 범위에서 제외한다.

## 저장 루트

기본 저장소는 `CCLG_HOME` 또는 `~/.cclg`이다.

```text
~/.cclg/
  config.json
  raw/
  nodes/
  patches/
  edges/
  sessions/
  active/
    codegraphs/
  audit/
    memory_audit.jsonl
```

## 모델별 역할

| 모델 | 경로 | 주요 필드 | 쓰는 곳 | 읽는 곳 |
| --- | --- | --- | --- | --- |
| Raw evidence | `raw/` | 원문, hook payload, tool output, session event | `cclg raw`, `cclg ingest`, `cclg-hook` | `grep`, audit, source 검증 |
| MemoryNode | `nodes/{id}.json` | `schema_version`, `scope`, `key`, `content`, `status`, `source`, `relations`, `retrieval` | `cclg add`, `ingest --jsonl`, `patch`, `session overlay` | `search`, `bm25`, `pack`, Hook, MCP |
| MemoryPatch | `patches/{id}.json` | `operation`, `target_ids`, `new_node_ids`, `reason`, `source`, `resolution_policy`, `applied_at` | `cclg patch`, `forget`, MCP `memory.patch`, `rollback` | `doctor`, `audit`, `diff`, relation 추적 |
| MemoryEdge | `edges/{id}.json` | `from`, `to`, `type`, `source_patch_id` | `apply_patch()` | graph 추적, `doctor`, audit |
| SessionState | `sessions/{id}.json` | `agent`, `workspace`, `project`, `session_overlay_ids`, `pending_patch_ids`, `events`, `policy` | Hook, `cclg session` | session-scoped `pack`, audit |
| ActiveMemoryPack | runtime stdout | `query`, `memory_nodes`, `suppressed_nodes`, `budget` | `compile_pack()` | Hook `additionalContext`, CLI `pack`, MCP `memory.pack` |
| CodeGraph | `active/codegraphs/{repo}.json` | `files`, `symbols`, `edges`, `git` | `cclg code-index`, Hook `--include-codegraph` | `code-search`, Hook context, MCP |
| RetrievalIndex | `indexes/{bm25,graph,temporal}/*.json` | postings, adjacency, buckets | `cclg index` | `search`, fusion |
| Audit | `audit/memory_audit.jsonl` | `raw_written`, `patch_written`, `session_event`, `session_overlay_written` | store/session operations | `audit`, failure analysis |

## MemoryNode

장기 기억의 최소 단위다. `status=active`만 기본 effective view에 들어간다.
`active_session`은 같은 session id로 pack을 만들 때만 active처럼 소비된다.

```json
{
  "schema_version": "cclg.memory_node.v0.1",
  "id": "mem_abc123",
  "type": "project_decision",
  "scope": {
    "user": "user_local",
    "org": null,
    "workspace": "local",
    "project": "CCLG",
    "agent": "global",
    "session": null
  },
  "key": "project.cclg.local_first",
  "content": "CCLG is local-first by default.",
  "status": "active",
  "confidence": 1.0,
  "priority": "high",
  "created_at": "2026-06-30T00:00:00+00:00",
  "updated_at": "2026-06-30T00:00:00+00:00",
  "effective_from": "2026-06-30T00:00:00+00:00",
  "effective_until": null,
  "source": {
    "label": "manual:quickstart",
    "session_ids": [],
    "turn_ids": [],
    "raw_spans": [],
    "tool_result_ids": [],
    "artifact_ids": []
  },
  "relations": {
    "supersedes": [],
    "superseded_by": [],
    "refines": [],
    "expands": [],
    "narrows": [],
    "contradicts": [],
    "depends_on": [],
    "derived_from": []
  },
  "retrieval": {
    "sparse_keys": ["cclg", "local-first"],
    "dense_text": "CCLG is local-first by default.",
    "entity_keys": [],
    "temporal_keys": []
  },
  "metadata": {
    "created_by": "manual",
    "review_status": "auto_applied",
    "privacy": "local_default"
  }
}
```

## Patch와 Edge

기억은 덮어쓰지 않는다. `MemoryPatch`가 변경 의도와 근거를 남기고,
새 node와 기존 node 사이의 관계는 `MemoryEdge`로 남긴다.

```text
patch refine
  target_ids = [old]
  new_node_ids = [new]

edge
  from = new
  to = old
  type = refines
  source_patch_id = patch
```

기본 동작:

| operation | target 상태 | 새 node | edge |
| --- | --- | --- | --- |
| `create` | 없음 | active | 없음 |
| `supersede` | superseded | active | `supersedes` |
| `refine` | superseded | active | `refines` |
| `expand` | active 유지 | active | `expands` |
| `narrow` | superseded | active | `narrows` |
| `expire` | expired | 없음 | 없음 |
| `deprecate` | deprecated | 없음 | 없음 |
| `forget` | forgotten | 없음 | 없음 |
| `rollback` | target 재활성/새 node discarded | 없음 | 없음 |

## Hook 참조 순서

`cclg-hook user-prompt --include-codegraph --code-root "$PWD"`:

1. stdin JSON에서 prompt와 session id를 읽는다.
2. session id가 없으면 하나 만들고, 같은 id로 event와 pack을 처리한다.
3. prompt event를 `sessions/`, `raw/`, `audit/`에 저장한다.
4. `status=active`와 현재 session의 `active_session` node만 검색한다.
5. `compile_pack()`이 `ActiveMemoryPack`을 만든다.
6. `--include-codegraph`이면 repo의 files/symbols/import edges/git churn을
   `active/codegraphs/{repo}.json`에 저장하고 context에 붙인다.
7. hook stdout의 `hookSpecificOutput.additionalContext`가 에이전트에게
   주입된다.

`raw/` 전체와 `suppressed_nodes`는 current truth가 아니다. 기본 prompt로
주입되는 것은 active memory pack과 code graph pack이다.

## 명령별 읽기/쓰기

| 명령 | raw | nodes | patches | edges | sessions | active/codegraphs |
| --- | --- | --- | --- | --- | --- | --- |
| `cclg init` | dir | dir | dir | dir | dir | dir |
| `cclg ingest` | write raw 또는 JSONL import | optional write | - | - | audit | - |
| `cclg add` | - | write | - | - | - | - |
| `cclg patch` | - | target update + new node | write | write | audit | 다음 pack에 반영 |
| `cclg forget` | - | status=forgotten | write | - | audit | 다음 pack에서 제외 |
| `cclg grep` | read | read active | - | - | - | - |
| `cclg bm25/search` | - | read active | - | - | - | - |
| `cclg pack` | - | read effective | - | - | optional session | stdout |
| `cclg validate` | - | validate | validate | validate | validate | - |
| `cclg audit/doctor` | read refs | validate | validate | validate | validate | - |
| `cclg session overlay` | - | write `active_session` | - | - | update | session pack에 반영 |
| `cclg session end --policy promote/discard` | - | overlay→active/discarded | - | - | update | 다음 pack 반영 |
| `cclg session fork` | - | - | - | - | write child | overlay 격리 |
| `cclg session merge` | - | overlay→active 또는 `conflict_pending` | - | - | status=merged | 충돌 표시 |
| `cclg session promote` | - | overlay→active | - | - | audit | 장기 메모리 승격 |
| `cclg conflicts` | - | read `conflict_pending` | - | - | - | - |
| `cclg detect "<turn>"` | - | read effective | - | - | - | patch 후보 제안 |
| `cclg cite <id>` | read span | read node | - | - | - | provenance 복구 |
| `cclg rollback <patch>` | - | prior status 복원 + new node discard | write | - | audit | effective view 복원 |
| `cclg index` | read active | bm25/graph/temporal index | - | - | audit | write `indexes/` |
| `cclg code-index` | - | - | - | - | - | write CodeGraph |
| `cclg-hook user-prompt` | write event | read effective | - | - | write event | optional write graph |
| `cclg-mcp` | tool별 | search/add/pack | patch | audit | audit | code_search |
