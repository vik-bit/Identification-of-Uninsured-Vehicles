"""Build AI-chat submission PDF + zip the project codebase."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "submission_pack"
PDF_PATH = OUT_DIR / "AI_Chat_History_Links.pdf"
ZIP_PATH = ROOT.parent / "Identification-of-Uninsured-Vehicles_CODEBASE.zip"

# Public AI share links (student-provided)
CHAT_LINKS = [
    (
        "Claude (project chat)",
        "https://claude.ai/share/ef26816a-0ead-43f7-a3f5-175094426e36",
    ),
    (
        "ChatGPT (project chat)",
        "https://chatgpt.com/share/6aabc4ab-6cb0-83ee-b29b-f776183ea9a0",
    ),
]

# Fill this after you click Share in Cursor (Public) and copy the URL.
# Format looks like: https://cursor.com/s/xxxxxxxx
CURSOR_SHARE_LINK = "PASTE_CURSOR_PUBLIC_SHARE_LINK_HERE"

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".streamlit",
    "submission_pack",
    "tools",  # large binaries (cloudflared)
}
EXCLUDE_SUFFIXES = {".pyc", ".exe", ".zip"}


def pdf_safe(s: str) -> str:
    repl = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u00a0": " ",
        "→": "->",
        "←": "<-",
    }
    for a, b in repl.items():
        s = s.replace(a, b)
    return s.encode("latin-1", errors="replace").decode("latin-1")


def break_url(url: str, every: int = 48) -> str:
    u = pdf_safe(url)
    return "\n".join(u[i : i + every] for i in range(0, len(u), every))


def write_url(pdf: FPDF, url: str) -> None:
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Courier", "", 9)
    pdf.set_text_color(0, 0, 180)
    for chunk in break_url(url).split("\n"):
        pdf.set_x(pdf.l_margin)
        pdf.cell(pdf.epw, 5, chunk, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)


def build_pdf() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(pdf.epw, 9, pdf_safe("IIA Project - AI Chat History Links"))
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        pdf.epw,
        6,
        pdf_safe(
            f"Project: Identification of Uninsured Vehicles\n"
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            "This PDF lists the complete/relevant AI conversations used for the project.\n"
            "Evaluators: open each link below (must be publicly accessible)."
        ),
    )
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(pdf.epw, 8, pdf_safe("1. External AI chats (already shared)"))
    for i, (title, url) in enumerate(CHAT_LINKS, start=1):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.epw, 6, pdf_safe(f"{i}. {title}"))
        write_url(pdf, url)

    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 8, pdf_safe("2. Cursor AI chat (this project)"))
    pdf.set_font("Helvetica", "", 11)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        pdf.epw,
        6,
        pdf_safe(
            "Primary Cursor agent conversation covering ETL, warehouse dashboard, "
            "Docker/theft source, teammate push/ingest, analytics UI, PDF/CSV export, "
            "and viva demo seeding.\n\n"
            "Public share link (paste after creating Share -> Public in Cursor):"
        ),
    )
    pdf.ln(2)
    write_url(pdf, CURSOR_SHARE_LINK)
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        pdf.epw,
        5,
        pdf_safe(
            "How to make the Cursor link accessible to evaluators:\n"
            "1. Open this chat in Cursor.\n"
            "2. Click the Share icon in the chat header.\n"
            "3. Choose Public (anyone with the link).\n"
            "4. Copy the URL (cursor.com/s/...). Set CURSOR_SHARE_LINK in "
            "build_submission_pack.py and re-run, or write the link on this PDF.\n"
            "5. Also: right-click chat tab -> Export Transcript (.md) if Share is unavailable.\n\n"
            "A full local transcript is in submission_pack/Cursor_Chat_Transcript.md "
            "inside the zip for complete history."
        ),
    )

    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 8, pdf_safe("3. Codebase zip"))
    pdf.set_font("Helvetica", "", 11)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        pdf.epw,
        6,
        pdf_safe(
            f"Full project zip path:\n{str(ZIP_PATH)}\n\n"
            "Submit this zip together with this PDF and all AI share links."
        ),
    )

    pdf.output(str(PDF_PATH))
    print("Wrote", PDF_PATH)


def export_cursor_transcript_md() -> Path | None:
    """Best-effort export from local agent transcript JSONL."""
    transcripts = Path.home() / (
        ".cursor/projects/"
        "c-Users-vikra-Downloads-Identification-of-Uninsured-Vehicles-"
        "Identification-of-Uninsured-Vehicles/agent-transcripts"
    )
    if not transcripts.exists():
        # fallback shorter search
        base = Path.home() / ".cursor/projects"
        candidates = list(base.glob("**/agent-transcripts/**/*.jsonl")) if base.exists() else []
    else:
        candidates = list(transcripts.rglob("*.jsonl"))

    if not candidates:
        print("No local Cursor transcript JSONL found.")
        return None

    # Prefer the known conversation id if present
    preferred = [p for p in candidates if "1cac398b-a0f9-4230-9bb1-d41fe14717d0" in str(p)]
    src = preferred[0] if preferred else max(candidates, key=lambda p: p.stat().st_mtime)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "Cursor_Chat_Transcript.md"
    lines = [
        "# Cursor AI chat transcript (local export)",
        "",
        f"Source: `{src}`",
        f"Exported: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "This is the complete local agent transcript for the project conversation.",
        "Prefer also submitting a **Public** Cursor share link (`cursor.com/s/...`) if available.",
        "",
        "---",
        "",
    ]
    with src.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = ev.get("role") or ev.get("type") or "event"
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            text_bits = []
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_bits.append(str(part.get("text") or ""))
                    elif isinstance(part, str):
                        text_bits.append(part)
            elif isinstance(content, str):
                text_bits.append(content)
            elif isinstance(ev.get("content"), str):
                text_bits.append(ev["content"])
            body = "\n".join(t for t in text_bits if t).strip()
            if not body:
                continue
            # keep transcript readable but not endless tool dumps
            if len(body) > 12000:
                body = body[:12000] + "\n\n...[truncated]..."
            lines.append(f"## {role}")
            lines.append("")
            lines.append(body)
            lines.append("")
            lines.append("---")
            lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    print("Wrote", out, "from", src)
    return out


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    if path.name == Path(__file__).name:
        return False
    return False


def build_zip() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    count = 0
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if should_skip(path.relative_to(ROOT)):
                continue
            rel = path.relative_to(ROOT)
            zf.write(path, arcname=str(Path("Identification-of-Uninsured-Vehicles") / rel))
            count += 1
        # ensure PDF + transcript are inside zip
        if PDF_PATH.exists():
            zf.write(
                PDF_PATH,
                arcname="Identification-of-Uninsured-Vehicles/submission_pack/AI_Chat_History_Links.pdf",
            )
        md = OUT_DIR / "Cursor_Chat_Transcript.md"
        if md.exists():
            zf.write(
                md,
                arcname="Identification-of-Uninsured-Vehicles/submission_pack/Cursor_Chat_Transcript.md",
            )
    print(f"Wrote {ZIP_PATH} ({count} files)")


if __name__ == "__main__":
    export_cursor_transcript_md()
    build_pdf()
    build_zip()
    print("\nDONE")
    print("PDF:", PDF_PATH)
    print("ZIP:", ZIP_PATH)
    print(
        "\nIMPORTANT: Open this Cursor chat -> Share -> Public, copy link,\n"
        "set CURSOR_SHARE_LINK in build_submission_pack.py, re-run to update PDF."
    )
