"""Release gate: tag, manifest version and CHANGELOG must agree.

Extracts the release notes of one version from CHANGELOG.md and refuses the
release when the pieces disagree. Meant to run *before* the publish step: the
only consistency check used to sit after the upload to PyPI, so a missing
CHANGELOG section produced a published version, no GitHub Release and a red
workflow -- and PyPI has no undo.

Usage:

    uv run python scripts/release_notes.py "$GITHUB_REF_NAME" --output release_notes.md
"""

from __future__ import annotations

import argparse
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
    major, minor, _ = (int(part) for part in previous.split("."))
    smallest = f"{major + 1}.0.0" if required == "major" else f"{major}.{minor + 1}.0"
    reason = f"section '{BREAKING_HEADING}'" if required == "major" else f"section '{FEATURE_HEADING}'"
    step = f"a {actual} bump" if actual else "not a bump at all"
    raise SystemExit(
        f"version {version} is {step} over {previous}, but the notes carry a {reason} and ask for "
        f"a {required} bump: release it as {smallest} or later, or move the entries into a "
        f"category that matches the number."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, with or without the leading 'v'")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, default=Path("release_notes.md"))
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
    args.output.write_text(f"## tg-export v{version} -- {date}\n\n{body}\n", encoding="utf-8")
    print(f"release notes for {version} ({date}): {len(body.splitlines())} lines -> {args.output}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"Error: {e}", file=sys.stderr)
            raise SystemExit(1) from None
        raise
