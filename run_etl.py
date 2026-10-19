"""
run_etl.py -- extract, standardize, transform, decide, load, report.

Sources can be LIVE (direct MySQL/Docker) or STAGING (rows pushed via ingest_api).
See staging.EXTRACT_FROM_STAGING.
"""

import re
import time
from datetime import date, datetime

from db_config import connect
from staging import (
    EXTRACT_FROM_STAGING,
    EXTERNAL_STAGING_ONLY,
    LOCAL_LIVE_ONLY,
    ensure_staging_tables,
    fetch_staging,
)
from warehouse_ops import (
    ensure_ops_tables,
    log_changes,
    log_etl_run,
    rebuild_conflict_cache,
    rebuild_plate_xref,
    refresh_serving_tables,
)

AS_OF = date(2026, 9, 12)
SENTINEL_START = datetime(2000, 1, 1)


def fetchall(database, query, params=None):
    conn = connect(database)
    cur = conn.cursor()
    cur.execute(query, params or ())
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def normalize_plate(raw):
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def extract_captures(watermark):
    # Local: always LIVE roadcapture (ignore leftover stg_capture_log)
    if "roadcapture" in LOCAL_LIVE_ONLY or not EXTRACT_FROM_STAGING.get("roadcapture"):
        rows = fetchall(
            "roadcapture",
            "SELECT * FROM capture_log WHERE capture_time > %s",
            (watermark,),
        )
        print(f"  extract roadcapture: LIVE local ({len(rows)} rows after watermark)")
        return rows
    rows = fetch_staging("roadcapture")
    filtered = [
        r for r in rows
        if r.get("capture_time") is not None and r["capture_time"] > watermark
    ]
    print(f"  extract roadcapture: STAGING ({len(filtered)}/{len(rows)} after watermark)")
    return filtered


def extract_policies():
    # External: never fall back to local insurance DB
    if "insurance" in EXTERNAL_STAGING_ONLY or EXTRACT_FROM_STAGING.get("insurance"):
        rows = fetch_staging("insurance")
        print(f"  extract insurance: STAGING/external push ({len(rows)} rows)")
        return rows
    raise RuntimeError("insurance must be EXTERNAL_STAGING_ONLY — refusing LIVE local read")


def extract_regs():
    if "rto_registration" in EXTERNAL_STAGING_ONLY or EXTRACT_FROM_STAGING.get(
        "rto_registration"
    ):
        rows = fetch_staging("rto_registration")
        print(f"  extract rto_registration: STAGING/external push ({len(rows)} rows)")
        return rows
    raise RuntimeError(
        "rto_registration must be EXTERNAL_STAGING_ONLY — refusing LIVE local read"
    )


def extract_thefts():
    # Local: always LIVE theft Docker (ignore leftover stg_theft_shredding)
    if "theft_records" in LOCAL_LIVE_ONLY or not EXTRACT_FROM_STAGING.get("theft_records"):
        rows = fetchall("theft_records", "SELECT * FROM theft_shredding")
        print(f"  extract theft_records: LIVE local ({len(rows)} rows)")
        return rows
    rows = fetch_staging("theft_records")
    print(f"  extract theft_records: STAGING ({len(rows)} rows)")
    return rows


def get_watermark():
    wh = connect("vehicle_dwh")
    cur = wh.cursor()
    ensure_ops_tables(cur)
    ensure_staging_tables(cur)
    cur.execute(
        """CREATE TABLE IF NOT EXISTS etl_watermark (
        source_name VARCHAR(50) PRIMARY KEY, last_processed_at DATETIME)"""
    )
    cur.execute(
        "SELECT last_processed_at FROM etl_watermark WHERE source_name='capture_log'"
    )
    row = cur.fetchone()
    wh.commit()
    cur.close()
    wh.close()
    return row[0] if row else SENTINEL_START


def set_watermark(cur, new_watermark):
    cur.execute(
        """INSERT INTO etl_watermark (source_name, last_processed_at) VALUES ('capture_log', %s)
           ON DUPLICATE KEY UPDATE last_processed_at=VALUES(last_processed_at)""",
        (new_watermark,),
    )


