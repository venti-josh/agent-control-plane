#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
uv sync --locked
if [ ! -f .env ]; then
    cp .env.example .env
fi
ACP_CONFIG_FILE="$PWD/.env" uv run --locked python -c 'from acp.config import Settings; Settings.from_env(); print("Configuration valid. Run make serve, or make check to verify the installation.")'
