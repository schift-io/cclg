.PHONY: test bench install uninstall code-index mcp-smoke clean

PYTHON ?= python3
REPO_ROOT := $(CURDIR)

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

bench:
	$(HOME)/.local/bin/cclg bench run --suite all --repo-root "$(REPO_ROOT)"

install:
	./scripts/install.sh --from-checkout

uninstall:
	./scripts/uninstall.sh

code-index:
	$(HOME)/.local/bin/cclg code-index "$(REPO_ROOT)" --json

mcp-smoke:
	printf '%s\n' \
	  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
	  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
	  | $(HOME)/.local/bin/cclg-mcp --line-delimited

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
