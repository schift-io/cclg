#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${CCLG_APP_HOME:-$HOME/.local/share/cclg}"
BIN_DIR="${CCLG_BIN_DIR:-$HOME/.local/bin}"
PURGE_STORE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge-store)
      PURGE_STORE=1
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
Uninstall CCLG local commands.

Usage:
  scripts/uninstall.sh [--purge-store]

Without --purge-store this removes commands and installed app files but keeps
~/.cclg memory data.
USAGE
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

rm -f "$BIN_DIR/cclg" "$BIN_DIR/acmc" "$BIN_DIR/cclg-hook" "$BIN_DIR/cclg-mcp"
rm -rf "$APP_HOME"

if [[ "$PURGE_STORE" == "1" ]]; then
  rm -rf "${CCLG_HOME:-$HOME/.cclg}"
fi

echo "CCLG uninstalled. Memory store kept unless --purge-store was used."
