#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv build --wheel --out-dir dist
uv export --locked --no-dev --no-emit-project --no-hashes --no-header --output-file dist/constraints.txt
uv run --locked python scripts/release-assets.py