def decide_status(plate, stolen_by_plate, policy_by_plate):
    if plate in stolen_by_plate:
        return f"STOLEN/SHREDDED -> FLAG ({stolen_by_plate[plate]['report_type']})"
    pol = policy_by_plate.get(plate)
    if not pol:
        return "NO INSURANCE ON FILE -> FLAG"
    if pol["valid_to"] < AS_OF:
        return f"INSURANCE EXPIRED ({pol['valid_to']}) -> FLAG"
    return "OK - INSURED & CLEAR"


def refresh_dimensions(wcur, policies, regs, thefts):
    """
    Re-apply insurance / RTO / theft onto every existing vehicle_360 row.
    Needed when teammates push after captures were already loaded (idle ETL).
    """
    policy_by_plate = {normalize_plate(p["vehicle_number"]): p for p in policies}
    reg_by_plate = {normalize_plate(r["reg_no"]): r for r in regs}
    stolen_by_plate = {normalize_plate(t["plate_number"]): t for t in thefts}

    wcur.execute("SELECT plate_canonical FROM vehicle_360")
    plates = [r[0] for r in wcur.fetchall()]
    if not plates:
        return 0

    updates = []
    for plate in plates:
        reg = reg_by_plate.get(plate, {})
        pol = policy_by_plate.get(plate, {})
        theft = stolen_by_plate.get(plate, {})
        updates.append(
            (
                reg.get("owner_name"),
                reg.get("make_model"),
                reg.get("registration_date"),
                pol.get("insurer"),
                pol.get("valid_to"),
                theft.get("report_type", "-"),
                decide_status(plate, stolen_by_plate, policy_by_plate),
                plate,
            )
        )
    wcur.executemany(
        """
        UPDATE vehicle_360 SET
          owner_name=%s, make_model=%s, registration_date=%s,
          insurer=%s, policy_valid_to=%s, theft_flag=%s, status=%s
        WHERE plate_canonical=%s
        """,
        updates,
    )
    return len(updates)


started = datetime.now()
t0 = time.perf_counter()
print("Ownership map:")
print("  LOCAL LIVE:     ", ", ".join(LOCAL_LIVE_ONLY))
print("  EXTERNAL PUSH:  ", ", ".join(EXTERNAL_STAGING_ONLY))
print(
    "Extract modes:",
    {k: ("STAGING/push" if v else "LIVE") for k, v in EXTRACT_FROM_STAGING.items()},
)
watermark = get_watermark()
new_captures = extract_captures(watermark)

print(f"Watermark going in: {watermark}")
print(f"New capture events since then: {len(new_captures)}")

wh = connect("vehicle_dwh")
wcur = wh.cursor()
ensure_ops_tables(wcur)
ensure_staging_tables(wcur)
wcur.execute(
    """CREATE TABLE IF NOT EXISTS vehicle_360 (
    plate_canonical VARCHAR(20) PRIMARY KEY, owner_name VARCHAR(50), make_model VARCHAR(50),
    registration_date DATE, insurer VARCHAR(50), policy_valid_to DATE,
    theft_flag VARCHAR(20), last_seen_location VARCHAR(100), last_seen_time DATETIME,
    status VARCHAR(100))"""
)

# Always pull dimension sources so xref + conflict cache stay current
# (captures stay incremental via watermark).
policies = extract_policies()
regs = extract_regs()
thefts = extract_thefts()
# Full capture set for xref (watermark only gates vehicle_360 upserts)
# Always LIVE local roadcapture — never leftover stg_capture_log
all_captures = fetchall("roadcapture", "SELECT * FROM capture_log")

if not new_captures:
    n_dim = refresh_dimensions(wcur, policies, regs, thefts)
    counts = refresh_serving_tables(wcur)
    n_xref = rebuild_plate_xref(wcur, all_captures, policies, regs, thefts, normalize_plate)
    n_conf = rebuild_conflict_cache(wcur, thefts, normalize_plate)
    finished = datetime.now()
    log_etl_run(
        wcur,
        started_at=started,
        finished_at=finished,
        watermark_in=watermark,
        watermark_out=watermark,
        capture_events=0,
        plates_upserted=n_dim,
        status_changes=0,
        note=f"idle — dims={n_dim} xref={n_xref} conflicts={n_conf}",
    )
    wh.commit()
    wcur.close()
    wh.close()
    print("Nothing new in capture_log — refreshed dimensions + serving + xref + conflicts.")
    print(f"Serving summary: {counts} | dims={n_dim} | xref={n_xref} | conflicts={n_conf}")
