"""
plate_ocr.py — scanned registration image -> noisy plate text + context (colour / type).

How the plate is read (first one that works wins):
  1) template-matching OCR  built in, pure PIL: find the white plate, cut it into glyphs,
                            compare each glyph with A-Z / 0-9 rendered in the plate font.
                            Reads the PIXELS, so it really "sees" 8 vs B, 0 vs O.
  2) pytesseract            for any other photo, if the Tesseract binary is installed
  3) ANPR metadata          our simulated camera also writes Plate/Color/Type into the PNG;
                            used only as a cross-check (or a last resort) and shown as such.
Colour is estimated from the image pixels; vehicle type comes from the camera metadata
(real ANPR cameras ship a vehicle-class field).
"""

from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_plates"
GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
FONT_SIZE = 52


def _guess_font(size: int = 64) -> ImageFont.ImageFont:
    for name in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        p = Path(name)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


COLOR_RGB = {
    "White": (225, 225, 225),
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
    """Draw a simulated ANPR crop (car body colour + plate) and embed camera metadata."""
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    w, h = 640, 280
    bg = COLOR_RGB.get(vehicle_color, (60, 60, 60))
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((40, 70, w - 40, 190), radius=12, fill=(250, 250, 248), outline=(20, 20, 20), width=4)
    font = _guess_font(FONT_SIZE)
    small = _guess_font(18)
    text = str(plate_text)
    # draw glyph by glyph with a fixed gap so characters never touch (clean segmentation)
    widths = [draw.textbbox((0, 0), ch, font=font)[2] - draw.textbbox((0, 0), ch, font=font)[0] for ch in text]
    gap = 6
    total = sum(widths) + gap * max(len(text) - 1, 0)
    x = (w - total) / 2
    for ch, cw in zip(text, widths):
        bx = draw.textbbox((0, 0), ch, font=font)
        draw.text((x - bx[0], 102), ch, fill=(15, 15, 15), font=font)
        x += cw + gap
    ink = (240, 240, 240) if sum(bg) < 300 else (20, 20, 20)
    draw.text((48, 24), "ANPR SCAN" + (" · NOISY OCR DEMO" if noisy_label else ""), fill=ink, font=small)
    draw.text((48, 220), f"type≈{vehicle_type}", fill=ink, font=small)

    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:40]
    path = out_path or (SAMPLE_DIR / f"scan_{safe}.png")
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Plate", text)
    meta.add_text("VehicleColor", vehicle_color or "")
    meta.add_text("VehicleType", vehicle_type or "")
    meta.add_text("Source", "iia-anpr-sim")
    img.save(path, "PNG", pnginfo=meta)
    return path


