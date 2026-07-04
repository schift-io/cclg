# Codex Local Apply

`scripts/apply-local.sh` applies CCLG to this machine without touching existing
project repositories.

It does three things:

1. Initializes `~/.cclg`.
2. Installs `~/.local/bin/cclg`, `acmc`, `cclg-hook`, and `cclg-mcp` as wrappers around
   this checkout.
3. Writes CCLG Codex skills under `~/.codex/skills/`.
4. Copies safe hook/MCP config examples into `~/.codex` and `~/.claude`.

The skill tells future Codex sessions to run:

```bash
cclg pack --query "<current task>" --format toml
```

The output is an ActiveMemoryPack. It is a hint layer, not proof. Drift-prone
facts still need live verification against files, tools, or source artifacts.

## Manual Smoke

```bash
cclg status
cclg grep "local-first"
cclg search "local-first"
cclg pack --query "CCLG implementation priorities" --format toml
cclg audit
cclg code-search "MemoryPatch compile_pack" --repo "$PWD"
cclg bench run --suite all --repo-root "$PWD"
```

## Installed Skills

- `cclg-memory`: load/search/pack active memory.
- `cclg-codegraph`: build/search repo code graph context.
- `cclg-hooks`: wire or validate hook/MCP surfaces.
- `cclg-bench`: run behavior verification suites.
