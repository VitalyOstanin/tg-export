#!/usr/bin/env bash
# Runs the whole set of checks CI runs: the dependency lock, the linter, the
# formatter, the types, the tests with coverage and the per-module coverage
# floors. Same order as .github/workflows/ci.yml; a divergence is caught by
# tests/test_code_style.py::test_one_command_runs_everything_ci_runs.
#
#   scripts/check.sh          check (the way CI does)
#   scripts/check.sh --fix    fix what can be fixed first: ruff check --fix, ruff format
#
# Stops at the first failing check and exits with that check's own code.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--fix" ]]; then
    echo "==> ruff check --fix"
    uv run ruff check --fix .
    echo "==> ruff format"
    uv run ruff format .
elif [[ $# -gt 0 ]]; then
    echo "Unknown argument: $1 (only --fix is accepted)" >&2
    exit 2
fi

echo "==> uv lock --check"
uv lock --check

echo "==> ruff check"
uv run ruff check .

echo "==> ruff format --check"
uv run ruff format --check .

echo "==> pyright"
uv run pyright

echo "==> pytest with coverage"
uv run python -m pytest --cov-report=term

echo "==> per-module coverage floors"
uv run python scripts/coverage_gate.py

echo "All checks passed."
