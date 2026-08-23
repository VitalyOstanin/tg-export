"""Release gate: tag, manifest version and CHANGELOG must agree.

Extracts the release notes of one version from CHANGELOG.md and refuses the
release when the pieces disagree. Meant to run *before* the publish step: the
only consistency check used to sit after the upload to PyPI, so a missing
CHANGELOG section produced a published version, no GitHub Release and a red
workflow -- and PyPI has no undo.

Usage:

    uv run python scripts/release_notes.py "$GITHUB_REF_NAME" \\
        --tag-date "$(git log -1 --format=%cs "$GITHUB_REF_NAME")" \\
        --output release_notes.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import tomllib
from pathlib import Path


def _manifest_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _section(changelog: str, version: str) -> tuple[str, str]:
    """Return (date, body) of the section of ``version``; raise if it is absent."""
    header = re.search(rf"^## \[{re.escape(version)}\][^\n]*$", changelog, re.MULTILINE)
    if header is None:
        raise SystemExit(
            f"CHANGELOG.md has no section for version {version} (expected '## [{version}] -- DATE')"
        )
    date_match = re.search(r"--\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", header.group(0))
    if date_match is None:
        raise SystemExit(f"CHANGELOG.md section '{header.group(0)}' carries no date in the form YYYY-MM-DD")
    rest = changelog[header.end() :]
    next_header = re.search(r"^## \[", rest, re.MULTILINE)
    body = (rest[: next_header.start()] if next_header else rest).strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md section for {version} is empty -- a release needs its notes")
    return date_match.group(1), body


# Categories of the CHANGELOG that carry a size: everything else releases as a patch.
BREAKING_HEADING = "Ломающие изменения"
FEATURE_HEADING = "Добавлено"

_ORDER = ("patch", "minor", "major")


def _previous_version(changelog: str, version: str) -> str | None:
    """Version of the section right below ``version``; None when it is the first one."""
    versions = re.findall(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", changelog, re.MULTILINE)
    if version not in versions:
        return None
    below = versions[versions.index(version) + 1 :]
    return below[0] if below else None


def _required_bump(body: str) -> str:
    """Smallest bump the notes of this section justify."""
    headings = [h.strip() for h in re.findall(r"^###\s+(.+?)\s*$", body, re.MULTILINE)]
    if BREAKING_HEADING in headings:
        return "major"
    if FEATURE_HEADING in headings:
        return "minor"
    return "patch"


def _actual_bump(previous: str, current: str) -> str | None:
    """Size of the step from ``previous`` to ``current``; None when it is not a step up."""
    old = tuple(int(part) for part in previous.split("."))
    new = tuple(int(part) for part in current.split("."))
    if new[0] > old[0]:
        return "major"
    if new[:1] == old[:1] and new[1] > old[1]:
        return "minor"
    if new[:2] == old[:2] and new[2] > old[2]:
        return "patch"
    return None


def _check_bump(changelog: str, version: str, body: str) -> None:
    """Refuse a version number smaller than what the notes announce.

    The gate used to check the tag, the manifest and the presence of notes, but
    not whether the number matches what accumulated: a section changing exit
    codes or the output directory could ship as a patch, and PyPI has no undo.
    """
    previous = _previous_version(changelog, version)
    if previous is None:
        return
    required = _required_bump(body)
    actual = _actual_bump(previous, version)
    if actual is not None and _ORDER.index(actual) >= _ORDER.index(required):
        return
    major, minor, patch = (int(part) for part in previous.split("."))
    if actual is None:
        # Not a step up at all: the section below carries a number that is not
        # smaller than this one -- a duplicated section, sections out of order,
        # or a number that went down. The notes have nothing to do with it, so
        # naming a category here would send the reader to the wrong place.
        raise SystemExit(
            f"version {version} is not a step up over {previous}, the number of the section below "
            f"it in the changelog: check that the sections are in order and that no number repeats."
        )
    smallest = {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[required]
    reason = {
        "major": f"section '{BREAKING_HEADING}'",
        "minor": f"section '{FEATURE_HEADING}'",
        "patch": "entries of its own",
    }[required]
    raise SystemExit(
        f"version {version} is a {actual} bump over {previous}, but the notes carry a {reason} and "
        f"ask for a {required} bump: release it as {smallest} or later, or move the entries into a "
        f"category that matches the number."
    )


# The section is written before the tag is pushed, so a day apart is normal.
DATE_SLACK = dt.timedelta(days=1)


def _check_date(section_date: str, tag_date: str) -> None:
    """Refuse a section whose date is not the date the tag was cut.

    The gate used to check the shape of the date and nothing else, so a date
    copied from the section above -- what renaming "Не выпущено" invites --
    passed and became the heading of the release notes on GitHub. By the file
    alone that is indistinguishable from two releases in one day.
    """
    try:
        section = dt.date.fromisoformat(section_date)
        tagged = dt.date.fromisoformat(tag_date)
    except ValueError as e:
        raise SystemExit(f"--tag-date must be a date in the form YYYY-MM-DD: {e}") from e
    if abs(section - tagged) > DATE_SLACK:
        raise SystemExit(
            f"CHANGELOG section is dated {section_date}, but the tag points at a commit of "
            f"{tag_date}. Fix the date of the section -- it is what the release notes are headed with."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, with or without the leading 'v'")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the notes here; without it they go to stdout and no file is left behind",
    )
    parser.add_argument("--tag-date", default=None, help="date of the tagged commit, YYYY-MM-DD")
    args = parser.parse_args()

    version = args.tag.removeprefix("v")
    manifest = _manifest_version(args.root)
    if version != manifest:
        raise SystemExit(
            f"tag {args.tag} does not match the version in pyproject.toml: {version} != {manifest}. "
            f"Bump the manifest (and uv.lock) in the commit the tag points at."
        )

    changelog = (args.root / "CHANGELOG.md").read_text(encoding="utf-8")
    date, body = _section(changelog, version)
    _check_bump(changelog, version, body)
    if args.tag_date:
        _check_date(date, args.tag_date)
    notes = f"## tg-export v{version} -- {date}\n\n{body}\n"
    if args.output is None:
        print(notes)
    else:
        args.output.write_text(notes, encoding="utf-8")
    where = args.output or "stdout"
    print(
        f"release notes for {version} ({date}): {len(body.splitlines())} lines -> {where}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
