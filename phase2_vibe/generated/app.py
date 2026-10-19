"""
app.py — Warehouse-only dashboard for vehicle_dwh.

Run:  streamlit run app.py
"""

from __future__ import annotations

import re
from datetime import datetime

import pandas as pd
import streamlit as st
import altair as alt
from fpdf import FPDF

from db_config import connect
from entity_matching import (
    cleanse_noisy_record,
    find_approximate_matches,
    find_noisy_near_clean,
    plate_similarity,
)
from plate_ocr import SAMPLE_DIR, extract_from_image, generate_samples_from_manifest
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
        return wh_query(
            """
            SELECT cleanse_id, cleansed_at, noisy_plate, chosen_plate, match_score,
                   context_json, action, note
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
1. **Upload** a scanned registration image (use `sample_plates/` demos).
2. Press **Extract plate from image** (OCR / ANPR metadata + colour hint).
3. Confirm plate + color/model/type, then **Find approximate matches**.
4. Select the best candidate → **Cleanse** to fix the recorded capture.
5. Vice-versa mode: clean plate → nearby noisy spellings.

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

# ---- Entity match (Phase 2 noisy OCR) ----
with tab_match:
    st.subheader("Entity matching under noisy plates")
    with st.expander("Why this matching algorithm?", expanded=False):
        st.markdown(
            """
**Image → text:** upload a scanned registration crop; OCR / ANPR metadata extracts a
*noisy* plate string (+ colour hint from pixels).

**Text → entities:**
1. **Normalize** separators/case (`DL-9-CAF-8888` ≡ `dl9caf8888`).
2. **OCR-aware edit distance** — `{0,O}`, `{1,I,L}`, `{8,B}`, `{5,S}`, `{2,Z}` cost 0.
3. **Levenshtein** + **Jaro-Winkler**, then **context** (color / type / model).

Score ≈ `0.45·OCR_sim + 0.25·Lev_sim + 0.30·JW + context`.
            """
        )

    st.markdown("#### 1) Upload scanned registration image")
    up_l, up_r = st.columns([2, 2], gap="large")
    with up_l:
        uploaded = st.file_uploader(
            "ANPR / phone crop (PNG or JPG)",
            type=["png", "jpg", "jpeg", "webp"],
            key="match_upload",
            help="Use files from sample_plates/ for a reliable demo, or any plate photo if Tesseract is installed.",
        )
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Generate sample plate images", key="btn_gen_plates"):
                try:
                    paths = generate_samples_from_manifest(30)
                    st.success(f"Wrote {len(paths)} files in `{SAMPLE_DIR.name}/`")
                except Exception as e:
                    st.error(f"Generate failed: {e}")
        with b2:
            if SAMPLE_DIR.exists():
                samples = sorted(SAMPLE_DIR.glob("scan_*.png"))
                st.caption(f"{len(samples)} sample image(s) in sample_plates/")
    with up_r:
        if uploaded is not None:
            raw = uploaded.getvalue()
            st.image(raw, caption=uploaded.name, use_container_width=True)
            if st.button("Extract plate from image", type="primary", key="btn_ocr"):
                try:
                    result = extract_from_image(raw, filename=uploaded.name)
                    st.session_state["ocr_result"] = result
                    if result.get("plate"):
                        st.session_state["match_plate"] = result["plate"]
                    if result.get("color"):
                        st.session_state["match_color"] = result["color"]
                    if result.get("type"):
                        st.session_state["match_type"] = result["type"]
                    # link expected clean from manifest if known
                    man = load_noisy_manifest(500)
                    if not man.empty and result.get("plate"):
                        hit = man[
                            man["noisy_plate"].astype(str).str.upper()
                            == str(result["plate"]).upper()
                        ]
                        if not hit.empty:
                            st.session_state["match_expected_clean"] = str(
                                hit.iloc[0]["clean_plate"]
                            )
                            if hit.iloc[0].get("context_model"):
                                st.session_state["match_model"] = str(
                                    hit.iloc[0]["context_model"] or ""
                                )
                    st.success(
                        f"Extracted `{result.get('plate') or '∅'}` via {result.get('method')}"
                    )
                except Exception as e:
                    st.error(f"Image extract failed: {e}")
        else:
            st.info(
                "Upload a plate image, then **Extract plate from image**. "
                "Demo files: run **Generate sample plate images** and upload from `sample_plates/`."
            )

    if st.session_state.get("ocr_result"):
        ocr = st.session_state["ocr_result"]
        st.caption(f"OCR method: {ocr.get('method')} — {ocr.get('notes')}")

    st.markdown("#### 2) Confirm extracted plate + context (editable)")
    manifest = load_noisy_manifest(300)
    with st.expander("Or pick a seeded noisy plate without uploading", expanded=False):
        if not manifest.empty:
            road_noise = manifest[manifest["source_name"] == "roadcapture"].copy()
            if road_noise.empty:
                road_noise = manifest.copy()
            labels = [
                f"{r.noisy_plate}  →  clean {r.clean_plate}  ({r.noise_kind})"
                for r in road_noise.itertuples()
            ]
            choice = st.selectbox(
                "Manifest",
                options=["(skip)"] + labels,
                key="match_manifest_pick",
            )
            if choice != "(skip)":
                row = road_noise.iloc[labels.index(choice)]
                st.session_state["match_plate"] = str(row["noisy_plate"])
                st.session_state["match_color"] = str(row.get("context_color") or "")
                st.session_state["match_type"] = str(row.get("context_type") or "")
                st.session_state["match_model"] = str(row.get("context_model") or "")
                st.session_state["match_expected_clean"] = str(row["clean_plate"])
        else:
            st.caption("Run `python populate_noisy_data.py` first.")

    mode = st.radio(
        "Direction",
        [
            "Noisy / scanned plate → approximate warehouse vehicles",
            "Clean warehouse plate → nearby noisy / alternate spellings",
        ],
        key="match_mode",
        horizontal=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        plate_q = st.text_input("Plate from image", key="match_plate", placeholder="DL9CAF8B88")
    with c2:
        color_q = st.text_input("Image color", key="match_color", placeholder="Black")
    with c3:
        model_q = st.text_input("Model", key="match_model", placeholder="Tata Nexon")
    with c4:
        type_q = st.text_input("Vehicle type", key="match_type", placeholder="SUV")
    s1, s2 = st.columns(2)
    with s1:
        min_score = st.slider("Minimum score", 0.50, 0.95, 0.72, 0.01, key="match_min")
    with s2:
        top_k = st.slider("Top K", 5, 30, 12, key="match_topk")

    if st.button("Find approximate matches", type="primary", key="btn_match"):
        if not (plate_q or "").strip():
            st.error("Upload an image and extract a plate, or type a plate.")
        else:
            try:
                q = plate_q.strip()
                if mode.startswith("Noisy"):
                    hits = find_approximate_matches(
                        q,
                        top_k=int(top_k),
                        min_score=float(min_score),
                        query_color=(color_q or "").strip() or None,
                        query_model=(model_q or "").strip() or None,
                        query_type=(type_q or "").strip() or None,
                        include_exact=True,
                    )
                else:
                    hits = find_noisy_near_clean(
                        q,
                        top_k=int(top_k),
                        min_score=float(min_score),
                    )
                st.session_state["match_hits"] = hits
                st.session_state["match_query"] = q
                expected = st.session_state.get("match_expected_clean")
                if expected and hits:
                    top = hits[0].plate_canonical
                    if top == expected or any(h.plate_canonical == expected for h in hits):
                        st.session_state["match_hint"] = (
                            f"Seeded clean plate `{expected}` appears in results (top=`{top}`)."
                        )
                    else:
                        st.session_state["match_hint"] = (
                            f"Seeded clean was `{expected}` — not in top hits; lower min score."
                        )
            except Exception as e:
                st.error(f"Match failed: {e}")

    if st.session_state.get("match_hint"):
        st.info(st.session_state["match_hint"])

    hits = st.session_state.get("match_hits") or []
    st.markdown("#### 3) Approximate matches")
    if hits:
        rows = [
            {
                "rank": i + 1,
                "plate_canonical": h.plate_canonical,
                "native_plate": h.native_plate,
                "score": h.score,
                "ocr_dist": h.ocr_dist,
                "lev": h.lev,
                "jaro_winkler": h.jaro_winkler,
                "context_bonus": h.context_bonus,
                "context": ", ".join(h.context_reasons) or "-",
                "owner_name": h.owner_name,
                "make_model": h.make_model,
                "vehicle_color": h.vehicle_color,
                "status": h.status,
                "source": h.source,
            }
            for i, h in enumerate(hits)
        ]
        match_df = pd.DataFrame(rows)
        st.dataframe(match_df, use_container_width=True, hide_index=True, height=360)
        export_buttons(match_df, "entity_match_hits", "entity_match")

        best = hits[0]
        sim = plate_similarity(
            st.session_state.get("match_query", plate_q or ""), best.native_plate
        )
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Best plate", best.plate_canonical)
        m2.metric("Score", f"{best.score:.3f}")
        m3.metric("OCR distance", f"{sim['ocr_dist']}")
        m4.metric("Jaro-Winkler", f"{sim['jaro_winkler']:.3f}")

        st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
        st.markdown("#### 4) Cleanse noisy recorded data")
        st.caption(
            "Rewrites the noisy `capture_log` plate, merges/removes the anomalous "
            "`vehicle_360` row, and appends `dwh_cleanse_log`."
        )
        options = [h.plate_canonical for h in hits]
        default_ix = 0
        expected = st.session_state.get("match_expected_clean")
        if expected in options:
            default_ix = options.index(expected)
        chosen = st.selectbox(
            "Chosen clean plate",
            options,
            index=default_ix,
            key="cleanse_choice",
        )
        note = st.text_input(
            "Cleanse note",
            value="OCR noise corrected using plate similarity + color/model context",
            key="cleanse_note",
        )
        if st.button("Cleanse noisy record", type="primary", key="btn_cleanse"):
            try:
                chosen_hit = next(h for h in hits if h.plate_canonical == chosen)
                result = cleanse_noisy_record(
                    noisy_plate=st.session_state.get("match_query", plate_q or ""),
                    chosen_plate=chosen,
                    match_score=float(chosen_hit.score),
                    query_color=(color_q or "").strip() or None,
                    query_model=(model_q or "").strip() or None,
                    query_type=(type_q or "").strip() or None,
                    note=note,
                    fix_capture=True,
                )
                load_cleanse_log.clear()
                load_noisy_manifest.clear()
                st.session_state["last_cleanse"] = result
                st.success(
                    f"Cleansed `{result['noisy_normalized']}` → `{result['chosen_plate']}`. "
                    f"capture_fixed={result['capture_fixed']}. "
                    f"{'; '.join(result['actions']) or 'logged'}. "
                    "Run `python run_etl.py` if status dims look stale."
                )
            except Exception as e:
                st.error(f"Cleanse failed: {e}")
    else:
        st.info(
            "Pick a noisy plate from the manifest (or type one), set color/model/type, "
            "then press **Find approximate matches**."
        )

    st.markdown('<div class="section-gap"></div>', unsafe_allow_html=True)
    st.markdown("#### 5) Noisy seed inventory & cleanse audit")
    inv1, inv2 = st.columns(2, gap="large")
    with inv1:
        st.markdown("**Seeded noisy rows** (`dwh_noisy_seed_manifest`)")
        if manifest.empty:
            st.caption("Empty — run `python populate_noisy_data.py`.")
        else:
            st.dataframe(
                manifest[
                    [
                        "noisy_plate",
                        "clean_plate",
                        "noise_kind",
                        "source_name",
                        "context_color",
                        "context_type",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
                height=280,
            )
            export_buttons(manifest, "noisy_seed_manifest", "noisy_manifest")
    with inv2:
        st.markdown("**Cleanse log** (`dwh_cleanse_log`)")
        clog = load_cleanse_log(40)
        if clog.empty:
            st.caption("No cleanses yet.")
        else:
            st.dataframe(clog, use_container_width=True, hide_index=True, height=280)
            export_buttons(clog, "dwh_cleanse_log", "cleanse_log")


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
