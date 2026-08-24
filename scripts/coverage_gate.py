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
That only works while they follow the numbers: a floor left far below the
actual coverage catches a collapse and misses the regression it exists for, so
one that fell behind by more than SLACK is reported here with the value to
write instead of it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# How far a floor may sit below the measured coverage. Five points is what the
# floors were set to when they were last written by hand; more than that and
# the guard only reacts to a collapse. Compared on whole points, so that a
# floor written from `int(actual) - SLACK` does not read as behind at once.
SLACK = 5.0


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
        try:
            cov.json_report(outfile=str(report_path))
        except coverage.exceptions.NoDataError:
            # The usual way to get here is a fresh clone where pytest has not
            # run yet: the gate is offered as a command of its own, and a stack
            # trace from the library says nothing about what to do.
            print("FAIL: no coverage data; run pytest --cov=tg_export first", file=sys.stderr)
            raise SystemExit(1) from None
        report = json.loads(report_path.read_text(encoding="utf-8"))
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
        print(
            f"FAIL: modules without a declared floor: {undeclared}; "
            f"add them to [tool.tg-export.coverage-floor] in pyproject.toml "
            f"(floor = current coverage minus SLACK, that is {SLACK:g} points)",
            file=sys.stderr,
        )
        return 1

    failures = []
    behind = []
    for path, floor in sorted(floors.items()):
        actual = measured[path]
        mark = "ok " if actual >= floor else "LOW"
        print(f"{mark} {path:32} {actual:5.1f}% (floor {floor:.0f}%)")
        if actual < floor:
            failures.append((path, actual, floor))
        elif int(actual) - floor > SLACK:
            behind.append((path, actual, floor))

    if failures:
        print("", file=sys.stderr)
        for path, actual, floor in failures:
            print(f"FAIL: {path} at {actual:.1f}%, floor {floor:.0f}%", file=sys.stderr)
        return 1

    if behind:
        print("", file=sys.stderr)
        for path, actual, floor in behind:
            raised = int(actual - SLACK)
            print(
                f"FAIL: {path} at {actual:.1f}% has floor {floor:.0f}%, "
                f"more than {SLACK:.0f} points behind: write {raised} in "
                f"[tool.tg-export.coverage-floor] of pyproject.toml",
                file=sys.stderr,
            )
        return 1

    print(f"\nOK: {len(floors)} modules within {SLACK:.0f} points of their floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
