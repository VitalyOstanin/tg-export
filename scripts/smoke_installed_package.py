"""Check that an installed tg-export can actually render, not just start.

Run against a wheel installed into a throwaway environment, from outside the
project directory -- inside it the editable install of the working tree wins,
the templates are always in place, and the check passes on a broken wheel:

    cd "$(mktemp -d)" && uv run --no-project --with "$OLDPWD"/dist/*.whl \
        python "$OLDPWD"/scripts/smoke_installed_package.py

`--version` and `--help` pass even when the wheel carries nothing but *.py,
which is how a packaging regression once shipped: every export died on
`TemplateNotFound: 'index.html.j2'`. This script exercises the data files --
it compiles every template and renders the index into a temporary directory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    from tg_export.config import OutputConfig
    from tg_export.html.renderer import STATIC_DIR, TEMPLATES_DIR, HtmlRenderer

    templates = sorted(TEMPLATES_DIR.glob("*.j2"))
    if not templates:
        print(f"FAIL: no templates under {TEMPLATES_DIR}", file=sys.stderr)
        return 1

    static = [p for p in STATIC_DIR.rglob("*") if p.is_file()]
    if not static:
        print(f"FAIL: no static assets under {STATIC_DIR}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "export"
        renderer = HtmlRenderer(output_dir=out, config=OutputConfig())

        for template in templates:
            renderer.env.get_template(template.name)

        renderer.setup()
        renderer.render_index(folders_list=[], unfiled=[], sections=[])

        index = out / "index.html"
        if not index.is_file() or not index.read_text(encoding="utf-8").strip():
            print("FAIL: index.html was not rendered", file=sys.stderr)
            return 1
        for asset in ("css/style.css", "js/script.js"):
            if not (out / asset).is_file():
                print(f"FAIL: static asset missing after setup(): {asset}", file=sys.stderr)
                return 1

    print(f"OK: {len(templates)} templates compiled, {len(static)} static files, index rendered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
