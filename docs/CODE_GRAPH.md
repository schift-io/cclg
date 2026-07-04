# Code Graph

CCLG includes a local code graph layer so coding agents can consume repository
context without a hosted code intelligence service.

## Command

```bash
cclg code-index /path/to/repo
cclg code-search "compile_pack MemoryPatch" --repo /path/to/repo
```

## What Is Indexed

- git-tracked source files when the target is a git repo
- fallback filesystem walk outside git repos
- file path, language, line count, byte count
- Python symbols via `ast`
- JavaScript/TypeScript symbols via regex
- Python and JS/TS imports
- import edges
- define edges
- co-change edges (files changed together in the same commit, count >= 2)
- git churn from `git log --name-only`
- per-file git recency (`git_last_modified`) and distinct author count (`git_authors`)

## Output

`code-search` returns a CodeGraphPack:

```text
# CodeGraphPack

root: /repo
query: MemoryPatch

## Relevant Code
- symbol `MemoryPatch` class in `src/cclg/models.py:96`
- file `src/cclg/cli.py` lang=python churn=...
```

This is not a replacement for reading files. It is a context-ordering and
retrieval-hint layer. Final claims should still cite or inspect real source
files.

## Why This Shape

The source discussion asked for code graph support without jumping straight to a
heavy graph database. This implementation starts with the useful local facts:
file map, symbols, imports, define/import edges, and git history. Tree-sitter,
LSP, SCIP, and graph DB can be added later behind the same pack interface.
