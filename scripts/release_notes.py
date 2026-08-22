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

    date, body = _section((args.root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
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