def _nearest_colour(pixels) -> str:
    if not pixels:
        return "White"
    n = len(pixels)
    r = sorted(p[0] for p in pixels)[n // 2]
    g = sorted(p[1] for p in pixels)[n // 2]
    b = sorted(p[2] for p in pixels)[n // 2]
    best, best_d = "Grey", 1e9
    for name, (cr, cg, cb) in COLOR_RGB.items():
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < best_d:
            best, best_d = name, d
    return best


def estimate_color_name(img: Image.Image) -> str:
    """
    Car body colour = median colour of a band AROUND the plate (the bumper / boot the plate is
    mounted on). Falls back to the image border when no plate is found.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    box = find_plate_box(rgb)
    if box:
        x0, y0, x1, y1 = box
        pw, ph = x1 - x0, y1 - y0
        ring = []
        for rx0, ry0, rx1, ry1 in (
            (x0 - pw // 3, y0 - ph, x1 + pw // 3, y0),        # above the plate
            (x0 - pw // 2, y0, x0, y1),                       # left
            (x1, y0, x1 + pw // 2, y1),                       # right
        ):
            rx0, ry0, rx1, ry1 = max(0, rx0), max(0, ry0), min(w, rx1), min(h, ry1)
            if rx1 - rx0 > 2 and ry1 - ry0 > 2:
                ring += list(rgb.crop((rx0, ry0, rx1, ry1)).resize((24, 8)).getdata())
        if ring:
            return _nearest_colour(ring)
    strips = [rgb.crop((0, 0, w, int(h * 0.2))), rgb.crop((0, int(h * 0.75), w, h))]
    return _nearest_colour([p for s_ in strips for p in s_.resize((40, 8)).getdata()])


# --------------------------------------------------------------------------- template OCR
_T_W, _T_H = 20, 30


def _ink_bbox(img: Image.Image, thresh: int = 110):
    g = img.convert("L")
    mask = g.point(lambda v: 255 if v < thresh else 0)
    return mask, mask.getbbox()


def _glyph_vector(mask: Image.Image) -> tuple[list[int], float]:
    bb = mask.getbbox()
    if not bb:
        return [0] * (_T_W * _T_H), 1.0
    crop = mask.crop(bb)
    aspect = crop.width / max(crop.height, 1)
    small = crop.resize((_T_W, _T_H), Image.BILINEAR)
    return [1 if v > 100 else 0 for v in small.getdata()], aspect


@lru_cache(maxsize=4)
def _templates(font_size: int = FONT_SIZE):
    font = _guess_font(font_size)
    out = {}
    for ch in GLYPHS:
        im = Image.new("L", (font_size * 2, font_size * 2), 255)
        ImageDraw.Draw(im).text((10, 10), ch, fill=0, font=font)
        mask = im.point(lambda v: 255 if v < 110 else 0)
        out[ch] = _glyph_vector(mask)
    return out


def _find_plate(img: Image.Image):
    """Largest near-white rectangle = the plate."""
    rgb = img.convert("RGB")
    mask = rgb.point(lambda v: 255 if v >= 244 else 0).convert("L")
    # all three channels must be bright
    r, g, b = rgb.split()
    m = Image.eval(r, lambda v: 255 if v >= 244 else 0)
    for ch in (g, b):
        m = Image.composite(m, Image.new("L", m.size, 0), Image.eval(ch, lambda v: 255 if v >= 244 else 0))
    del mask
    return m.getbbox()


def template_ocr_detail(img: Image.Image) -> tuple[str, list[float]]:
    """Read the plate glyph by glyph. Returns (text, per-character confidence 0..1)."""
    from PIL import ImageFilter
    bbox = _find_plate(img.filter(ImageFilter.MedianFilter(5)))
    if not bbox:
        return "", []
    x0, y0, x1, y1 = bbox
    plate = img.convert("RGB").crop((x0 + 8, y0 + 8, x1 - 8, y1 - 8))
    plate = plate.filter(ImageFilter.MedianFilter(3))          # remove dust / salt-and-pepper
    mask, ink = _ink_bbox(plate)
    if not ink:
        return "", []
    w, h = mask.size
    px = mask.load()
    cols = [sum(1 for y in range(h) if px[x, y]) >= 2 for x in range(w)]
    segs, start = [], None
    for x, on in enumerate(cols + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            if x - start >= 3:
                segs.append((start, x))
            start = None
    temps = _templates()
    text, confs = [], []
    heights = []
    for a, b in segs:
        bb = mask.crop((a, 0, b, h)).getbbox()
        heights.append((bb[3] - bb[1]) if bb else 0)
    tallest = max(heights or [0])
    for (a, b), gh in zip(segs, heights):
        if gh < 0.45 * tallest:   # '-' or '.' separator (or a speck), not a character
            continue
        vec, aspect = _glyph_vector(mask.crop((a, 0, b, h)))
        if sum(vec) < 15:
            continue
        scores = []
        for ch, (tv, ta) in temps.items():
            agree = sum(1 for p, q in zip(vec, tv) if p == q) / len(vec)
            scores.append((agree - 0.35 * abs(aspect - ta), ch))
        scores.sort(reverse=True)
        best, ch = scores[0]
        text.append(ch)
        confs.append(round(max(0.0, min(1.0, best)), 3))
    return "".join(text), confs


def template_ocr(img: Image.Image) -> tuple[str, float]:
    """Return (text, mean confidence 0..1). Works on plates drawn in the plate font."""
    text, confs = template_ocr_detail(img)
    return text, round(sum(confs) / len(confs), 3) if confs else 0.0


def degrade_image(img: Image.Image, *, blur: float = 0.0, dust: float = 0.0, smudge_char: int | None = None,
                  smudge_size: int = 40, seed: int = 7) -> Image.Image:
    """Simulate a bad camera scan: motion/focus blur, dust specks, mud over one character."""
    import random as _r
    from PIL import ImageFilter
    im = img.convert("RGB").copy()
    if smudge_char is not None:
        bbox = _find_plate(im)
        if bbox:
            x0, y0, x1, y1 = bbox
            inner = im.crop((x0 + 8, y0 + 8, x1 - 8, y1 - 8))
            mask, _ = _ink_bbox(inner)
            w, h = mask.size
            pxm = mask.load()
            cols = [any(pxm[x, y] for y in range(h)) for x in range(w)]
            segs, st_ = [], None
            for x, on in enumerate(cols + [False]):
                if on and st_ is None:
                    st_ = x
                elif not on and st_ is not None:
                    segs.append((st_, x))
                    st_ = None
            if 0 <= smudge_char < len(segs):
                a, b = segs[smudge_char]
                cx = x0 + 8 + (a + b) // 2
                cy = (y0 + y1) // 2 + 6
                ImageDraw.Draw(im).ellipse((cx - smudge_size // 2, cy - smudge_size // 2 + 8,
                                            cx + smudge_size // 2, cy + smudge_size // 2 + 8),
                                           fill=(248, 248, 244))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    if dust:
        rnd = _r.Random(seed)
        px = im.load()
        w, h = im.size
        for _ in range(int(w * h * dust)):
            x, y = rnd.randrange(w), rnd.randrange(h)
            v = rnd.choice([10, 245])
            px[x, y] = (v, v, v)
    return im


def find_plate_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """
    Locate the number plate in a photo: a connected bright (white / yellow) region with a
    plate-like shape (width 1.5-7x height) that has dark characters inside it.
    Returns a padded box in pixels, or None.
    """
    rgb = img.convert("RGB")
    W, H = rgb.size
    scale = 320 / max(W, H)
    small = rgb.resize((max(1, int(W * scale)), max(1, int(H * scale))))
    w, h = small.size
    px = small.load()
    lum = [[sum(px[x, y]) / 3 for x in range(w)] for y in range(h)]
    cands = []
    for thresh in (235, 205, 175):
        on = [[False] * w for _ in range(h)]
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y]
                white = min(r, g, b) >= thresh and max(r, g, b) - min(r, g, b) < 40
                yellow = r >= thresh and g >= thresh - 30 and b < 120
                on[y][x] = white or yellow
        seen = [[False] * w for _ in range(h)]
        for y0 in range(h):
            for x0 in range(w):
                if not on[y0][x0] or seen[y0][x0]:
                    continue
                stack, pts = [(x0, y0)], 0
                seen[y0][x0] = True
                minx = maxx = x0
                miny = maxy = y0
                while stack:
                    x, y = stack.pop()
                    pts += 1
                    minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < w and 0 <= ny < h and on[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
                bw, bh = maxx - minx + 1, maxy - miny + 1
                if bh < 5 or bw < 15 or not (1.5 <= bw / bh <= 7) or pts / (bw * bh) < 0.40:
                    continue
                # characters: dark pixels inside the region's box (not on its edge)
                ix0, ix1 = minx + bw // 10, maxx - bw // 10
                iy0, iy1 = miny + bh // 6, maxy - bh // 6
                inner = [lum[y][x] for y in range(iy0, iy1 + 1) for x in range(ix0, ix1 + 1)]
                dark = sum(1 for v in inner if v < 100) / max(len(inner), 1)
                if not (0.04 <= dark <= 0.6):
                    continue
                # text test: a line through the middle crosses many dark/bright edges
                trans = []
                for fy in (0.4, 0.5, 0.6):
                    yy = miny + int(bh * fy)
                    row = [lum[yy][x] < 100 for x in range(ix0, ix1 + 1)]
                    trans.append(sum(1 for i in range(1, len(row)) if row[i] != row[i - 1]))
                if sum(trans) / 3 < 8:
                    continue
                cands.append((pts, minx, miny, maxx, maxy))
    if not cands:
        return None
    _, x0, y0, x1, y1 = max(cands)
    pad_x, pad_y = (x1 - x0) * 0.06, (y1 - y0) * 0.15
    return (max(0, int((x0 - pad_x) / scale)), max(0, int((y0 - pad_y) / scale)),
            min(W, int((x1 + 1 + pad_x) / scale)), min(H, int((y1 + 1 + pad_y) / scale)))


def crop_rotate(img: Image.Image, box_pct: tuple[float, float, float, float] = (0, 0, 100, 100),
                angle: float = 0.0) -> Image.Image:
    """Rotate (degrees, counter-clockwise) then crop to a box given in % of width/height."""
    im = img.convert("RGB")
    if angle:
        im = im.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(128, 128, 128))
    W, H = im.size
    l, t, r, b = box_pct
    l, r = sorted((l, r))
    t, b = sorted((t, b))
    box = (int(W * l / 100), int(H * t / 100), max(int(W * r / 100), int(W * l / 100) + 2),
           max(int(H * b / 100), int(H * t / 100) + 2))
    return im.crop(box)


def draw_box_preview(img: Image.Image, box_pct, angle: float = 0.0) -> Image.Image:
    """Original (rotated) image with the crop rectangle drawn on it."""
    im = img.convert("RGB")
    if angle:
        im = im.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(128, 128, 128))
    W, H = im.size
    l, t, r, b = box_pct
    d = ImageDraw.Draw(im)
    lw = max(2, W // 200)
    d.rectangle((W * min(l, r) / 100, H * min(t, b) / 100, W * max(l, r) / 100, H * max(t, b) / 100),
                outline=(255, 40, 40), width=lw)
    return im


def image_to_png_bytes(img: Image.Image, meta: dict | None = None) -> bytes:
    info = PngImagePlugin.PngInfo()
    for k, v in (meta or {}).items():
        if v:
            info.add_text(k, str(v))
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
    str(Path.home() / "AppData" / "Local" / "Tesseract-OCR" / "tesseract.exe"),
]


def tesseract_status() -> str:
    """'ready (5.x at ...)' or why not — shown in the app and in phase2_check."""
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return "pytesseract not installed (pip install pytesseract)"
    import shutil
    if not shutil.which(pytesseract.pytesseract.tesseract_cmd):
        for c in TESSERACT_CANDIDATES:  # installer default folders; no PATH edit needed
            if Path(c).exists():
                pytesseract.pytesseract.tesseract_cmd = c
                break
    try:
        return f"ready (Tesseract {pytesseract.get_tesseract_version()} at {pytesseract.pytesseract.tesseract_cmd})"
    except Exception:
        return "Tesseract program not found (run tools/tesseract-ocr-w64-setup-*.exe)"


def _prep_for_tesseract(img: Image.Image) -> Image.Image:
    """Standard Tesseract preparation: plate only, grey, ~120 px tall, binarised, white border."""
    from PIL import ImageOps
    box = find_plate_box(img)
    im = img.convert("L") if box is None else img.convert("L").crop(box)
    if im.height < 120:
        im = im.resize((int(im.width * 120 / max(im.height, 1)), 120), Image.LANCZOS)
    hist = im.histogram()
    total = sum(hist)
    # Otsu threshold
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = s_b = 0
    best_t, best_v = 128, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0 or w_b == total:
            continue
        s_b += t * hist[t]
        m_b, m_f = s_b / w_b, (sum_all - s_b) / (total - w_b)
        v = w_b * (total - w_b) * (m_b - m_f) ** 2
        if v > best_v:
            best_v, best_t = v, t
    bw = im.point(lambda v: 255 if v > best_t else 0)
    return ImageOps.expand(bw, border=25, fill=255)


def _ocr_tesseract(img: Image.Image) -> str | None:
    if not tesseract_status().startswith("ready"):
        return None
    import pytesseract  # type: ignore
    try:
        prepared = _prep_for_tesseract(img)
        raw = pytesseract.image_to_string(
            prepared, config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        cleaned = re.sub(r"[^A-Z0-9]", "", raw.upper())
        return cleaned or None
    except Exception:
        return None


def _read_embedded(img: Image.Image) -> dict[str, str]:
    info = getattr(img, "info", {}) or {}
    return {k: str(info[k]) for k in ("Plate", "VehicleColor", "VehicleType", "Source") if info.get(k)}


def extract_from_image(file_bytes: bytes, filename: str = "") -> dict[str, Any]:
    """Returns plate, color, type, method, confidence, metadata cross-check, notes."""
    raw = Image.open(io.BytesIO(file_bytes))
    meta = _read_embedded(raw)
    img = raw.convert("RGB")
    color = estimate_color_name(img)
    vtype = meta.get("VehicleType", "")

    plate, method, conf = "", "none", 0.0
    t, char_conf = template_ocr_detail(img)
    c = round(sum(char_conf) / len(char_conf), 3) if char_conf else 0.0
    if t and c >= 0.90:          # our ANPR-style crops: deterministic pixel read
        plate, method, conf = t, "template-matching OCR (pixels)", c
    else:                        # any other photo: real OCR engine if installed
        tess = _ocr_tesseract(img)
        if tess:
            plate, method, conf = tess, "tesseract OCR (pixels)", 0.9
    meta_plate = meta.get("Plate", "")
    if not plate and meta_plate:
        plate, method, conf = meta_plate, "ANPR camera metadata (fallback)", 1.0
    if not plate:
        m = re.search(r"([A-Za-z]{2}[\w-]{5,})", Path(filename or "").stem)
        if m:
            plate, method, conf = m.group(1).replace("_", "").upper(), "filename hint (fallback)", 0.3

    check = ""
    if meta_plate and plate:
        check = ("agrees with camera metadata" if re.sub(r"[^A-Z0-9]", "", meta_plate.upper()) == plate
                 else f"camera metadata says {meta_plate}")
    return {
        "plate": plate,
        "color": color,
        "type": vtype,
        "method": method,
        "confidence": conf,
        "metadata_check": check,
        "char_conf": char_conf if method.startswith("template") else [],
        "notes": f"{method}; colour estimated from pixels ({color})"
                 + (f"; {check}" if check else ""),
        "image_size": img.size,
    }


@lru_cache(maxsize=512)
def _cached_extract(path: str, mtime: float) -> dict:
    return extract_from_image(Path(path).read_bytes(), Path(path).name)


def scan_image_folder(folder: Path | None = None) -> list[dict]:
    """OCR every scan in sample_plates/ (cached) — used by the vice-versa search."""
    folder = folder or SAMPLE_DIR
    out = []
    for p in sorted(Path(folder).glob("*.png")) + sorted(Path(folder).glob("*.jpg")):
        try:
            r = dict(_cached_extract(str(p), p.stat().st_mtime))
            r["file"] = p.name
            out.append(r)
        except Exception:
            continue
    return out


def generate_samples_from_manifest(limit: int = 25) -> list[Path]:
    """Build uploadable PNGs for the noisy capture rows (+ the fixed demo scans)."""
    from db_config import connect

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for p in SAMPLE_DIR.glob("scan_*.png"):
        p.unlink()

    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT noisy_plate, context_color, context_type
        FROM dwh_noisy_seed_manifest
        WHERE source_name='roadcapture' AND noise_kind NOT LIKE 'context%%' AND noise_kind <> 'support_sighting'
        ORDER BY manifest_id
        LIMIT %s
        """,
        (int(limit),),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    paths = [render_plate_image(str(n), vehicle_color=str(c or "White"), vehicle_type=str(t or "Sedan"))
             for n, c, t in rows]
    for plate, col, typ in DEMO_SCANS:
        paths.append(render_plate_image(plate, vehicle_color=col, vehicle_type=typ))
    return paths


# Fixed demo scans (see populate_noisy_data.DEMO): OCR slip + a smudged/dropped digit
DEMO_SCANS = [
    ("DL9CAF8B88", "Black", "SUV"),   # 8 read as B          -> DL9CAF8888
    ("DL9CAF888", "Black", "SUV"),    # one digit lost       -> 3 equally close plates; context decides
]
