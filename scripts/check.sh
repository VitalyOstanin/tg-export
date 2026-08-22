#!/usr/bin/env bash
# Прогоняет весь набор проверок, который выполняет CI: блокировка зависимостей,
# линтер, формат, типы, тесты с покрытием и границы покрытия по модулям.
# Порядок тот же, что в .github/workflows/ci.yml; расхождение ловит тест
# tests/test_code_style.py::test_one_command_runs_everything_ci_runs.
#
#   scripts/check.sh          проверить (как на CI)
#   scripts/check.sh --fix    сначала исправить исправимое: ruff check --fix и ruff format
#
# Останавливается на первой упавшей проверке, код возврата -- её собственный.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--fix" ]]; then
    echo "==> ruff check --fix"
    uv run ruff check --fix .
    echo "==> ruff format"
    uv run ruff format .
elif [[ $# -gt 0 ]]; then
    echo "Неизвестный аргумент: $1 (допустим только --fix)" >&2
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

echo "==> pytest с покрытием"
uv run python -m pytest --cov-report=term

echo "==> границы покрытия по модулям"
uv run python scripts/coverage_gate.py

echo "Все проверки пройдены."
