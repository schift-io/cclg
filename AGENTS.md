# CCLG Agent Notes

- Keep the runtime local-first unless the user explicitly asks for sync or API mode.
- Do not add external dependencies for the local MVP.
- Memory nodes require source provenance.
- Suppressed memory must not be injected as active memory.
- Prefer deterministic tests before adding model-dependent behavior.
- Keep benchmarks embedding-independent first; dense retrieval is optional.
