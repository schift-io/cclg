# Security

CCLG stores memory and raw sources locally by default under `~/.cclg`.

## Reporting

Until the public repository is created, report issues privately to the Schift
maintainers. After publication, use GitHub private vulnerability reporting if it
is enabled for `schift-io/cclg`.

## Sensitive Data

- Do not commit raw private transcripts.
- Do not commit OAuth tokens, API keys, cookies, or local agent config secrets.
- `scripts/install.sh` copies hook/MCP examples only; it does not merge into
  existing user config.
- `scripts/uninstall.sh` keeps `~/.cclg` by default. Use `--purge-store` only
  when you intentionally want to delete local memory data.
