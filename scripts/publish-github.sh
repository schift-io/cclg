#!/usr/bin/env bash
set -euo pipefail

ORG="${ORG:-schift-io}"
NAME="${NAME:-cclg}"
VISIBILITY="${VISIBILITY:-public}"
EXECUTE=0

usage() {
  cat <<'USAGE'
Prepare or publish the CCLG repository to GitHub.

Dry-run by default:
  scripts/publish-github.sh

Actually create/push:
  scripts/publish-github.sh --execute

Environment:
  ORG=schift-io
  NAME=cclg
  VISIBILITY=public
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute)
      EXECUTE=1
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

command -v gh >/dev/null || {
  echo "gh CLI is required" >&2
  exit 1
}

git diff --quiet || {
  echo "working tree has unstaged changes; commit first" >&2
  exit 1
}

git diff --cached --quiet || {
  echo "index has staged changes; commit first" >&2
  exit 1
}

repo="$ORG/$NAME"
url="https://github.com/$repo"

if [[ "$EXECUTE" != "1" ]]; then
  cat <<EOF
Dry run. No GitHub changes made.

Would publish:
  repo: $repo
  visibility: $VISIBILITY
  url: $url

Commands:
  gh repo create "$repo" --$VISIBILITY --source=. --remote=origin
  git push -u origin HEAD:main

If repo already exists:
  git remote add origin "$url.git"
  git push -u origin HEAD:main

Run with --execute to create/push through gh.
EOF
  exit 0
fi

if gh repo view "$repo" >/dev/null 2>&1; then
  if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "$url.git"
  fi
  git push -u origin HEAD:main
else
  gh repo create "$repo" "--$VISIBILITY" --source=. --remote=origin
  git push -u origin HEAD:main
fi
