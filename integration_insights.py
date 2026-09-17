"""
integration_insights.py
-----------------------
Innovative integration features used by the Streamlit "Innovation lab" tab.

1. Live schema-matching studio  — show heterogeneous plate forms + join key
2. Decision provenance          — explain WHY a status was chosen (rule trace)
3. Cross-source conflict scan   — find disagreements no single DB can see
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pandas as pd

from db_config import connect

AS_OF = date(2026, 9, 12)


def normalize_plate(raw: str | None) -> str:
    if raw is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def detect_plate_style(raw: str) -> str:
    s = str(raw).strip()
    if " " in s:
        return "spaced (theft-style)"
    if "-" in s:
        return "hyphenated (insurance-style)"
    if s.islower():
        return "lowercase compact (RTO-style)"
    if s.isupper() and s.isalnum():
        return "uppercase compact (capture-style)"
    return "mixed / other"


def _fetch(database: str, sql: str, params=None) -> pd.DataFrame:
    conn = connect(database)
    cur = conn.cursor()
    cur.execute(sql, params or ())
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return pd.DataFrame(rows, columns=cols)


def live_match_plate(user_plate: str) -> dict[str, Any]:
    """Pull raw rows from all 4 sources for one plate (after canonicalization)."""
    key = normalize_plate(user_plate)
    out: dict[str, Any] = {
        "input": user_plate,
        "input_style": detect_plate_style(user_plate),
        "canonical": key,
        "sources": {},
        "mapping": [
            {"local_attr": "roadcapture.capture_log.plate_no", "global": "plate_canonical",
             "transform": "normalize_plate"},
            {"local_attr": "insurance.policies.vehicle_number", "global": "plate_canonical",
             "transform": "normalize_plate"},
            {"local_attr": "rto_registration.registrations.reg_no", "global": "plate_canonical",
             "transform": "normalize_plate"},
            {"local_attr": "theft_records.theft_shredding.plate_number", "global": "plate_canonical",
             "transform": "normalize_plate"},
        ],
    }
    if not key:
        return out

    # Fetch candidates with a broad LIKE then filter in Python by normalize
    # (avoids depending on MySQL REGEXP privileges / dialect)
    captures = _fetch("roadcapture", "SELECT * FROM capture_log")
    policies = _fetch("insurance", "SELECT * FROM policies")
    regs = _fetch("rto_registration", "SELECT * FROM registrations")
    thefts = _fetch("theft_records", "SELECT * FROM theft_shredding")

    cap_hit = captures[captures["plate_no"].map(normalize_plate) == key]
    pol_hit = policies[policies["vehicle_number"].map(normalize_plate) == key]
    reg_hit = regs[regs["reg_no"].map(normalize_plate) == key]
    thf_hit = thefts[thefts["plate_number"].map(normalize_plate) == key]

    def pack(df: pd.DataFrame, plate_col: str) -> dict:
        if df.empty:
            return {"hit": False, "rows": df, "native_plates": [], "styles": []}
        plates = df[plate_col].astype(str).tolist()
        return {
            "hit": True,
            "rows": df,
            "native_plates": plates,
            "styles": [detect_plate_style(p) for p in plates],
        }

    out["sources"] = {
        "roadcapture.capture_log": pack(cap_hit, "plate_no"),
        "insurance.policies": pack(pol_hit, "vehicle_number"),
        "rto_registration.registrations": pack(reg_hit, "reg_no"),
        "theft_records.theft_shredding": pack(thf_hit, "plate_number"),
    }
    out["hits"] = sum(1 for s in out["sources"].values() if s["hit"])
    return out


def explain_decision(user_plate: str) -> dict[str, Any]:
    """Rule-trace provenance for the integration decision (same logic as ETL)."""
    match = live_match_plate(user_plate)
    key = match["canonical"]
    steps = []
    sources = match["sources"]

    theft = sources["theft_records.theft_shredding"]
    pol = sources["insurance.policies"]
    reg = sources["rto_registration.registrations"]
    cap = sources["roadcapture.capture_log"]

    steps.append({
        "step": 1,
        "rule": "Canonicalize plate",
        "detail": f"'{user_plate}' ({match['input_style']}) → '{key}'",
        "fired": True,
    })
    steps.append({
        "step": 2,
        "rule": "Match across heterogeneous schemas",
        "detail": (
            f"Hits: capture={cap['hit']}, insurance={pol['hit']}, "
            f"RTO={reg['hit']}, theft={theft['hit']} ({match['hits']}/4 sources)"
        ),
        "fired": True,
    })

    final = "OK - INSURED & CLEAR"
    if theft["hit"]:
        rtype = theft["rows"].iloc[0]["report_type"]
        final = f"STOLEN/SHREDDED -> FLAG ({rtype})"
        steps.append({
            "step": 3,
            "rule": "PRIORITY: theft overrides everything",
            "detail": f"theft_shredding.report_type = {rtype}",
            "fired": True,
        })
    else:
        steps.append({
            "step": 3,
            "rule": "PRIORITY: theft overrides everything",
            "detail": "No theft/shredding case for this plate",
            "fired": False,
        })
        if not pol["hit"]:
            final = "NO INSURANCE ON FILE -> FLAG"
            steps.append({
                "step": 4,
                "rule": "Insurance present?",
                "detail": "No matching policies.vehicle_number",
                "fired": True,
            })
        else:
            valid_to = pol["rows"].iloc[0]["valid_to"]
            if pd.notna(valid_to) and pd.to_datetime(valid_to).date() < AS_OF:
                final = f"INSURANCE EXPIRED ({valid_to}) -> FLAG"
                steps.append({
                    "step": 4,
                    "rule": "Insurance present?",
                    "detail": f"Yes — but valid_to {valid_to} < AS_OF {AS_OF}",
                    "fired": True,
                })
            else:
                steps.append({
                    "step": 4,
                    "rule": "Insurance present & valid?",
                    "detail": f"Yes — insurer={pol['rows'].iloc[0]['insurer']}, valid_to={valid_to}",
                    "fired": True,
                })
                final = "OK - INSURED & CLEAR"

    steps.append({
        "step": 5,
        "rule": "Emit integrated verdict",
        "detail": final,
        "fired": True,
    })

    return {
        "canonical": key,
        "verdict": final,
        "steps": steps,
        "match": match,
        "as_of": AS_OF,
    }


def scan_conflicts(limit_per_type: int = 25) -> dict[str, pd.DataFrame]:
    """
    Conflicts that only appear AFTER integration (innovative for IIA demos).
    """
    captures = _fetch("roadcapture", "SELECT plate_no, capture_time, location FROM capture_log")
    policies = _fetch("insurance", "SELECT vehicle_number, insurer, valid_to FROM policies")
    regs = _fetch("rto_registration", "SELECT reg_no, owner_name, make_model FROM registrations")
    thefts = _fetch("theft_records", "SELECT plate_number, report_type, report_date FROM theft_shredding")

    captures = captures.assign(k=captures["plate_no"].map(normalize_plate))
    policies = policies.assign(k=policies["vehicle_number"].map(normalize_plate))
    regs = regs.assign(k=regs["reg_no"].map(normalize_plate))
    thefts = thefts.assign(k=thefts["plate_number"].map(normalize_plate))

    # latest capture per plate
    latest = (
        captures.sort_values("capture_time")
        .groupby("k", as_index=False)
        .tail(1)
    )

    merged = latest.merge(policies, on="k", how="left", suffixes=("", "_pol"))
    merged = merged.merge(regs, on="k", how="left")
    merged = merged.merge(thefts, on="k", how="left", suffixes=("", "_thf"))

    results: dict[str, pd.DataFrame] = {}

    # 1) Insured AND stolen (business conflict — theft should win)
    c1 = merged[merged["report_type"].notna() & merged["insurer"].notna()].copy()
    if not c1.empty:
        c1["conflict"] = "Valid/listed insurance BUT theft/shredding case exists"
        results["insured_but_stolen"] = c1[
            ["k", "plate_no", "insurer", "valid_to", "report_type", "report_date", "conflict"]
        ].drop_duplicates("k").head(limit_per_type)

    # 2) Seen on road, no RTO registration
    c2 = merged[merged["owner_name"].isna()].copy()
    if not c2.empty:
        c2["conflict"] = "Captured on road but no RTO registration match"
        results["capture_without_rto"] = c2[
            ["k", "plate_no", "location", "capture_time", "conflict"]
        ].drop_duplicates("k").head(limit_per_type)

    # 3) Policy exists, never seen in capture universe of merged (orphan policy among captured set is inverse)
    # Policies whose plate never appears in captures
    cap_keys = set(captures["k"])
    orphan_pol = policies[~policies["k"].isin(cap_keys)].copy()
    if not orphan_pol.empty:
        orphan_pol["conflict"] = "Insurance row with no road-capture sighting"
        results["insurance_never_seen"] = orphan_pol[
            ["k", "vehicle_number", "insurer", "valid_to", "conflict"]
        ].drop_duplicates("k").head(limit_per_type)

    # 4) Theft phantoms — in theft DB but never captured
    orphan_thf = thefts[~thefts["k"].isin(cap_keys)].copy()
    if not orphan_thf.empty:
        orphan_thf["conflict"] = "Theft/shredding case with no road-capture sighting (phantom)"
        results["theft_phantom"] = orphan_thf[
            ["k", "plate_number", "report_type", "report_date", "conflict"]
        ].drop_duplicates("k").head(limit_per_type)

    # 5) Format diversity — same vehicle, different native strings
    format_rows = []
    for k, group in latest.groupby("k"):
        natives = []
        styles = []
        for src_df, col in (
            (captures, "plate_no"),
            (policies, "vehicle_number"),
            (regs, "reg_no"),
            (thefts, "plate_number"),
        ):
            hit = src_df[src_df["k"] == k]
            if not hit.empty:
                native = str(hit.iloc[0][col])
                natives.append(native)
                styles.append(detect_plate_style(native))
        if len(set(styles)) >= 2:
            format_rows.append({
                "canonical": k,
                "native_forms": " | ".join(dict.fromkeys(natives)),
                "styles_seen": " | ".join(dict.fromkeys(styles)),
                "n_formats": len(set(styles)),
            })
    if format_rows:
        results["format_heterogeneity"] = (
            pd.DataFrame(format_rows)
            .sort_values("n_formats", ascending=False)
            .head(limit_per_type)
        )

    return results
