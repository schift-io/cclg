# Contributing

CCLG is alpha software. Keep changes small and testable.

## Local Checks

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
cclg bench run --suite all --repo-root "$PWD"
```

## Design Rules

- Keep local-first behavior as the default.
- Do not add hosted API behavior to the core runtime without a separate design.
- Do not inject `superseded`, `expired`, or `forgotten` memory as active context.
- Prefer deterministic retrieval and benchmarks before model-dependent features.
- Treat CodeGraphPack as context ordering, not source proof.
