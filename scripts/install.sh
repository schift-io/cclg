#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${CCLG_REPO_URL:-https://github.com/schift-io/cclg.git}"
APP_HOME="${CCLG_APP_HOME:-$HOME/.local/share/cclg}"
BIN_DIR="${CCLG_BIN_DIR:-$HOME/.local/bin}"
STORE_HOME="${CCLG_HOME:-$HOME/.cclg}"
PYTHON_BIN="${PYTHON:-python3}"
FROM_CHECKOUT=0
EDITABLE=0

usage() {
  cat <<'USAGE'
Install CCLG locally.

Usage:
  scripts/install.sh [--from-checkout] [--editable]

Environment:
  CCLG_REPO_URL   Git URL used when not installing from a checkout.
                  Default: https://github.com/schift-io/cclg.git
  CCLG_APP_HOME   Install directory. Default: ~/.local/share/cclg
  CCLG_BIN_DIR    Shim directory. Default: ~/.local/bin
  CCLG_HOME       Memory store. Default: ~/.cclg
  PYTHON          Python executable. Default: python3

Examples:
  curl -fsSL https://raw.githubusercontent.com/schift-io/cclg/main/scripts/install.sh | bash

  git clone https://github.com/schift-io/cclg.git
  cd cclg
  ./scripts/install.sh --from-checkout

  CCLG_REPO_URL=https://github.com/schift-io/cclg.git bash scripts/install.sh
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-checkout)
      FROM_CHECKOUT=1
      shift
      ;;
    --editable)
      EDITABLE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v "$PYTHON_BIN" >/dev/null || {
  echo "python executable not found: $PYTHON_BIN" >&2
  exit 1
}

mkdir -p "$APP_HOME" "$BIN_DIR" "$STORE_HOME"

if [[ "$FROM_CHECKOUT" == "1" ]]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  if [[ -d "$APP_HOME/repo/.git" ]]; then
    git -C "$APP_HOME/repo" pull --ff-only
  else
    rm -rf "$APP_HOME/repo"
    git clone "$REPO_URL" "$APP_HOME/repo"
  fi
  REPO_ROOT="$APP_HOME/repo"
fi

VENV="$APP_HOME/venv"
"$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
if [[ "$EDITABLE" == "1" ]]; then
  "$VENV/bin/python" -m pip install -e "$REPO_ROOT" >/dev/null
else
  "$VENV/bin/python" -m pip install "$REPO_ROOT" >/dev/null
fi

for command_name in cclg acmc cclg-hook cclg-mcp; do
  cat > "$BIN_DIR/$command_name" <<EOF
#!/usr/bin/env bash
export CCLG_HOME="\${CCLG_HOME:-$STORE_HOME}"
exec "$VENV/bin/$command_name" "\$@"
EOF
  chmod +x "$BIN_DIR/$command_name"
done

"$BIN_DIR/cclg" init
"$BIN_DIR/cclg" import-jsonl "$REPO_ROOT/examples/acmc_seed.jsonl" >/dev/null
"$BIN_DIR/cclg" index >/dev/null
"$BIN_DIR/cclg" apply-codex --write-skill >/dev/null

# Optional dense retrieval (off by default, local-first). Backends: local
# (sentence-transformers), ollama, or hosted APIs (openai/schift/google/cloudflare)
# detected from env keys. API backends need only stdlib; local needs cclg[dense].
# Non-interactive: CCLG_DENSE_PROVIDER=<provider> [CCLG_DENSE_MODEL=<id>].
DENSE_PROVIDER="${CCLG_DENSE_PROVIDER:-}"
DENSE_MODEL="${CCLG_DENSE_MODEL:-}"
if [[ -z "$DENSE_PROVIDER" && -t 0 ]]; then
  echo
  echo "Optional dense retrieval. Providers: auto | local | ollama | openai | schift | google | cloudflare"
  echo "  auto picks from API keys in your env (OPENAI_API_KEY, SCHIFT_API_KEY, GOOGLE_API_KEY, CLOUDFLARE_API_TOKEN, OLLAMA_HOST), else local."
  echo "Leave blank to skip (sparse grep/BM25/graph only)."
  read -r -p "Dense provider [blank=skip]: " DENSE_PROVIDER || DENSE_PROVIDER=""
fi
if [[ -n "$DENSE_PROVIDER" ]]; then
  # The local sentence-transformers backend is the only one needing extra deps.
  if [[ "$DENSE_PROVIDER" == "local" || ( "$DENSE_PROVIDER" == "auto" && -z "${OPENAI_API_KEY:-}${SCHIFT_API_KEY:-}${GOOGLE_API_KEY:-}${GEMINI_API_KEY:-}${CLOUDFLARE_API_TOKEN:-}${OLLAMA_HOST:-}" ) ]]; then
    echo "Installing local embedding deps (cclg[dense])..."
    "$VENV/bin/python" -m pip install "$REPO_ROOT[dense]" >/dev/null
    : "${DENSE_MODEL:=ibm-granite/granite-embedding-97m-multilingual-r2}"
  fi
  ENABLE_ARGS=(--provider "$DENSE_PROVIDER")
  [[ -n "$DENSE_MODEL" ]] && ENABLE_ARGS+=(--model "$DENSE_MODEL")
  "$BIN_DIR/cclg" dense enable "${ENABLE_ARGS[@]}" >/dev/null || echo "dense enable skipped (configure later with: cclg dense enable)"
  "$BIN_DIR/cclg" dense status || true
fi

mkdir -p "$HOME/.codex" "$HOME/.claude"
render_example() {
  sed "s|{{CCLG_BIN_DIR}}|$BIN_DIR|g" "$1" > "$2"
}
render_example "$REPO_ROOT/adapters/codex/hooks.cclg.example.json" "$HOME/.codex/hooks.cclg-example.json"
render_example "$REPO_ROOT/adapters/codex/mcp.cclg.example.json" "$HOME/.codex/mcp.cclg-example.json"
render_example "$REPO_ROOT/adapters/claude/settings.cclg.example.json" "$HOME/.claude/settings.cclg-example.json"

cat <<EOF
CCLG installed.

Commands:
  $BIN_DIR/cclg
  $BIN_DIR/acmc
  $BIN_DIR/cclg-hook
  $BIN_DIR/cclg-mcp

Install root:
  $APP_HOME

Memory store:
  $STORE_HOME

Codex skills:
  $HOME/.codex/skills/cclg-memory
  $HOME/.codex/skills/cclg-codegraph
  $HOME/.codex/skills/cclg-hooks
  $HOME/.codex/skills/cclg-bench

Config examples:
  $HOME/.codex/hooks.cclg-example.json
  $HOME/.codex/mcp.cclg-example.json
  $HOME/.claude/settings.cclg-example.json

Next:
  cclg bench run --suite all --repo-root "$REPO_ROOT"
EOF
