.PHONY: setup check serve hooks

setup:
	sh scripts/setup.sh

check:
	uv run --locked pytest -q
	uv run --locked ruff check --select E9,F63,F7,F82 acp tests
	node --check acp/static/app.js

serve:
	ACP_CONFIG_FILE="$(CURDIR)/.env" uv run --locked acp serve

hooks:
	ACP_CONFIG_FILE="$(CURDIR)/.env" uv run --locked acp hooks
