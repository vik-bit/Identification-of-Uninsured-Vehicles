"""
show_changes.py — RUBRIC 3: highlight what we changed in the AI-generated code.

    python phase2_vibe/show_changes.py            -> writes phase2_vibe/changes.html
    python phase2_vibe/show_changes.py --stat     -> just the line counts

generated/<file>  = the code exactly as the AI (Cursor) generated it from PROMPTS.md (prompt 1)
../<file>         = the code after our review + fixes (see CHANGES.md)
"""

from __future__ import annotations

import difflib
import html
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FILES = ["entity_matching.py", "populate_noisy_data.py", "plate_ocr.py", "app.py"]


def stats(a: list[str], b: list[str]) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def unified(name: str, context: int = 3) -> str:
    a = (HERE / "generated" / name).read_text(encoding="utf-8").splitlines()
    b = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return "\n".join(difflib.unified_diff(a, b, f"generated/{name}", f"current/{name}", lineterm="", n=context))


def build_html() -> Path:
    parts = []
    summary = []
    for name in FILES:
        gen, cur = HERE / "generated" / name, ROOT / name
        if not (gen.exists() and cur.exists()):
            continue
        a = gen.read_text(encoding="utf-8").splitlines()
        b = cur.read_text(encoding="utf-8").splitlines()
        add, rem = stats(a, b)
        summary.append(f"<tr><td><a href='#{name}'>{name}</a></td><td>{len(a)}</td><td>{len(b)}</td>"
                       f"<td class='add'>+{add}</td><td class='rem'>-{rem}</td></tr>")
        diff = difflib.HtmlDiff(wrapcolumn=95).make_table(
            a, b, fromdesc=f"AI-generated {name}", todesc=f"after our changes {name}", context=True, numlines=2)
        parts.append(f"<h2 id='{name}'>{name}</h2>{diff}")
    changes_md = (HERE / "CHANGES.md").read_text(encoding="utf-8") if (HERE / "CHANGES.md").exists() else ""
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Phase 2 code changes</title>
<style>
 body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}}
 table.diff{{font-family:Consolas,monospace;font-size:12px;border-collapse:collapse;width:100%}}
 .diff_header{{background:#eee;color:#888}} td.diff_header{{text-align:right;padding:0 4px}}
 .diff_next{{background:#ddd}} .diff_add{{background:#c8f0c8}} .diff_chg{{background:#fff3a8}}
 .diff_sub{{background:#f7c6c6}} th{{text-align:left}}
 .add{{color:#1a7f37}} .rem{{color:#cf222e}} pre{{white-space:pre-wrap;background:#f6f8fa;padding:12px}}
 .legend span{{padding:2px 8px;margin-right:8px}}
</style></head><body>
<h1>Phase 2 — AI-generated code vs our final code</h1>
<p>Generated {datetime.now():%Y-%m-%d %H:%M}. Left = exactly what the AI produced (phase2_vibe/generated/),
right = what is in the project now.</p>
<p class="legend"><span class="diff_add">added</span><span class="diff_chg">changed</span><span class="diff_sub">removed</span></p>
<table><tr><th>file</th><th>generated lines</th><th>now</th><th>added</th><th>removed</th></tr>{''.join(summary)}</table>
<h2>Why we changed it</h2><pre>{html.escape(changes_md)}</pre>
{''.join(parts)}
</body></html>"""
    out = HERE / "changes.html"
    out.write_text(page, encoding="utf-8")
    return out


if __name__ == "__main__":
    if "--stat" in sys.argv:
        for n in FILES:
            a = (HERE / "generated" / n).read_text(encoding="utf-8").splitlines()
            b = (ROOT / n).read_text(encoding="utf-8").splitlines()
            print(n, "+%d -%d" % stats(a, b))
    else:
        p = build_html()
        print(f"wrote {p}")
