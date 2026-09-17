"""
app.py — Warehouse-only dashboard for vehicle_dwh.

Reads serving tables + vehicle_360. No source-DB queries, no logs tab,
no ministry/dossier UI. CSV + PDF export on every data view.

Run:  streamlit run app.py
"""

from __future__ import annotations

import io
import re
from datetime import datetime

import pandas as pd
import streamlit as st
from fpdf import FPDF

from db_config import connect
from warehouse_ops import read_summary

st.set_page_config(
    page_title="Vehicle warehouse dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

WAREHOUSE = "vehicle_dwh"

SEVERITY = {
    "Stolen / shredded": 0,
    "No insurance on file": 1,
    "Insurance expired": 2,
    "Clear": 3,
}


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
    """All UI reads go through vehicle_dwh only."""
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
def load_fleet(limit: int = 5000) -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM vehicle_360 ORDER BY last_seen_time DESC LIMIT %s",
        (int(limit),),
    )


@st.cache_data(ttl=20)
def load_flagged() -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM dwh_flagged_cache ORDER BY last_seen_time DESC"
    )


@st.cache_data(ttl=20)
def load_priority() -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM ministry_priority ORDER BY severity_rank, last_seen_time DESC"
    )


@st.cache_data(ttl=20)
def load_state_risk() -> pd.DataFrame:
    return wh_query(
        "SELECT * FROM ministry_state_risk ORDER BY risk_score DESC"
    )


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
def load_conflicts() -> pd.DataFrame:
    return wh_query(
        "SELECT conflict_type, plate_canonical, detail, refreshed_at "
        "FROM dwh_conflict_cache ORDER BY conflict_id DESC LIMIT 1000"
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


def df_to_pdf_bytes(df: pd.DataFrame, title: str) -> bytes:
    """Simple tabular PDF (first 200 rows, truncated cells)."""
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, title[:80], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(
        0,
        6,
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  rows={len(df)}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    if df.empty:
        pdf.cell(0, 8, "No rows.", new_x="LMARGIN", new_y="NEXT")
        return bytes(pdf.output())

    view = df.head(200).copy()
    cols = list(view.columns)
    # Fit columns on landscape A4 (~277mm usable)
    usable = 277
    col_w = max(18, usable / max(len(cols), 1))

    pdf.set_font("Helvetica", "B", 7)
    for c in cols:
        pdf.cell(col_w, 6, str(c)[:18], border=1)
    pdf.ln()

    pdf.set_font("Helvetica", "", 7)
    for _, row in view.iterrows():
        for c in cols:
            val = "" if pd.isna(row[c]) else str(row[c])
            pdf.cell(col_w, 5, val[:22], border=1)
        pdf.ln()

    if len(df) > 200:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 6, f"(Showing first 200 of {len(df)} rows — use CSV for full export.)")

    return bytes(pdf.output())


def export_buttons(df: pd.DataFrame, stem: str, key: str) -> None:
    c1, c2, _ = st.columns([1, 1, 4])
    with c1:
        st.download_button(
            "Download CSV",
            data=df_to_csv_bytes(df),
            file_name=f"{stem}.csv",
            mime="text/csv",
            key=f"csv_{key}",
            disabled=df.empty,
        )
    with c2:
        st.download_button(
            "Download PDF",
            data=df_to_pdf_bytes(df, stem.replace("_", " ").title()),
            file_name=f"{stem}.pdf",
            mime="application/pdf",
            key=f"pdf_{key}",
            disabled=df.empty,
        )


def filter_fleet(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    work["category"] = work["status"].astype(str).apply(bucket)

    q = st.text_input("Search plate / owner / location", key="fleet_q")
    cats = sorted(work["category"].unique(), key=lambda c: SEVERITY.get(c, 9))
    chosen = st.multiselect("Status", cats, default=cats, key="fleet_cats")
    work = work[work["category"].isin(chosen)]

    if q.strip():
        needle = q.strip().lower()
        plate_n = normalize_plate(q)
        mask = (
            work["plate_canonical"].astype(str).str.contains(plate_n, case=False, na=False)
            | work["owner_name"].astype(str).str.lower().str.contains(needle, na=False)
            | work["last_seen_location"].astype(str).str.lower().str.contains(needle, na=False)
            | work["make_model"].astype(str).str.lower().str.contains(needle, na=False)
        )
        work = work[mask]
    return work


# --------------------------------------------------------------------------- UI
st.title("Vehicle warehouse dashboard")
st.caption(
    "All views read **vehicle_dwh** only (fact + serving tables). "
    "Refresh with `python run_etl.py` after source data changes."
)

summary = load_summary()
total = sum(summary.values())
flagged_n = total - summary.get("OK", 0)

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Vehicles", f"{total:,}")
k2.metric("Clear", f"{summary.get('OK', 0):,}")
k3.metric("Flagged", f"{flagged_n:,}")
k4.metric("Stolen", f"{summary.get('STOLEN', 0):,}")
k5.metric("No / expired cover", f"{summary.get('NO INSURANCE', 0) + summary.get('EXPIRED', 0):,}")

tab_overview, tab_fleet, tab_flagged, tab_analytics, tab_conflicts = st.tabs(
    ["Overview", "Fleet", "Flagged", "Analytics", "Conflicts"]
)

# ---- Overview ----
with tab_overview:
    left, right = st.columns(2)
    with left:
        st.subheader("Status mix")
        chart = pd.DataFrame(
            [{"bucket": k, "vehicles": v} for k, v in summary.items()]
        )
        if not chart.empty:
            st.bar_chart(chart.set_index("bucket"))
        else:
            st.info("No summary yet — run ETL.")
    with right:
        st.subheader("Top enforcement priority")
        pri = load_priority().head(15)
        if pri.empty:
            st.info("Priority queue empty — run ETL.")
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

    st.subheader("Lookup one plate")
    plate_in = st.text_input("Plate (any format)", key="overview_plate")
    if plate_in:
        plate = normalize_plate(plate_in)
        hit = wh_query(
            "SELECT * FROM vehicle_360 WHERE plate_canonical = %s", (plate,)
        )
        xref = load_xref(plate)
        if hit.empty:
            st.warning(f"No warehouse row for `{plate}`.")
        else:
            st.dataframe(hit, use_container_width=True, hide_index=True)
            if not xref.empty:
                st.caption("Source plate spellings (xref)")
                st.dataframe(xref, use_container_width=True, hide_index=True)
            export_buttons(hit, f"vehicle_{plate}", "overview_hit")

# ---- Fleet ----
with tab_fleet:
    st.subheader("Integrated fleet (`vehicle_360`)")
    limit = st.slider("Max rows to load", 500, 20000, 5000, step=500)
    fleet = load_fleet(limit)
    view = filter_fleet(fleet)
    st.caption(f"Showing {len(view):,} of {len(fleet):,} loaded rows")
    st.dataframe(view, use_container_width=True, hide_index=True, height=480)
    export_buttons(view, "fleet_vehicle_360", "fleet")

# ---- Flagged ----
with tab_flagged:
    st.subheader("Flagged vehicles (serving cache)")
    flagged = load_flagged()
    if flagged.empty:
        st.warning("Flagged cache empty — run `python run_etl.py`.")
    else:
        flagged = flagged.copy()
        flagged["category"] = flagged["status"].astype(str).apply(bucket)
        cats = sorted(flagged["category"].unique(), key=lambda c: SEVERITY.get(c, 9))
        chosen = st.multiselect("Filter", cats, default=cats, key="flag_cats")
        view = (
            flagged[flagged["category"].isin(chosen)]
            .assign(_sev=lambda d: d["category"].map(SEVERITY))
            .sort_values(["_sev", "last_seen_time"], ascending=[True, False])
            .drop(columns="_sev", errors="ignore")
        )
        st.dataframe(view, use_container_width=True, hide_index=True, height=480)
        export_buttons(view, "flagged_vehicles", "flagged")

# ---- Analytics ----
with tab_analytics:
    st.subheader("Warehouse analytics (precomputed by ETL)")
    a1, a2 = st.columns(2)

    with a1:
        st.markdown("#### State risk")
        state = load_state_risk()
        if state.empty:
            st.info("No data.")
        else:
            st.bar_chart(state.set_index("state_code")[["risk_score"]])
            st.dataframe(state, use_container_width=True, hide_index=True, height=280)
            export_buttons(state, "analytics_state_risk", "state")

    with a2:
        st.markdown("#### Hotspot locations")
        hot = load_hotspots()
        if hot.empty:
            st.info("No data.")
        else:
            st.bar_chart(hot.set_index("location")[["flagged_sightings"]].head(15))
            st.dataframe(hot, use_container_width=True, hide_index=True, height=280)
            export_buttons(hot, "analytics_hotspots", "hot")

    st.markdown("#### Insurer gaps (expired / still listed but stolen)")
    gap = load_insurer_gap()
    if gap.empty:
        st.info("No data.")
    else:
        st.dataframe(gap, use_container_width=True, hide_index=True, height=320)
        export_buttons(gap, "analytics_insurer_gap", "gap")

# ---- Conflicts ----
with tab_conflicts:
    st.subheader("Integration conflicts (`dwh_conflict_cache`)")
    conf = load_conflicts()
    if conf.empty:
        st.warning("Conflict cache empty — run ETL.")
    else:
        types = sorted(conf["conflict_type"].unique())
        chosen = st.multiselect("Type", types, default=types, key="conf_types")
        view = conf[conf["conflict_type"].isin(chosen)]
        st.dataframe(view, use_container_width=True, hide_index=True, height=480)
        export_buttons(view, "conflicts", "conf")
