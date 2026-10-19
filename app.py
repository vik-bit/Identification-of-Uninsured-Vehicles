"""
app.py — Warehouse-only dashboard for vehicle_dwh.

Run:  streamlit run app.py
"""

from __future__ import annotations

import difflib
import html
import io
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import altair as alt
from fpdf import FPDF

from db_config import connect
from entity_matching import (
    LOOKUP_META,
    WEIGHTS_META,
    W_FMT,
    W_JW,
    W_LEV,
    W_OCR,
    align,
    anomaly_summary,
    cleanse_all,
    cleanse_noisy_record,
    detect_anomalies,
    ensure_cleanse_log,
    find_approximate_matches,
    find_records_near_plate,
    invalidate_cache,
    records_for_plates,
    score_breakdown,
)
from evaluate_matching import RESULT_JSON as BENCH_JSON, run as run_benchmark
from plate_ocr import (
    SAMPLE_DIR,
    crop_rotate,
    draw_box_preview,
    estimate_color_name,
    find_plate_box,
    tesseract_status,
    degrade_image,
    extract_from_image,
    generate_samples_from_manifest,
    image_to_png_bytes,
)
from warehouse_ops import read_summary


@st.cache_data(ttl=15)
def load_noisy_manifest(limit: int = 200) -> pd.DataFrame:
    try:
        return wh_query(
            """
            SELECT manifest_id, clean_plate, noisy_plate, noise_kind, source_name,
                   context_color, context_type, context_model, note, created_at
            FROM dwh_noisy_seed_manifest
            ORDER BY manifest_id DESC
            LIMIT %s
            """,
            (int(limit),),
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=15)
def load_cleanse_log(limit: int = 50) -> pd.DataFrame:
    try:
        conn = connect(WAREHOUSE)
        cur = conn.cursor()
        ensure_cleanse_log(cur)  # adds the old->new columns to an older table
        conn.commit()
        cur.close()
        conn.close()
        return wh_query(
            """
            SELECT cleanse_id, cleansed_at, source_name, record_ref, field_name,
                   old_value, new_value, chosen_plate AS vehicle, match_score, action, note, context_json
            FROM dwh_cleanse_log
            ORDER BY cleanse_id DESC
            LIMIT %s
            """,
            (int(limit),),
        )
    except Exception:
        return pd.DataFrame()

st.set_page_config(
    page_title="Vehicle warehouse",
    layout="wide",
    initial_sidebar_state="collapsed",
)

WAREHOUSE = "vehicle_dwh"
SQL_ALLOW = ("select", "show", "describe", "desc", "explain")

SEVERITY = {
    "Stolen / shredded": 0,
    "No insurance on file": 1,
    "Insurance expired": 2,
    "Clear": 3,
}