else:
    policy_by_plate = {normalize_plate(p["vehicle_number"]): p for p in policies}
    reg_by_plate = {normalize_plate(r["reg_no"]): r for r in regs}
    stolen_by_plate = {normalize_plate(t["plate_number"]): t for t in thefts}

    latest_by_plate = {}
    for c in new_captures:
        plate = normalize_plate(c["plate_no"])
        prev = latest_by_plate.get(plate)
        if prev is None or c["capture_time"] > prev["capture_time"]:
            latest_by_plate[plate] = c

    plates = list(latest_by_plate.keys())
    old_status = {}
    if plates:
        placeholders = ",".join(["%s"] * len(plates))
        wcur.execute(
            f"SELECT plate_canonical, status FROM vehicle_360 WHERE plate_canonical IN ({placeholders})",
            plates,
        )
        old_status = dict(wcur.fetchall())

    rows = []
    changes = []
    for plate, c in latest_by_plate.items():
        reg, pol = reg_by_plate.get(plate, {}), policy_by_plate.get(plate, {})
        theft = stolen_by_plate.get(plate, {})
        new_st = decide_status(plate, stolen_by_plate, policy_by_plate)
        rows.append(
            (
                plate,
                reg.get("owner_name"),
                reg.get("make_model"),
                reg.get("registration_date"),
                pol.get("insurer"),
                pol.get("valid_to"),
                theft.get("report_type", "-"),
                c["location"],
                c["capture_time"],
                new_st,
            )
        )
        prev = old_status.get(plate)
        if prev is None:
            changes.append((plate, "INSERT", None, new_st))
        elif prev != new_st:
            changes.append((plate, "STATUS_CHANGE", prev, new_st))

    wcur.executemany(
        """INSERT INTO vehicle_360 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          owner_name=VALUES(owner_name), make_model=VALUES(make_model),
          registration_date=VALUES(registration_date), insurer=VALUES(insurer),
          policy_valid_to=VALUES(policy_valid_to), theft_flag=VALUES(theft_flag),
          last_seen_location=VALUES(last_seen_location), last_seen_time=VALUES(last_seen_time),
          status=VALUES(status)""",
        rows,
    )

    new_watermark = max(c["capture_time"] for c in new_captures)
    set_watermark(wcur, new_watermark)

    n_dim = refresh_dimensions(wcur, policies, regs, thefts)
    counts = refresh_serving_tables(wcur)
    n_xref = rebuild_plate_xref(wcur, all_captures, policies, regs, thefts, normalize_plate)
    n_conf = rebuild_conflict_cache(wcur, thefts, normalize_plate)
    finished = datetime.now()
    run_id = log_etl_run(
        wcur,
        started_at=started,
        finished_at=finished,
        watermark_in=watermark,
        watermark_out=new_watermark,
        capture_events=len(new_captures),
        plates_upserted=len(rows),
        status_changes=len(changes),
        note=f"incremental + dims={n_dim} xref={n_xref} conflicts={n_conf}",
    )
    log_changes(wcur, run_id, changes)
    wh.commit()

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    print(f"-> {len(rows)} distinct vehicle(s) upserted into vehicle_360.")
    print(f"-> {len(changes)} change-log entries (inserts / status flips).")
    print(f"-> dimensions refreshed on {n_dim} plate(s); xref={n_xref}, conflicts={n_conf}")
    print(f"Watermark advanced to: {new_watermark}")
    print(f"ETL run_id={run_id} duration~={elapsed_ms} ms")
    print("\nServing layer (dwh_status_summary):")
    total = sum(counts.values()) or 1
    for k, v in counts.items():
        print(f"  {k:14} {v:5}  ({v / total:.1%})")

    wcur.close()
    wh.close()
