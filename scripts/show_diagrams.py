"""Open docs/DIAGRAMS.md rendered in your browser (for screen recordings and reviews).

    uv run python scripts/show_diagrams.py [--no-open]

Builds one self-contained page in a temporary folder from the mermaid blocks of docs/DIAGRAMS.md and
opens it. The page loads mermaid 10.9.1 from the jsDelivr CDN, so it needs internet; nothing is
written into the repository.
"""

from __future__ import annotations

import argparse
import html
import re
import tempfile
import webbrowser
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "DIAGRAMS.md"
PAGE = """<!doctype html><meta charset="utf-8"><title>VitalMesh diagrams</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:1200px;margin:0 auto;padding:24px;
background:#fff;color:#1b1f24}}
h2{{margin-top:48px}} p{{max-width:70ch;color:#4a5560}} pre.mermaid{{background:#fff}}
</style>
<body>{body}
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>
mermaid.initialize({{startOnLoad: true, theme: "neutral", flowchart: {{useMaxWidth: true}}}});
</script>
"""


def build() -> str:
    parts: list[str] = []
    text = DOC.read_text(encoding="utf-8")
    for chunk in re.split(r"(?m)^## ", text)[1:]:
        title, _, rest = chunk.partition("\n")
        diagram = re.search(r"```mermaid\n(.*?)```", rest, flags=re.S)
        intro = rest.split("```mermaid")[0].strip()
        parts.append(f"<h2>{html.escape(title)}</h2><p>{html.escape(intro)}</p>")
        if diagram:
            parts.append(f'<pre class="mermaid">{html.escape(diagram.group(1))}</pre>')
    return PAGE.format(body="\n".join(parts))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    out = Path(tempfile.mkdtemp(prefix="vitalmesh-diagrams-")) / "diagrams.html"
    out.write_text(build(), encoding="utf-8")
    print(out)
    if not args.no_open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
