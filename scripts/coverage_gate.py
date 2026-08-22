"""Per-module coverage floors.

A single project-wide fail_under hides the modules that matter most: with the
whole tree at 56% the largest files could sit at 13% and the gate would still
pass. This script checks each module against its own floor, declared in
pyproject.toml under [tool.tg-export.coverage-floor].

Usage (after a run that produced .coverage):

    uv run python -m pytest --cov=tg_export
    uv run python scripts/coverage_gate.py

The floors are a regression guard, not a target: they sit slightly below the
current numbers, so they fail when coverage drops rather than demanding growth.
"""

from __future__ import annotations

import json
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _floors() -> dict[str, float]:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    return pyproject["tool"]["tg-export"]["coverage-floor"]


def _measured() -> dict[str, float]:
    import coverage

    cov = coverage.Coverage(data_file=str(ROOT / ".coverage"))
    cov.load()
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "coverage.json"
        cov.json_report(outfile=str(report_path))
        report = json.loads(report_path.read_text())
    return {
        path.replace("\\", "/"): data["summary"]["percent_covered"] for path, data in report["files"].items()
    }


def main() -> int:
    floors = _floors()
    measured = _measured()

    missing = sorted(set(floors) - set(measured))
    if missing:
        print(f"FAIL: no coverage data for {missing}; run pytest --cov=tg_export first", file=sys.stderr)
        return 1

    undeclared = sorted(p for p in measured if p not in floors)
    if undeclared:
        print(f"FAIL: modules without a declared floor: {undeclared}", file=sys.stderr)
        return 1

    failures = []
    for path, floor in sorted(floors.items()):
        actual = measured[path]
        mark = "ok " if actual >= floor else "LOW"
        print(f"{mark} {path:32} {actual:5.1f}% (floor {floor:.0f}%)")
        if actual < floor:
            failures.append((path, actual, floor))

    if failures:
        print("", file=sys.stderr)
        for path, actual, floor in failures:
            print(f"FAIL: {path} at {actual:.1f}%, floor {floor:.0f}%", file=sys.stderr)
        return 1

    print(f"\nOK: {len(floors)} modules at or above their floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