st.markdown(
    """
    <style>
      .block-container { padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1400px; }
      div[data-testid="stMetric"] {
        background: #f6f5f2;
        border: 1px solid #e6e4de;
        border-radius: 10px;
        padding: 12px 14px;
      }
      div[data-testid="stMetric"] label { color: #5f5e5a !important; }
      div[data-testid="stHeadingWithActionElements"],
      div[data-testid="stHeadingWithActionElements"] h1,
      div[data-testid="stHeadingWithActionElements"] h2,
      div[data-testid="stHeadingWithActionElements"] h3 {
        overflow: visible !important;
        white-space: normal !important;
        writing-mode: horizontal-tb !important;
        text-orientation: mixed !important;
        line-height: 1.4 !important;
      }
      .section-title {
        font-size: 1.15rem;
        font-weight: 600;
        margin: 0 0 0.75rem 0;
        line-height: 1.4;
        white-space: normal;
      }
      .section-gap { height: 1.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def normalize_plate(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def bucket(status: str) -> str:
    s = str(status or "")
    if s.startswith("OK"):
        return "Clear"
    if "EXPIRED" in s:
        return "Insurance expired"
    if "STOLEN" in s or "SHREDDED" in s:
        return "Stolen / shredded"
    return "No insurance on file"


def wh_query(sql: str, params=None) -> pd.DataFrame:
    conn = connect(WAREHOUSE)
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return pd.DataFrame(rows, columns=cols)


@st.cache_data(ttl=20)
def load_summary() -> dict[str, int]:
    try:
        return read_summary()
    except Exception:
        return {"OK": 0, "EXPIRED": 0, "STOLEN": 0, "NO INSURANCE": 0}


@st.cache_data(ttl=20)
def load_records(limit: int = 5000) -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM vehicle_360 ORDER BY last_seen_time DESC LIMIT %s",
        (int(limit),),
    )


@st.cache_data(ttl=20)
def load_priority() -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM ministry_priority ORDER BY severity_rank, last_seen_time DESC"
    )


@st.cache_data(ttl=20)
def load_state_risk() -> pd.DataFrame:
    return wh_query("SELECT * FROM ministry_state_risk ORDER BY risk_score DESC")


@st.cache_data(ttl=20)
def load_hotspots() -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM ministry_hotspots ORDER BY flagged_sightings DESC"
    )


@st.cache_data(ttl=20)
def load_insurer_gap() -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM ministry_insurer_gap "
        "ORDER BY (expired_flagged + still_listed_but_stolen) DESC"
    )


@st.cache_data(ttl=20)
def load_xref(plate: str) -> pd.DataFrame:
    return wh_query(
        "SELECT source_name, native_plate, source_pk "
        "FROM dwh_plate_xref WHERE plate_canonical = %s ORDER BY source_name",
        (plate,),
    )


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def pdf_safe(text: object, max_len: int | None = None) -> str:
    if text is None or (isinstance(text, float) and pd.isna(text)):
        s = ""
    else:
        try:
            s = "" if pd.isna(text) else str(text)
        except (ValueError, TypeError):
            s = str(text)
    for a, b in {
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
        "≈": "~=",
        "·": "-",
    }.items():
        s = s.replace(a, b)
    s = s.encode("latin-1", errors="replace").decode("latin-1")
    return s[:max_len] if max_len is not None else s


def df_to_pdf_bytes(df: pd.DataFrame, title: str) -> bytes:
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, pdf_safe(title, 80), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(
        0,
        6,
        pdf_safe(
            f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  rows={len(df)}"
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)
    if df.empty:
        pdf.cell(0, 8, "No rows.", new_x="LMARGIN", new_y="NEXT")
        return bytes(pdf.output())

    view = df.head(200).copy()
    cols = list(view.columns)
    col_w = max(18, 277 / max(len(cols), 1))
    pdf.set_font("Helvetica", "B", 7)
    for c in cols:
        pdf.cell(col_w, 6, pdf_safe(c, 18), border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 7)
    for _, row in view.iterrows():
        for c in cols:
            val = "" if pd.isna(row[c]) else row[c]
            pdf.cell(col_w, 5, pdf_safe(val, 22), border=1)
        pdf.ln()
    if len(df) > 200:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(
            0,
            6,
            pdf_safe(
                f"(Showing first 200 of {len(df)} rows - use CSV for full export.)"
            ),
        )
    return bytes(pdf.output())


def export_buttons(df: pd.DataFrame, stem: str, key: str) -> None:
    c1, c2, _ = st.columns([1, 1, 6])
    with c1:
        st.download_button(
            "CSV",
            data=df_to_csv_bytes(df),
            file_name=f"{stem}.csv",
            mime="text/csv",
            key=f"csv_{key}",
            disabled=df.empty,
            use_container_width=True,
        )
    with c2:
        st.download_button(
            "PDF",
            data=df_to_pdf_bytes(df, stem.replace("_", " ").title()),
            file_name=f"{stem}.pdf",
            mime="application/pdf",
            key=f"pdf_{key}",
            disabled=df.empty,
            use_container_width=True,
        )


def run_warehouse_sql(sql: str) -> pd.DataFrame:
    stripped = sql.strip().rstrip(";")
    cleaned = re.sub(r"(?s)^(?:\s|--[^\n]*\n|/\*.*?\*/)*", "", stripped)
    first = (cleaned.split(None, 1)[0].lower() if cleaned else "")
    if first not in SQL_ALLOW:
        raise ValueError(
            f"Only {SQL_ALLOW} are allowed against vehicle_dwh "
            f"(got {first!r})."
        )
    return wh_query(stripped)


def chart_bars(
    df: pd.DataFrame,
    category: str,
    value: str | list[str],
    *,
    height: int = 320,
    top_n: int | None = None,
) -> None:
    """Bar chart with category labels kept horizontal (Altair labelAngle=0)."""
    if df.empty:
        st.info("No data.")
        return
    data = df.copy()
    values = [value] if isinstance(value, str) else list(value)
    if top_n:
        sort_col = values[0]
        data = data.nlargest(top_n, sort_col)

    if len(values) == 1:
        plot = data[[category, values[0]]].rename(columns={values[0]: "value"})
        chart = (
            alt.Chart(plot)
            .mark_bar()
            .encode(
                x=alt.X(
                    f"{category}:N",
                    sort="-y",
                    title=None,
                    axis=alt.Axis(labelAngle=0, labelLimit=160, labelOverlap=False),
                ),
                y=alt.Y("value:Q", title=values[0]),
                tooltip=[category, "value"],
            )
            .properties(height=height)
        )
    else:
        long = data.melt(
            id_vars=[category],
            value_vars=values,
            var_name="metric",
            value_name="value",
        )
        chart = (
            alt.Chart(long)
            .mark_bar()
            .encode(
                x=alt.X(
                    f"{category}:N",
                    sort="-y",
                    title=None,
                    axis=alt.Axis(labelAngle=0, labelLimit=160, labelOverlap=False),
                ),
                y=alt.Y("value:Q", title=None),
                color=alt.Color("metric:N", title=None),
                xOffset="metric:N",
                tooltip=[category, "metric", "value"],
            )
            .properties(height=height)
        )
    st.altair_chart(chart, use_container_width=True)


def ensure_feedback_table() -> None:
    conn = connect(WAREHOUSE)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_feedback (
            feedback_id   INT PRIMARY KEY AUTO_INCREMENT,
            submitted_at  DATETIME NOT NULL,
            name          VARCHAR(80) NOT NULL,
            role          VARCHAR(40) NOT NULL,
            rating        INT NOT NULL,
            category      VARCHAR(40) NOT NULL,
            message       VARCHAR(1000) NOT NULL,
            contact_email VARCHAR(120) NULL
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def save_feedback(
    name: str,
    role: str,
    rating: int,
    category: str,
    message: str,
    contact_email: str = "",
) -> int:
    ensure_feedback_table()
    conn = connect(WAREHOUSE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ui_feedback
          (submitted_at, name, role, rating, category, message, contact_email)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            datetime.now(),
            name[:80],
            role[:40],
            int(rating),
            category[:40],
            message[:1000],
            (contact_email or "")[:120] or None,
        ),
    )
    fid = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return int(fid)


def load_recent_feedback(limit: int = 20) -> pd.DataFrame:
    ensure_feedback_table()
    return wh_query(
        "SELECT feedback_id, submitted_at, name, role, rating, category, message "
        "FROM ui_feedback ORDER BY feedback_id DESC LIMIT %s",
        (int(limit),),
    )


def _small_headings(md: str) -> str:
    """Render a markdown file inside a column without page-sized headings."""
    return re.sub(r"(?m)^(#{1,3}) ", lambda m: "#" * (len(m.group(1)) + 3) + " ", md)


# --------------------------------------------------------------------------- Phase-2 visual helpers
_OP_STYLE = {
    "match": ("#d7f2dd", "#14532d"),
    "lookalike": ("#fde9b8", "#7a4b00"),
    "unreadable": ("#d6e6fb", "#123a6b"),
    "different": ("#f9d2d2", "#7f1d1d"),
    "missing": ("#e5e5e5", "#444444"),
    "extra": ("#e5e5e5", "#444444"),
}
_CHIP = ("display:inline-block;min-width:1.55em;text-align:center;margin:1px;padding:2px 0;"
         "border-radius:5px;font:600 15px/1.3 Consolas,monospace;")


def _chip(ch: str, op: str, title: str = "") -> str:
    bg, fg = _OP_STYLE[op]
    return f"<span title='{html.escape(title)}' style='{_CHIP}background:{bg};color:{fg}'>{html.escape(ch or '·')}</span>"


def align_html(scan: str, hit_or_plate, label: str | None = None) -> str:
    """Two rows of chips: the scanned plate over the candidate, coloured by edit type."""
    if isinstance(hit_or_plate, str):
        cand, extra = hit_or_plate, ""
    else:
        h = hit_or_plate
        cand = h.plate_canonical
        extra = (f" &nbsp;<b>{h.score:.3f}</b> &nbsp;<span style='opacity:.75'>{html.escape(h.make_model or '')} "
                 f"{html.escape(h.vehicle_color or '')} {html.escape(h.vehicle_type or '')}"
                 f"{' · ' + html.escape(', '.join(h.context_reasons)) if h.context_reasons else ''}</span>")
    ops = align(scan, cand)
    top = "".join(_chip(a, op, op) for a, b, op in ops)
    bot = "".join(_chip(b, op, op) for a, b, op in ops)
    head = html.escape(label) if label else f"<b>{html.escape(cand)}</b>"
    return (f"<div style='margin:6px 0 10px'><div style='font-size:13px;margin-bottom:2px'>{head}{extra}</div>"
            f"<div><span style='display:inline-block;width:4.2em;font-size:12px;opacity:.7'>scan</span>{top}</div>"
            f"<div><span style='display:inline-block;width:4.2em;font-size:12px;opacity:.7'>record</span>{bot}</div></div>")


ALIGN_LEGEND = (
    "<div style='font-size:12px;opacity:.85;margin-top:-4px'>"
    + " ".join(_chip(sample, op) + f" {name} &nbsp;" for op, name, sample in
               [("match", "same", "D"), ("lookalike", "OCR look-alike (cheap)", "B"),
                ("unreadable", "unreadable", "?"), ("different", "different", "3"),
                ("missing", "missing / extra", "·")])
    + "</div>"
)


def ocr_chips_html(plate: str, confs: list[float]) -> str:
    """The raw OCR read with a confidence under every character."""
    if not plate:
        return ""
    if not confs or len(confs) != len(plate):
        return "<div>" + "".join(_chip(c, "match") for c in plate) + "</div>"
    cells = []
    for ch, c in zip(plate, confs):
        op = "match" if c >= 0.85 else "lookalike" if c >= 0.60 else "different"
        cells.append(f"<span style='display:inline-block;text-align:center;margin-right:1px'>{_chip(ch, op)}"
                     f"<br><span style='font-size:10px;opacity:.75'>{c:.2f}</span></span>")
    return ("<div style='margin:4px 0 8px'><span style='font-size:12px;opacity:.7'>OCR read · confidence per "
            "character (red &lt; 0.60 → treated as ?)</span><br>" + "".join(cells) + "</div>")


def progress_strip_html(flags: dict) -> str:
    steps = [("image", "Image read"), ("match", "Matches found"), ("vice", "Vice-versa"),
             ("cleanse", "Record cleansed"), ("zero", "0 anomalies"), ("bench", "Accuracy shown")]
    out = []
    for key, name in steps:
        ok = flags.get(key)
        bg, fg = ("#d7f2dd", "#14532d") if ok else ("#ececec", "#555555")
        out.append(f"<span style='display:inline-block;margin:0 6px 6px 0;padding:3px 10px;border-radius:12px;"
                   f"font-size:13px;background:{bg};color:{fg}'>{'✔' if ok else '○'} {name}</span>")
    return "<div style='margin:-4px 0 6px'>" + "".join(out) + "</div>"


def _auto_crop() -> None:
    """Button callback: find the plate in the current image and set the crop sliders to it."""
    data = st.session_state.get("crop_src")
    if not data:
        return
    from PIL import Image as _PILImage
    im = _PILImage.open(io.BytesIO(data)).convert("RGB")
    rot = st.session_state.get("crop_rot", 0)
    if rot:
        im = im.rotate(rot, expand=True, fillcolor=(128, 128, 128))
    box = find_plate_box(im)
    if box is None:
        st.session_state["crop_msg"] = "No plate-like region found — crop with the sliders."
        return
    W, H = im.size
    st.session_state["crop_x"] = (int(box[0] * 100 / W), min(100, int(box[2] * 100 / W + 0.999)))
    st.session_state["crop_y"] = (int(box[1] * 100 / H), min(100, int(box[3] * 100 / H + 0.999)))
    st.session_state["crop_mode"] = "Sliders"
    st.session_state["crop_msg"] = ""


# --------------------------------------------------------------------------- UI
st.title("Vehicle warehouse")

summary = load_summary()
total = sum(summary.values())
flagged_n = total - summary.get("OK", 0)

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Vehicles", f"{total:,}")
k2.metric("Clear", f"{summary.get('OK', 0):,}")
k3.metric("Flagged", f"{flagged_n:,}")
k4.metric("Stolen", f"{summary.get('STOLEN', 0):,}")
k5.metric("Uninsured / expired", f"{summary.get('NO INSURANCE', 0) + summary.get('EXPIRED', 0):,}")

st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)

tab_howto, tab_overview, tab_records, tab_match, tab_analytics, tab_feedback = st.tabs(
    ["How to use", "Overview", "Records", "Entity match", "Analytics", "Feedback"]
)

# ---- How to use ----
with tab_howto:
    st.subheader("How to use this dashboard")
    st.markdown(
        """
This dashboard shows **integrated vehicle status** from the warehouse
(road captures, registration, insurance, and theft reports combined into one view).

The numbers at the top are a quick snapshot of the whole fleet.

#### Overview
- **Status mix** — how many vehicles are clear, uninsured, expired, or stolen/shredded.
- **Priority queue** — vehicles that need attention first, with a suggested action.
- Use **CSV** / **PDF** under a table to download what you see.

#### Records
1. Enter a **plate number** (any common format is fine) to find a vehicle.
2. Optionally filter by **owner / location / model** and **status**.
3. Browse the integrated record list (owner, insurer, last seen, status, etc.).
4. Need a custom view? Use the **SQL** box at the bottom for simple read-only queries
   (for example: show the first 50 vehicles). Ask an admin if you are unsure.
5. Download results with **CSV** or **PDF**.

#### Entity match (Phase 2 — noisy plates)
1. **① Match** — pick a sample scan (or upload one) → **Read plate from image**.
   The plate is read from the pixels; colour comes from the car body, type from the camera.
   *Simulate a bad scan* adds blur / dust / mud so you can watch the OCR misread a clean plate;
   characters read with low confidence become `?` (unknown) instead of a wrong letter.
2. Check / edit plate, colour, type (and model if known) → **Find matches**.
   Every vehicle that could be meant is listed with its score and *why* (OCR distance, context).
   Switch the direction to *Recorded vehicle → noisy records & scans* for the vice-versa search.
3. **② Cleanse** — choose the correct vehicle → **Cleanse noisy record**, or
   **Scan for anomalies** → **Cleanse all** to clean every source; it ends with "0 anomalies left".
4. **③ Accuracy & why** — the benchmark that justifies the algorithm.
5. **④ Prompt & code changes** — prompts, generated code and our changes.

#### Analytics
- **State risk** — which state codes look riskiest.
- **Hotspot locations** — where flagged vehicles were last seen most often.
- **Insurer gaps** — insurers linked to expired cover or stolen-but-still-listed cases.

Scroll the page to see each section. Charts and tables can be exported the same way.

#### Feedback
Use the **Feedback** tab to rate the tool or report a problem.
Include your name and a short message so the team can follow up.

#### Tips
- If numbers look empty or outdated, ask the warehouse operator to refresh the data,
  then reload this page.
- Plate formats differ across systems; searching with or without dashes/spaces usually works.
- Status meanings:
  - **Clear** — insured and not flagged as stolen
  - **No insurance on file** — no matching policy found
  - **Insurance expired** — policy lapsed
  - **Stolen / shredded** — reported stolen or shredded in theft records
        """
    )

# ---- Overview ----
with tab_overview:
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Status mix")
        chart = pd.DataFrame(
            [{"bucket": k, "vehicles": v} for k, v in summary.items()]
        )
        if chart.empty:
            st.info("No summary yet. Run ETL.")
        else:
            chart_bars(chart, "bucket", "vehicles", height=360)
    with right:
        st.subheader("Priority queue")
        pri = load_priority().head(20)
        if pri.empty:
            st.info("Priority queue empty. Run ETL.")
        else:
            st.dataframe(
                pri[
                    [
                        "severity_rank",
                        "plate_canonical",
                        "status",
                        "last_seen_location",
                        "action_hint",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                height=360,
            )
            export_buttons(pri, "priority_queue", "overview_pri")

# ---- Records (was Fleet) ----
with tab_records:
    st.subheader("Integrated records")

    c1, c2, c3 = st.columns([2, 2, 1], gap="medium")
    with c1:
        plate_in = st.text_input(
            "Plate number",
            placeholder="DL3CAB1234 / DL-3-CAB-1234",
            key="records_plate",
        )
    with c2:
        search_q = st.text_input(
            "Owner / location / model",
            key="records_search",
        )
    with c3:
        limit = st.number_input("Row limit", min_value=100, max_value=20000, value=5000, step=500)

    records = load_records(int(limit))
    view = records.copy()
    if not view.empty:
        view["category"] = view["status"].astype(str).apply(bucket)
        cats = sorted(view["category"].unique(), key=lambda c: SEVERITY.get(c, 9))
        chosen = st.multiselect("Status", cats, default=cats, key="records_cats")
        view = view[view["category"].isin(chosen)]

        if plate_in.strip():
            plate_n = normalize_plate(plate_in)
            view = view[
                view["plate_canonical"].astype(str).str.contains(plate_n, case=False, na=False)
            ]
            xref = load_xref(plate_n)
            if not xref.empty:
                st.dataframe(xref, use_container_width=True, hide_index=True)

        if search_q.strip():
            needle = search_q.strip().lower()
            view = view[
                view["owner_name"].astype(str).str.lower().str.contains(needle, na=False)
                | view["last_seen_location"].astype(str).str.lower().str.contains(needle, na=False)
                | view["make_model"].astype(str).str.lower().str.contains(needle, na=False)
            ]

    st.dataframe(view, use_container_width=True, hide_index=True, height=460)
    export_buttons(view, "records_vehicle_360", "records")

    st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
    st.subheader("SQL (vehicle_dwh only)")
    sql = st.text_area(
        "Query",
        value="SELECT plate_canonical, status, last_seen_location, last_seen_time\n"
        "FROM vehicle_360\nORDER BY last_seen_time DESC\nLIMIT 50;",
        height=120,
        key="records_sql",
        label_visibility="collapsed",
    )
    if st.button("Run SQL", type="primary", key="btn_records_sql"):
        try:
            out = run_warehouse_sql(sql)
            st.success(f"{len(out):,} row(s)")
            st.dataframe(out, use_container_width=True, hide_index=True, height=360)
            export_buttons(out, "sql_result", "records_sql_out")
        except Exception as e:
            st.error(str(e))

# ---- Entity match (Phase 2: noisy plates) ----
with tab_match:
    st.subheader("Entity matching & cleansing under noisy plates")
    flags = st.session_state.setdefault("p2_flags", {})
    strip_ph = st.empty()  # filled at the end, after the buttons below have run
    m_match, m_clean, m_acc, m_vibe = st.tabs(
        ["① Match", "② Cleanse", "③ Accuracy & why", "④ Prompt & code changes"]
    )

    # ======================================================== ① MATCH
    with m_match:
        left, right = st.columns([1, 1.15], gap="large")
        with left:
            st.markdown("##### Step 1 · Scanned image")
            samples = sorted(SAMPLE_DIR.glob("scan_*.png")) if SAMPLE_DIR.exists() else []
            src = st.radio("Image source", ["Sample scan", "Upload a photo"], horizontal=True, key="img_src")
            img_bytes, img_name = None, ""
            if src == "Sample scan":
                if samples:
                    names = [p.name for p in samples]
                    default = names.index("scan_DL9CAF8B88.png") if "scan_DL9CAF8B88.png" in names else 0
                    pick = st.selectbox("sample_plates/", names, index=default, key="img_pick")
                    img_bytes, img_name = (SAMPLE_DIR / pick).read_bytes(), pick
                else:
                    st.info("No sample scans yet — press **Re-generate sample scans**.")
            else:
                up = st.file_uploader("Plate photo (PNG / JPG)", type=["png", "jpg", "jpeg", "webp"], key="match_upload")
                st.caption(f"Real-photo OCR: {tesseract_status()}")
                if up is not None:
                    img_bytes, img_name = up.getvalue(), up.name

            # ---- crop / straighten -------------------------------------------------
            for _k, _v in (("crop_x", (0, 100)), ("crop_y", (0, 100)), ("crop_rot", 0)):
                st.session_state.setdefault(_k, _v)
            if img_bytes and st.session_state.get("crop_for") != img_name:
                st.session_state.update(crop_for=img_name, crop_x=(0, 100), crop_y=(0, 100), crop_rot=0)
            st.session_state["crop_src"] = img_bytes
            with st.expander("Crop / straighten the image (recommended for phone photos)",
                             expanded=src == "Upload a photo"):
                cropper_ok = False
                try:
                    from streamlit_cropper import st_cropper  # optional: pip install streamlit-cropper
                    cropper_ok = True
                except ImportError:
                    pass
                crop_mode = st.radio("Crop with", ["Sliders"] + (["Drag a box"] if cropper_ok else []),
                                     horizontal=True, key="crop_mode", label_visibility="collapsed") \
                    if cropper_ok else "Sliders"
                b_auto, b_reset = st.columns(2)
                b_auto.button("Auto-find plate", key="btn_autocrop", on_click=_auto_crop,
                              use_container_width=True, disabled=not img_bytes,
                              help="Finds the bright rectangle with characters in it and crops to it")
                b_reset.button("Reset crop", key="btn_resetcrop", use_container_width=True,
                               on_click=lambda: st.session_state.update(crop_x=(0, 100), crop_y=(0, 100), crop_rot=0))
                if st.session_state.get("crop_msg"):
                    st.warning(st.session_state["crop_msg"])
                st.slider("Rotate (°)", -20, 20, key="crop_rot", help="Straighten a tilted plate")
                if crop_mode == "Sliders":
                    st.slider("Left ↔ right (%)", 0, 100, key="crop_x")
                    st.slider("Top ↕ bottom (%)", 0, 100, key="crop_y")
                    if not cropper_ok:
                        st.caption("Tip: `pip install streamlit-cropper` adds drag-to-crop.")
                elif img_bytes:
                    from PIL import Image as _PILImage
                    base = _PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
                    if st.session_state.get("crop_rot"):
                        base = base.rotate(st.session_state["crop_rot"], expand=True, fillcolor=(128, 128, 128))
                    st.session_state["crop_dragged"] = st_cropper(base, realtime_update=True, box_color="#FF2828",
                                                                  aspect_ratio=None, key="cropper")

            with st.expander("Simulate a bad scan (blur · dust · mud on one character)", expanded=False):
                st.caption("Damages the picture before OCR, so the noise comes from the *reading*, not from the data.")
                d1, d2 = st.columns(2)
                with d1:
                    blur = st.slider("Blur", 0.0, 3.0, 0.0, 0.5, key="dg_blur")
                    dust = st.slider("Dust", 0.0, 0.05, 0.0, 0.01, key="dg_dust")
                with d2:
                    smudge = st.selectbox("Mud over character", ["none"] + [str(i) for i in range(1, 11)],
                                          key="dg_smudge")
                    st.checkbox("Mark low-confidence characters as ?", value=True, key="dg_wild",
                                help="A character read with confidence < 0.60 becomes '?', which the matcher "
                                     "treats as 'unknown' instead of trusting a wrong letter.")
            if st.button("Re-generate sample scans", key="btn_gen_plates",
                         help="One ANPR-style crop per noisy sighting in the DB + the 2 demo scans"):
                try:
                    paths = generate_samples_from_manifest(30)
                    st.success(f"Wrote {len(paths)} images to sample_plates/")
                    (getattr(st, "rerun", None) or st.experimental_rerun)()
                except Exception as e:
                    st.error(f"Generate failed: {e}")

        with right:
            if not img_bytes:
                st.info("Pick or upload a scan, then press **Read plate from image**.")
            else:
                from PIL import Image as _PILImage
                raw_img = _PILImage.open(io.BytesIO(img_bytes))
                vtype_meta = (raw_img.info or {}).get("VehicleType", "")
                body_colour = estimate_color_name(raw_img.convert("RGB"))  # from the whole photo, not the crop
                cx, cy = st.session_state.get("crop_x", (0, 100)), st.session_state.get("crop_y", (0, 100))
                rot = st.session_state.get("crop_rot", 0)
                dragged = st.session_state.get("crop_dragged") if st.session_state.get("crop_mode") == "Drag a box" else None
                cropped = dragged is not None or rot != 0 or tuple(cx) != (0, 100) or tuple(cy) != (0, 100)
                work = raw_img.convert("RGB")
                if dragged is not None:
                    work = dragged.convert("RGB")
                elif cropped:
                    work = crop_rotate(raw_img, (cx[0], cy[0], cx[1], cy[1]), rot)
                damaged = blur > 0 or dust > 0 or smudge != "none"
                if damaged:
                    work = degrade_image(work, blur=blur, dust=dust,
                                         smudge_char=None if smudge == "none" else int(smudge) - 1)
                processed = cropped or damaged
                # a processed image carries no plate metadata: what is read comes from the pixels only
                read_bytes = image_to_png_bytes(work, {"VehicleType": vtype_meta}) if processed else img_bytes
                if processed:
                    i1, i2 = st.columns([1.3, 1])
                    preview = (draw_box_preview(raw_img, (cx[0], cy[0], cx[1], cy[1]), rot)
                               if cropped and dragged is None else raw_img)
                    i1.image(image_to_png_bytes(preview.convert("RGB")), caption="original"
                             + (" · red box = crop" if cropped and dragged is None else ""), use_container_width=True)
                    i2.image(read_bytes, caption="this is what is read" + (" (damaged)" if damaged else ""),
                             use_container_width=True)
                else:
                    st.image(img_bytes, caption=img_name, use_container_width=True)
                if st.button("Read plate from image", type="primary", key="btn_ocr"):
                    try:
                        r = extract_from_image(read_bytes, filename="" if processed else img_name)
                        r["color"] = body_colour
                        plate_read = r.get("plate") or ""
                        confs = r.get("char_conf") or []
                        if st.session_state.get("dg_wild") and confs and len(confs) == len(plate_read):
                            plate_read = "".join(ch if c >= 0.60 else "?" for ch, c in zip(plate_read, confs))
                        r["plate_used"] = plate_read
                        st.session_state["ocr_result"] = r
                        st.session_state["match_plate"] = plate_read
                        st.session_state["match_color"] = r.get("color") or ""
                        st.session_state["match_type"] = r.get("type") or ""
                        st.session_state.pop("match_hits", None)
                        st.session_state.pop("near_rows", None)
                        flags["image"] = bool(plate_read)
                    except Exception as e:
                        st.error(f"Image read failed: {e}")
                ocr = st.session_state.get("ocr_result")
                if ocr:
                    st.markdown(ocr_chips_html(ocr.get("plate") or "", ocr.get("char_conf") or []),
                                unsafe_allow_html=True)
                    o1, o2, o3 = st.columns([2, 1, 1])
                    o1.metric("Plate used for matching", ocr.get("plate_used") or "—")
                    o2.metric("Colour (pixels)", ocr.get("color") or "—")
                    o3.metric("Type (camera)", ocr.get("type") or "—")
                    st.caption(f"Method: **{ocr.get('method')}** · mean confidence {ocr.get('confidence', 0):.2f}"
                               + (f" · {ocr['metadata_check']}" if ocr.get("metadata_check") else ""))

        st.markdown("##### Step 2 · Confirm what was read and search")
        mode = st.radio(
            "Direction",
            ["Scanned plate → matching vehicles", "Recorded vehicle → noisy records & scans (vice-versa)"],
            key="match_mode", horizontal=True,
        )
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            plate_q = st.text_input("Plate ( ? = unreadable )", key="match_plate", placeholder="DL9CAF8B88")
        with c2:
            color_q = st.text_input("Colour", key="match_color", placeholder="Black")
        with c3:
            type_q = st.text_input("Vehicle type", key="match_type", placeholder="SUV")
        with c4:
            model_q = st.text_input("Model (optional)", key="match_model", placeholder="Tata Nexon",
                                    help="e.g. told by the officer at the checkpoint")
        s1, s2 = st.columns(2)
        with s1:
            min_score = st.slider("Minimum score", 0.50, 0.95, 0.70, 0.01, key="match_min")
        with s2:
            top_k = st.slider("Show at most", 5, 50, 15, key="match_topk")

        if st.button("Find matches", type="primary", key="btn_match"):
            if not (plate_q or "").strip():
                st.error("Read a plate from an image or type one.")
            else:
                invalidate_cache()
                if mode.startswith("Scanned"):
                    st.session_state["match_hits"] = find_approximate_matches(
                        plate_q.strip(), top_k=int(top_k), min_score=float(min_score),
                        query_color=(color_q or "").strip() or None,
                        query_model=(model_q or "").strip() or None,
                        query_type=(type_q or "").strip() or None,
                    )
                    st.session_state.pop("near_rows", None)
                    flags["match"] = bool(st.session_state["match_hits"])
                else:
                    st.session_state["near_rows"] = find_records_near_plate(
                        plate_q.strip(), top_k=int(top_k) * 2, min_score=float(min_score))
                    st.session_state.pop("match_hits", None)
                    flags["vice"] = bool(st.session_state["near_rows"])
                st.session_state["match_query"] = plate_q.strip()

        hits = st.session_state.get("match_hits")
        near_rows = st.session_state.get("near_rows")
        query = st.session_state.get("match_query", "")
        if hits is not None:
            st.markdown(f"##### Step 3 · {len(hits)} vehicle(s) approximately matching `{query}`")
            if not hits:
                st.warning("Nothing above the minimum score — lower it.")
            else:
                best = hits[0]
                b1, b2, b3, b4 = st.columns([2, 1, 1, 1])
                b1.metric("Best match", best.plate_canonical)
                b2.metric("Final score", f"{best.score:.3f}")
                b3.metric("Plate-text score", f"{best.string_score:.3f}")
                b4.metric("Context", f"{best.context_bonus:+.2f}")
                if len(hits) > 1 and abs(hits[0].string_score - hits[1].string_score) < 0.02:
                    st.info(f"The plate text alone cannot separate **{hits[0].plate_canonical}** and "
                            f"**{hits[1].plate_canonical}** (text scores {hits[0].string_score:.3f} vs "
                            f"{hits[1].string_score:.3f}) — the colour / type / model context decides.")
                cl, cr = st.columns([1.1, 1], gap="large")
                with cl:
                    st.markdown("**Character by character** (top 3)")
                    for h in hits[:3]:
                        st.markdown(align_html(query, h), unsafe_allow_html=True)
                    st.markdown(ALIGN_LEGEND, unsafe_allow_html=True)
                with cr:
                    st.markdown("**What each signal contributed** (top 5)")
                    contrib = [{"plate": h.plate_canonical, "signal": k, "value": v}
                               for h in hits[:5] for k, v in score_breakdown(h, query).items()]
                    cdf = pd.DataFrame(contrib)
                    chart = (
                        alt.Chart(cdf).mark_bar()
                        .encode(
                            y=alt.Y("plate:N", sort=[h.plate_canonical for h in hits[:5]], title=None),
                            x=alt.X("sum(value):Q", title="score contribution"),
                            color=alt.Color("signal:N", title=None,
                                            sort=["exact match", "OCR-weighted", "Levenshtein", "Jaro-Winkler",
                                                  "plate grammar", "context"]),
                            tooltip=["plate", "signal", alt.Tooltip("value:Q", format=".3f")],
                        )
                        .properties(height=230)
                    )
                    st.altair_chart(chart, use_container_width=True)
                match_df = pd.DataFrame([{
                    "rank": i + 1, "plate": h.plate_canonical, "score": h.score,
                    "text score": h.string_score, "context": h.context_bonus,
                    "why": ", ".join(h.context_reasons) or "-",
                    "ocr_dist": h.ocr_dist, "lev": h.lev, "jaro_winkler": h.jaro_winkler,
                    "grammar repair": "yes" if h.format_repair else "",
                    "model": h.make_model, "colour": h.vehicle_color, "type": h.vehicle_type,
                    "owner": h.owner_name, "status": h.status, "seen in": h.source,
                } for i, h in enumerate(hits)])
                with st.expander(f"All {len(hits)} candidates (table)", expanded=False):
                    st.dataframe(match_df, use_container_width=True, hide_index=True, height=320)
                    export_buttons(match_df, "entity_match_hits", "entity_match")
                st.caption("Next: **② Cleanse** fixes the noisy record using the chosen vehicle.")
        if near_rows is not None:
            st.markdown(f"##### Step 3 · Records & scans close to `{query}`")
            if not near_rows:
                st.warning("No records near this plate.")
            else:
                ndf = pd.DataFrame(near_rows)
                n1, n2, n3, n4 = st.columns(4)
                n1.metric("Records found", len(ndf))
                n2.metric("Noisy spellings", int(ndf["verdict"].str.startswith("noisy").sum()))
                n3.metric("Scanned images", int((ndf["source"] == "scanned_image").sum()))
                n4.metric("Look-alike vehicles", int(ndf["verdict"].str.startswith("look").sum()))
                noisy_view = ndf[ndf["verdict"].str.startswith("noisy")]
                if not noisy_view.empty:
                    st.markdown("**Noisy records of this vehicle**")
                    for r in noisy_view.head(6).itertuples():
                        st.markdown(align_html(r.normalized, query, label=f"{r.source} · {r.record_ref}"),
                                    unsafe_allow_html=True)
                    st.markdown(ALIGN_LEGEND, unsafe_allow_html=True)
                st.dataframe(ndf[["verdict", "source", "native_plate", "score", "ocr_dist", "context",
                                  "color", "type", "model", "where", "record_ref"]],
                             use_container_width=True, hide_index=True, height=300)
                export_buttons(ndf, "records_near_plate", "near_plate")

    # ======================================================== ② CLEANSE
    with m_clean:
        st.markdown("##### A · Cleanse the scanned record")
        hits = st.session_state.get("match_hits") or []
        if not hits:
            st.info("Run **① Match → Find matches** first (direction: scanned plate → vehicles).")
        else:
            options = [h.plate_canonical for h in hits]
            chosen = st.selectbox(
                "Correct vehicle", options, index=0, key="cleanse_choice",
                format_func=lambda p: next(f"{h.plate_canonical}  ·  score {h.score:.3f}  ·  {h.make_model or '?'} / "
                                           f"{h.vehicle_color or '?'} / {h.vehicle_type or '?'}"
                                           for h in hits if h.plate_canonical == p))
            noisy_q = st.session_state.get("match_query", "")
            if "?" in noisy_q:
                st.info("The scan has unreadable characters (`?`): there is no stored record with this exact "
                        "spelling to rewrite. Use **B** below to clean every noisy record in the database.")
            pending = records_for_plates({noisy_q})
            st.markdown(f"Rows recorded as **{noisy_q}** that will be rewritten to **{chosen}** "
                        "(each source keeps its own format; colour/type checked against the vehicle):")
            if pending:
                st.dataframe(pd.DataFrame(pending), use_container_width=True, hide_index=True)
            else:
                st.caption("No stored row has this spelling (already cleansed, or the scan was never recorded).")
            if st.button("Cleanse noisy record", type="primary", key="btn_cleanse", disabled=not pending):
                try:
                    h = next(h for h in hits if h.plate_canonical == chosen)
                    before = pending
                    res = cleanse_noisy_record(
                        noisy_plate=noisy_q, chosen_plate=chosen, match_score=float(h.score),
                        query_color=(st.session_state.get("match_color") or "").strip() or None,
                        query_model=(st.session_state.get("match_model") or "").strip() or None,
                        query_type=(st.session_state.get("match_type") or "").strip() or None,
                        note="cleansed from Entity match tab",
                    )
                    load_cleanse_log.clear()
                    load_summary.clear()
                    load_records.clear()
                    refs = {(r["source"], str(r["record_ref"]).split("|")[0]) for r in before}
                    after = [r for r in records_for_plates({chosen})
                             if (r["source"], str(r["record_ref"]).split("|")[0]) in refs]
                    st.session_state["last_cleanse"] = {"res": res, "before": before, "after": after,
                                                        "noisy": noisy_q, "chosen": chosen}
                    flags["cleanse"] = res["changes"] > 0
                except Exception as e:
                    st.error(f"Cleanse failed: {e}")
            lc = st.session_state.get("last_cleanse")
            if lc:
                st.success(f"Cleansed {lc['noisy']} → {lc['chosen']}: {lc['res']['changes']} change(s).")
                ba1, ba2 = st.columns(2)
                with ba1:
                    st.markdown("**Before**")
                    st.dataframe(pd.DataFrame(lc["before"]), use_container_width=True, hide_index=True)
                with ba2:
                    st.markdown("**After**")
                    st.dataframe(pd.DataFrame(lc["after"]), use_container_width=True, hide_index=True)
                st.markdown("\n".join(f"- {a}" for a in lc["res"]["actions"]))

        st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
        st.markdown("##### B · Whole database: find every anomaly, fix it, prove none are left")
        a1, a2, a3 = st.columns([1, 1.3, 2])
        with a1:
            if st.button("Scan for anomalies", key="btn_scan", use_container_width=True):
                st.session_state["anoms"] = detect_anomalies()
                flags["zero"] = not st.session_state["anoms"]
        with a2:
            if st.button("Cleanse all", type="primary", key="btn_clean_all", use_container_width=True):
                with st.spinner("Cleansing sources and refreshing the warehouse…"):
                    res = cleanse_all(include_review=st.session_state.get("cl_review", False))
                st.session_state["clean_all"] = res
                st.session_state["anoms"] = detect_anomalies()
                flags["zero"] = not st.session_state["anoms"]
                load_cleanse_log.clear()
                load_summary.clear()
                load_records.clear()
                (getattr(st, "rerun", None) or st.experimental_rerun)()  # refresh KPI header
        with a3:
            st.checkbox("also apply REVIEW items (human-approved)", value=False, key="cl_review")
        res = st.session_state.get("clean_all")
        if res:
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Anomalies before", sum(res["before"].values()))
            r2.metric("Fixes applied", res["applied"])
            r3.metric("Passes", res.get("passes", 1))
            r4.metric("Anomalies left", sum(res["after"].values()))
            if not res["after"]:
                st.success("No anomalies left in the recorded data. " + res["etl"])
            else:
                st.warning(f"Left for review: {res['after']}")
        anoms = st.session_state.get("anoms")
        if anoms is not None:
            if not anoms:
                st.success("Scan complete: 0 anomalies in capture_log, stg_policies, stg_registrations, "
                           "theft_shredding and vehicle_360.")
            else:
                adf = pd.DataFrame([a.__dict__ for a in anoms])
                kinds = adf.groupby(["kind", "decision"]).size().reset_index(name="count")
                kc = st.columns(max(1, len(kinds)))
                for col, row in zip(kc, kinds.itertuples()):
                    col.metric(f"{row.kind.replace('_', ' ').title()} · {row.decision}", row.count)
                st.dataframe(adf[["kind", "decision", "source", "record_ref", "field", "current", "suggested",
                                  "score", "margin", "reason"]],
                             use_container_width=True, hide_index=True, height=300)
                export_buttons(adf, "anomalies", "anoms")
        with st.expander("What counts as an anomaly?"):
            st.markdown(
                """
| kind | meaning | fix |
|---|---|---|
| NOISY_PLATE | plate not corroborated by another source but within reach of a trusted vehicle | rewrite to that vehicle |
| FORMAT_VARIANT | right vehicle, wrong format for that source (e.g. `DL-9-CAF-8888` in capture_log) | standardise |
| CONTEXT_CONFLICT | a sighting's colour / type disagrees with the vehicle's other sightings | majority value |
| GHOST_WAREHOUSE_ROW | `vehicle_360` row that no source supports any more (made by noise / a deleted row) | remove |
| DUPLICATE_RECORD | identical policy / registration twice | keep one |

**Trusted vehicle** = valid Indian plate format + seen in ≥ 2 independent sources + not just an OCR twin
of a registered plate. **AUTO** = confident (score ≥ 0.80 with a clear gap to the runner-up, or a clear
gap confirmed by context). **REVIEW** = shown with a suggestion, applied only when a human ticks the box.
                """
            )
        st.markdown("##### Cleanse log (`dwh_cleanse_log` — every change, old → new)")
        clog = load_cleanse_log(60)
        if clog.empty:
            st.caption("No cleanses yet.")
        else:
            st.dataframe(clog, use_container_width=True, hide_index=True, height=260)
            export_buttons(clog, "dwh_cleanse_log", "cleanse_log")
        with st.expander("SQL to show the evaluator"):
            st.code(
                "-- source rows for the demo vehicle\n"
                "SELECT capture_id, plate_no, vehicle_color, vehicle_type, location\n"
                "FROM roadcapture.capture_log WHERE plate_no LIKE 'DL9CAF%' ORDER BY capture_time DESC;\n\n"
                "-- warehouse: no ghost row for the noisy spelling\n"
                "SELECT plate_canonical, make_model, status FROM vehicle_dwh.vehicle_360\n"
                "WHERE plate_canonical IN ('DL9CAF8888','DL9CAF8B88','DL9CAF888');\n\n"
                "-- audit trail\n"
                "SELECT cleansed_at, source_name, field_name, old_value, new_value, action\n"
                "FROM vehicle_dwh.dwh_cleanse_log ORDER BY cleanse_id DESC LIMIT 20;",
                language="sql",
            )

    # ======================================================== ③ ACCURACY
    with m_acc:
        st.markdown(
            """
**Algorithm (combination)** — each signal fixes a different kind of noise:

| signal | catches | example |
|---|---|---|
| normalise | separators / case | `DL-9-CAF-8888` = `dl9caf8888` |
| OCR-weighted edit distance (look-alikes cost 0.25, Damerau) | 0↔O, 1↔I, 8↔B, 5↔S, 2↔Z, 6↔G, swaps | `DL9CAF8B88` |
| Levenshtein | dropped / extra characters | `DL9CAF888` |
| Jaro-Winkler | shared state + RTO prefix | `DL9C…` |
| Indian plate grammar (format repair) | letter in a digit slot | `B` in `8B88` must be `8` |
| context: colour + type (camera), model (RTO) | ties between equally close plates | Black SUV Nexon vs White Swift |

`score = w_ocr·OCR + w_lev·Lev + w_jw·JW + w_fmt·format + context` — weights from
`LogisticRegression` on compact labelled pairs (`train_plate_matching.py` → `match_weights.json`);
OCR glyph dictionary in `plate_ocr_lookup.json` (seed + variants discovered from training).
Plate text alone cannot separate look-alike plates in the same series; context is what pushes
accuracy from ~67% to ~98%.
            """
        )
        st.caption(
            f"App weights (ML): OCR={W_OCR:.2f} Lev={W_LEV:.2f} JW={W_JW:.2f} format={W_FMT:.2f}"
            + (f" · CV F1={WEIGHTS_META.get('cv_metrics', {}).get('cv_f1')}" if WEIGHTS_META.get("cv_metrics") else " · defaults (run train_plate_matching.py)")
            + (f" · OCR dict discoveries: {LOOKUP_META.get('discovered') or '{}'}" if LOOKUP_META else "")
        )
        n_q = st.slider("Benchmark size (noisy plates)", 200, 2000, 600, 100, key="bench_n")
        if st.button("Run accuracy benchmark", type="primary", key="btn_bench"):
            with st.spinner("Corrupting plates and ranking with every algorithm…"):
                st.session_state["bench"] = run_benchmark(n=int(n_q), save=True, verbose=False)
                st.session_state.setdefault("p2_flags", {})["bench"] = True
        bench = st.session_state.get("bench")
        if bench is None and BENCH_JSON.exists():
            try:
                bench = json.loads(BENCH_JSON.read_text(encoding="utf-8"))
                st.caption(f"Last run: {bench.get('run_at')}")
            except Exception:
                bench = None
        if bench:
            ov = pd.DataFrame([{"method": k, **v} for k, v in bench["overall"].items()])
            ov["top1 %"] = (ov["top1"] * 100).round(1)
            chart_bars(ov, "method", "top1 %", height=300)
            st.dataframe(ov[["method", "top1", "top3", "mrr", "n"]], use_container_width=True, hide_index=True)
            pk = pd.DataFrame(bench["per_noise_kind_top1"]).T
            st.markdown("**Top-1 accuracy by noise type** (rows = how the plate was corrupted)")
            st.dataframe((pk * 100).round(0).astype(int).astype(str) + "%", use_container_width=True)
            sn = bench.get("seeded_noise") or {}
            st.caption(
                f"Benchmark grid-search weights: {bench['weights']} · app ML weights: {bench.get('weights_in_app')} · "
                f"blocking recall {bench['blocking_recall']:.0%} · "
                f"each test plate competes with ~24 look-alike vehicles"
                + (f" · seeded DB noise recovered: {sn['top1']:.0%} of {sn['n']}" if sn.get("n") else "")
            )
        manifest = load_noisy_manifest(300)
        with st.expander("Answer key (dwh_noisy_seed_manifest) — used ONLY to score, never by the matcher"):
            if manifest.empty:
                st.caption("Empty — run `python populate_noisy_data.py`.")
            else:
                st.dataframe(manifest, use_container_width=True, hide_index=True, height=260)

    # ======================================================== ④ PROMPT & CHANGES
    with m_vibe:
        vibe = Path(__file__).resolve().parent / "phase2_vibe"
        v1, v2 = st.columns(2, gap="large")
        with v1:
            st.markdown("**Prompts used**")
            p = vibe / "PROMPTS.md"
            st.markdown(_small_headings(p.read_text(encoding="utf-8")) if p.exists() else "_PROMPTS.md missing_")
        with v2:
            st.markdown("**What we changed and why**")
            p = vibe / "CHANGES.md"
            st.markdown(_small_headings(p.read_text(encoding="utf-8")) if p.exists() else "_CHANGES.md missing_")
        st.markdown("**Generated code vs our code** (green = added, red = removed)")
        gen_files = ["entity_matching.py", "populate_noisy_data.py", "plate_ocr.py", "app.py"]
        f = st.selectbox("File", gen_files, key="vibe_file")
        gp, cp = vibe / "generated" / f, Path(__file__).resolve().parent / f
        if gp.exists() and cp.exists():
            a = gp.read_text(encoding="utf-8").splitlines()
            b = cp.read_text(encoding="utf-8").splitlines()
            diff = "\n".join(difflib.unified_diff(a, b, f"generated/{f}", f"current/{f}", lineterm="", n=2))
            added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
            st.caption(f"{len(a)} generated lines → {len(b)} now · +{added} / -{removed}")
            st.code(diff or "(identical)", language="diff")
            with st.expander("Show the generated file as-is"):
                st.code(gp.read_text(encoding="utf-8"), language="python")
        else:
            st.info("phase2_vibe/generated/ not found.")
        st.caption("Full side-by-side report: `python phase2_vibe/show_changes.py` → phase2_vibe/changes.html")


    strip_ph.markdown(progress_strip_html(st.session_state.get("p2_flags", {})), unsafe_allow_html=True)


# ---- Analytics (stacked full width; chart labels horizontal) ----
with tab_analytics:
    st.subheader("State risk")
    state = load_state_risk()
    if state.empty:
        st.info("No state risk data. Run ETL.")
    else:
        chart_bars(state, "state_code", "risk_score", height=340)
        st.dataframe(state, use_container_width=True, hide_index=True, height=360)
        export_buttons(state, "analytics_state_risk", "state")

    st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
    st.subheader("Hotspot locations")
    hot = load_hotspots()
    if hot.empty:
        st.info("No hotspot data. Run ETL.")
    else:
        st.bar_chart(
            hot.set_index("location")[["flagged_sightings"]].head(20),
            height=340,
        )
        st.dataframe(hot, use_container_width=True, hide_index=True, height=360)
        export_buttons(hot, "analytics_hotspots", "hot")

    st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
    st.subheader("Insurer gaps")
    gap = load_insurer_gap()
    if gap.empty:
        st.info("No insurer gap data. Run ETL.")
    else:
        chart_gap = gap.set_index("insurer")[
            ["expired_flagged", "still_listed_but_stolen"]
        ]
        st.bar_chart(chart_gap, height=340)
        st.dataframe(gap, use_container_width=True, hide_index=True, height=360)
        export_buttons(gap, "analytics_insurer_gap", "gap")

# ---- Feedback ----
with tab_feedback:
    st.subheader("Feedback")
    st.markdown(
        "Tell us what worked, what broke, or what to improve. "
        "Submissions are stored in warehouse table `ui_feedback`."
    )

    with st.form("feedback_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Name", placeholder="Your name")
            role = st.selectbox(
                "Role",
                [
                    "Warehouse owner",
                    "Insurance source",
                    "RTO source",
                    "Road capture",
                    "Theft source",
                    "Evaluator / instructor",
                    "Other",
                ],
            )
            email = st.text_input("Email (optional)", placeholder="you@example.com")
        with c2:
            category = st.selectbox(
                "Category",
                [
                    "General",
                    "UI / usability",
                    "ETL / data",
                    "Docker / setup",
                    "Bug report",
                    "Feature request",
                ],
            )
            rating = st.slider("Rating", min_value=1, max_value=5, value=4)
        message = st.text_area(
            "Message",
            placeholder="What should we know?",
            height=140,
        )
        submitted = st.form_submit_button("Submit feedback", type="primary")

    if submitted:
        if not name.strip() or not message.strip():
            st.error("Name and message are required.")
        else:
            try:
                fid = save_feedback(
                    name=name.strip(),
                    role=role,
                    rating=rating,
                    category=category,
                    message=message.strip(),
                    contact_email=email.strip(),
                )
                st.success(f"Thanks — feedback #{fid} saved.")
            except Exception as e:
                st.error(f"Could not save feedback: {e}")

    st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
    st.subheader("Recent feedback")
    try:
        recent = load_recent_feedback(20)
        if recent.empty:
            st.info("No feedback yet.")
        else:
            st.dataframe(recent, use_container_width=True, hide_index=True, height=320)
            export_buttons(recent, "ui_feedback", "feedback_recent")
    except Exception as e:
        st.warning(f"Could not load feedback list: {e}")
