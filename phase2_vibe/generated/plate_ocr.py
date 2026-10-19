"""
plate_ocr.py — scanned registration image → noisy plate text + context hints.

Pipeline:
  1) Prefer real OCR via pytesseract if Tesseract is installed.
  2) For our generated sample PNGs, read embedded plate metadata (scanner sidecar)
     so the viva demo works even without a system Tesseract binary.
  3) Estimate a coarse vehicle colour from image pixels for contextual matching.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_plates"


def _guess_font(size: int = 64) -> ImageFont.ImageFont:
    for name in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        p = Path(name)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


COLOR_RGB = {
    "White": (235, 235, 235),
    "Silver": (180, 180, 185),
    "Black": (35, 35, 35),
    "Red": (170, 45, 45),
    "Blue": (45, 75, 160),
    "Grey": (120, 120, 120),
}


def render_plate_image(
    plate_text: str,
    *,
    vehicle_color: str = "White",
    vehicle_type: str = "Sedan",
    out_path: Path | None = None,
    noisy_label: bool = True,
) -> Path:
    """Draw a fake ANPR crop and embed plate string in PNG metadata."""
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    w, h = 640, 280
    bg = COLOR_RGB.get(vehicle_color, (60, 60, 60))
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    # plate rectangle
    draw.rounded_rectangle((40, 70, w - 40, 190), radius=12, fill=(245, 245, 240), outline=(20, 20, 20), width=4)
    font = _guess_font(52)
    small = _guess_font(18)
    text = str(plate_text)
    # center text
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - tw) / 2, 100), text, fill=(15, 15, 15), font=font)
    caption = "ANPR SCAN" + (" · NOISY OCR DEMO" if noisy_label else "")
    draw.text((48, 24), caption, fill=(240, 240, 240) if sum(bg) < 300 else (20, 20, 20), font=small)
    draw.text((48, 220), f"color≈{vehicle_color}  type≈{vehicle_type}", fill=(240, 240, 240) if sum(bg) < 300 else (30, 30, 30), font=small)

    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:40]
    path = out_path or (SAMPLE_DIR / f"scan_{safe}.png")
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Plate", text)
    meta.add_text("VehicleColor", vehicle_color or "")
    meta.add_text("VehicleType", vehicle_type or "")
    meta.add_text("Source", "iia-anpr-sim")
    img.save(path, "PNG", pnginfo=meta)
    return path


def estimate_color_name(img: Image.Image) -> str:
    """Map average luminance/chroma to a coarse palette used in capture_log."""
    small = img.convert("RGB").resize((32, 32))
    pixels = list(small.getdata())
    if not pixels:
        return "White"
    r = sum(p[0] for p in pixels) / len(pixels)
    g = sum(p[1] for p in pixels) / len(pixels)
    b = sum(p[2] for p in pixels) / len(pixels)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx < 55:
        return "Black"
    if mn > 200:
        return "White"
    if abs(r - g) < 18 and abs(g - b) < 18:
        return "Silver" if mx > 150 else "Grey"
    if r > g + 25 and r > b + 25:
        return "Red"
    if b > r + 15 and b > g + 10:
        return "Blue"
    return "Grey"


def _ocr_tesseract(img: Image.Image) -> str | None:
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return None
    try:
        # Prefer the bright plate band
        w, h = img.size
        crop = img.crop((0, int(h * 0.2), w, int(h * 0.75)))
        raw = pytesseract.image_to_string(
            crop,
            config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- ",
        )
        cleaned = re.sub(r"[^A-Za-z0-9 -]", "", raw).strip().upper()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned or None
    except Exception:
        return None


def _read_embedded_plate(img: Image.Image) -> dict[str, str]:
    info = getattr(img, "info", {}) or {}
    out = {}
    for key in ("Plate", "VehicleColor", "VehicleType", "Source"):
        if key in info and info[key]:
            out[key] = str(info[key])
    return out


def extract_from_image(file_bytes: bytes, filename: str = "") -> dict[str, Any]:
    """
    Returns dict with plate, color, type, method, notes.
    method: embedded_metadata | tesseract | filename_hint | none
    """
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    color_est = estimate_color_name(img)
    embedded = _read_embedded_plate(img)

    if embedded.get("Plate"):
        return {
            "plate": embedded["Plate"],
            "color": embedded.get("VehicleColor") or color_est,
            "type": embedded.get("VehicleType") or "",
            "method": "embedded_metadata (ANPR sim PNG)",
            "notes": "Read plate from scanner PNG metadata; colour also estimated from pixels.",
            "image_size": img.size,
        }

    tess = _ocr_tesseract(img)
    if tess:
        return {
            "plate": tess,
            "color": color_est,
            "type": "",
            "method": "tesseract OCR",
            "notes": "Extracted with pytesseract. Install Tesseract OCR for arbitrary photos.",
            "image_size": img.size,
        }

    # Filename hint: scan_DL9CAF8B88.png
    stem = Path(filename or "").stem
    m = re.search(r"([A-Za-z]{2}[\w-]{5,})", stem)
    if m:
        return {
            "plate": m.group(1).replace("_", ""),
            "color": color_est,
            "type": "",
            "method": "filename_hint",
            "notes": "No Tesseract / metadata — guessed plate from filename. Prefer sample_plates/*.png.",
            "image_size": img.size,
        }

    return {
        "plate": "",
        "color": color_est,
        "type": "",
        "method": "none",
        "notes": "Could not read plate. Use a sample from sample_plates/ or install Tesseract + pytesseract.",
        "image_size": img.size,
    }


def generate_samples_from_manifest(limit: int = 25) -> list[Path]:
    """Build uploadable PNGs for noisy roadcapture seed rows."""
    from db_config import connect

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    # wipe old generated scans
    for p in SAMPLE_DIR.glob("scan_*.png"):
        p.unlink()

    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT noisy_plate, clean_plate, context_color, context_type
        FROM dwh_noisy_seed_manifest
        WHERE source_name='roadcapture'
        ORDER BY manifest_id DESC
        LIMIT %s
        """,
        (int(limit),),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    paths = []
    for noisy, _clean, color, vtype in rows:
        paths.append(
            render_plate_image(
                str(noisy),
                vehicle_color=str(color or "White"),
                vehicle_type=str(vtype or "Sedan"),
            )
        )
    # always include classic demo
    paths.append(
        render_plate_image("DL9CAF8B88", vehicle_color="Black", vehicle_type="SUV")
    )
    return paths
