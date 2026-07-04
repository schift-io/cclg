# Publishing to GitHub

Target public repository:

```text
https://github.com/schift-io/cclg
```

This repo is prepared for public release but publishing is intentionally an
explicit action.

## Dry Run

```bash
scripts/publish-github.sh
```

## Create and Push

```bash
scripts/publish-github.sh --execute
```

The script defaults to:

```text
ORG=schift-io
NAME=cclg
VISIBILITY=public
```

Override if needed:

```bash
ORG=schift-io NAME=cclg VISIBILITY=public scripts/publish-github.sh --execute
```

## After Publish

1. Confirm repository visibility is public.
2. Enable GitHub vulnerability reporting if available.
3. Add repo topics: `agent-memory`, `mcp`, `codex`, `claude-code`,
   `codegraph`, `local-first`.
4. Confirm CI passes.
5. Test public install:

```bash
curl -fsSL https://raw.githubusercontent.com/schift-io/cclg/main/scripts/install.sh | bash
```

Or from a fresh clone:

```bash
tmpdir="$(mktemp -d)"
git clone https://github.com/schift-io/cclg.git "$tmpdir/cclg"
"$tmpdir/cclg/scripts/install.sh" --from-checkout
```
