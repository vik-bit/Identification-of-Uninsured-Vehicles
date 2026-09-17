"""
warehouse_ops.py
----------------
Operational layer on top of vehicle_dwh:

1. LOGS
   - etl_run_log          every ETL run (watermark, row counts, duration)
   - dwh_change_log       status changes per plate when ETL upserts
   - access_audit_log     GUI / SQL / CRUD actions

2. EFFICIENT SERVING TABLES (maintained by ETL, read by UI)
   - dwh_status_summary   pre-aggregated KPI counts (UI does NOT scan vehicle_360)
   - dwh_flagged_cache    only non-clear vehicles for the ministry list
   - ministry_state_risk  state-wise risk rates (from plate prefix)
   - ministry_hotspots    checkpoint locations with most flags
   - ministry_insurer_gap insurers with most expired / missing among flagged
   - ministry_priority    enforcement queue: recent + severe first


This is the "why warehouse" story: fact table for point lookups,
serving tables for dashboards, logs for auditability.
"""

from __future__ import annotations

from datetime import datetime

from db_config import connect

BUCKETS = ("OK", "EXPIRED", "STOLEN", "NO INSURANCE")


def ensure_ops_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS etl_run_log (
            run_id INT PRIMARY KEY AUTO_INCREMENT,
            started_at DATETIME NOT NULL,
            finished_at DATETIME NOT NULL,
            watermark_in DATETIME NULL,
            watermark_out DATETIME NULL,
            capture_events INT NOT NULL,
            plates_upserted INT NOT NULL,
            status_changes INT NOT NULL,
            duration_ms INT NOT NULL,
            note VARCHAR(255) NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dwh_change_log (
            change_id INT PRIMARY KEY AUTO_INCREMENT,
            changed_at DATETIME NOT NULL,
            etl_run_id INT NULL,
            plate_canonical VARCHAR(20) NOT NULL,
            change_type VARCHAR(20) NOT NULL,
            old_status VARCHAR(100) NULL,
            new_status VARCHAR(100) NULL,
            INDEX idx_plate (plate_canonical),
            INDEX idx_changed (changed_at)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS access_audit_log (
            access_id INT PRIMARY KEY AUTO_INCREMENT,
            accessed_at DATETIME NOT NULL,
            actor VARCHAR(50) NOT NULL DEFAULT 'gui',
            action VARCHAR(40) NOT NULL,
            target VARCHAR(80) NOT NULL,
            detail VARCHAR(500) NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dwh_status_summary (
            bucket VARCHAR(20) PRIMARY KEY,
            vehicle_count INT NOT NULL,
            refreshed_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dwh_flagged_cache (
            plate_canonical VARCHAR(20) PRIMARY KEY,
            owner_name VARCHAR(50),
            make_model VARCHAR(50),
            insurer VARCHAR(50),
            policy_valid_to DATE,
            theft_flag VARCHAR(20),
            last_seen_location VARCHAR(100),
            last_seen_time DATETIME,
            status VARCHAR(100),
            refreshed_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ministry_state_risk (
            state_code VARCHAR(5) PRIMARY KEY,
            total_vehicles INT NOT NULL,
            stolen_cnt INT NOT NULL,
            no_insurance_cnt INT NOT NULL,
            expired_cnt INT NOT NULL,
            clear_cnt INT NOT NULL,
            risk_score DOUBLE NOT NULL,
            refreshed_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ministry_hotspots (
            location VARCHAR(100) PRIMARY KEY,
            flagged_sightings INT NOT NULL,
            stolen_cnt INT NOT NULL,
            no_insurance_cnt INT NOT NULL,
            expired_cnt INT NOT NULL,
            refreshed_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ministry_insurer_gap (
            insurer VARCHAR(50) PRIMARY KEY,
            expired_flagged INT NOT NULL,
            still_listed_but_stolen INT NOT NULL,
            refreshed_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ministry_priority (
            plate_canonical VARCHAR(20) PRIMARY KEY,
            severity_rank INT NOT NULL,
            owner_name VARCHAR(50),
            make_model VARCHAR(50),
            last_seen_location VARCHAR(100),
            last_seen_time DATETIME,
            status VARCHAR(100),
            action_hint VARCHAR(120),
            refreshed_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dwh_plate_xref (
            plate_canonical VARCHAR(20) NOT NULL,
            source_name VARCHAR(40) NOT NULL,
            native_plate VARCHAR(40) NOT NULL,
            source_pk VARCHAR(40) NULL,
            refreshed_at DATETIME NOT NULL,
            PRIMARY KEY (plate_canonical, source_name),
            INDEX idx_native (native_plate)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dwh_conflict_cache (
            conflict_id INT PRIMARY KEY AUTO_INCREMENT,
            conflict_type VARCHAR(60) NOT NULL,
            plate_canonical VARCHAR(20) NOT NULL,
            detail VARCHAR(400) NOT NULL,
            refreshed_at DATETIME NOT NULL,
            INDEX idx_type (conflict_type),
            INDEX idx_plate (plate_canonical)
        )
        """
    )


def status_bucket(status: str) -> str:
    if not status:
        return "NO INSURANCE"
    if status.startswith("OK"):
        return "OK"
    if "EXPIRED" in status:
        return "EXPIRED"
    if "STOLEN" in status:
        return "STOLEN"
    return "NO INSURANCE"


def refresh_serving_tables(cur) -> dict[str, int]:
    """Rebuild small serving tables from vehicle_360 (cheap for UI)."""
    now = datetime.now()
    cur.execute("SELECT status FROM vehicle_360")
    counts = {b: 0 for b in BUCKETS}
    for (s,) in cur.fetchall():
        counts[status_bucket(s)] += 1

    cur.execute("DELETE FROM dwh_status_summary")
    cur.executemany(
        "INSERT INTO dwh_status_summary (bucket, vehicle_count, refreshed_at) VALUES (%s,%s,%s)",
        [(b, counts[b], now) for b in BUCKETS],
    )

    cur.execute("DELETE FROM dwh_flagged_cache")
    cur.execute(
        """
        INSERT INTO dwh_flagged_cache (
            plate_canonical, owner_name, make_model, insurer, policy_valid_to,
            theft_flag, last_seen_location, last_seen_time, status, refreshed_at
        )
        SELECT plate_canonical, owner_name, make_model, insurer, policy_valid_to,
               theft_flag, last_seen_location, last_seen_time, status, %s
        FROM vehicle_360
        WHERE status NOT LIKE 'OK%%'
        """,
        (now,),
    )

    # ---- Ministry dashboard serving tables ----
    cur.execute("DELETE FROM ministry_state_risk")
    cur.execute(
        """
        INSERT INTO ministry_state_risk (
            state_code, total_vehicles, stolen_cnt, no_insurance_cnt, expired_cnt,
            clear_cnt, risk_score, refreshed_at
        )
        SELECT
            LEFT(plate_canonical, 2) AS state_code,
            COUNT(*) AS total_vehicles,
            SUM(CASE WHEN status LIKE '%%STOLEN%%' THEN 1 ELSE 0 END) AS stolen_cnt,
            SUM(CASE WHEN status LIKE '%%NO INSURANCE%%' THEN 1 ELSE 0 END) AS no_insurance_cnt,
            SUM(CASE WHEN status LIKE '%%EXPIRED%%' THEN 1 ELSE 0 END) AS expired_cnt,
            SUM(CASE WHEN status LIKE 'OK%%' THEN 1 ELSE 0 END) AS clear_cnt,
            (
                SUM(CASE WHEN status LIKE '%%STOLEN%%' THEN 3
                         WHEN status LIKE '%%NO INSURANCE%%' THEN 2
                         WHEN status LIKE '%%EXPIRED%%' THEN 1
                         ELSE 0 END)
            ) / COUNT(*) AS risk_score,
            %s
        FROM vehicle_360
        GROUP BY LEFT(plate_canonical, 2)
        """,
        (now,),
    )

    cur.execute("DELETE FROM ministry_hotspots")
    cur.execute(
        """
        INSERT INTO ministry_hotspots (
            location, flagged_sightings, stolen_cnt, no_insurance_cnt, expired_cnt, refreshed_at
        )
        SELECT
            last_seen_location,
            COUNT(*) AS flagged_sightings,
            SUM(CASE WHEN status LIKE '%%STOLEN%%' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status LIKE '%%NO INSURANCE%%' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status LIKE '%%EXPIRED%%' THEN 1 ELSE 0 END),
            %s
        FROM vehicle_360
        WHERE status NOT LIKE 'OK%%' AND last_seen_location IS NOT NULL
        GROUP BY last_seen_location
        ORDER BY flagged_sightings DESC
        LIMIT 50
        """,
        (now,),
    )

    cur.execute("DELETE FROM ministry_insurer_gap")
    cur.execute(
        """
        INSERT INTO ministry_insurer_gap (
            insurer, expired_flagged, still_listed_but_stolen, refreshed_at
        )
        SELECT
            COALESCE(insurer, '(none on file)') AS insurer,
            SUM(CASE WHEN status LIKE '%%EXPIRED%%' THEN 1 ELSE 0 END) AS expired_flagged,
            SUM(CASE WHEN status LIKE '%%STOLEN%%' AND insurer IS NOT NULL THEN 1 ELSE 0 END)
                AS still_listed_but_stolen,
            %s
        FROM vehicle_360
        WHERE status NOT LIKE 'OK%%'
        GROUP BY COALESCE(insurer, '(none on file)')
        HAVING expired_flagged > 0 OR still_listed_but_stolen > 0
        """,
        (now,),
    )

    cur.execute("DELETE FROM ministry_priority")
    cur.execute(
        """
        INSERT INTO ministry_priority (
            plate_canonical, severity_rank, owner_name, make_model,
            last_seen_location, last_seen_time, status, action_hint, refreshed_at
        )
        SELECT
            plate_canonical,
            CASE
                WHEN status LIKE '%%STOLEN%%' THEN 1
                WHEN status LIKE '%%NO INSURANCE%%' THEN 2
                WHEN status LIKE '%%EXPIRED%%' THEN 3
                ELSE 9
            END AS severity_rank,
            owner_name,
            make_model,
            last_seen_location,
            last_seen_time,
            status,
            CASE
                WHEN status LIKE '%%STOLEN%%' THEN 'Detain / alert control room'
                WHEN status LIKE '%%NO INSURANCE%%' THEN 'Issue notice — no policy'
                WHEN status LIKE '%%EXPIRED%%' THEN 'Issue lapse notice'
                ELSE 'Review'
            END AS action_hint,
            %s
        FROM vehicle_360
        WHERE status NOT LIKE 'OK%%'
        ORDER BY severity_rank ASC, last_seen_time DESC
        LIMIT 100
        """,
        (now,),
    )

    return counts


def rebuild_plate_xref(cur, captures, policies, regs, thefts, normalize_plate) -> int:
    """Keep each source's ORIGINAL plate spelling linked to plate_canonical."""
    now = datetime.now()
    cur.execute("DELETE FROM dwh_plate_xref")
    batch = []

    def add(source, native, pk, plate_key):
        if not plate_key:
            return
        batch.append((plate_key, source, str(native)[:40], None if pk is None else str(pk)[:40], now))

    for c in captures:
        add("roadcapture", c.get("plate_no"), c.get("capture_id"), normalize_plate(c.get("plate_no")))
    for p in policies:
        add("insurance", p.get("vehicle_number"), p.get("policy_id"), normalize_plate(p.get("vehicle_number")))
    for r in regs:
        add("rto_registration", r.get("reg_no"), r.get("reg_id"), normalize_plate(r.get("reg_no")))
    for t in thefts:
        add("theft_records", t.get("plate_number"), t.get("case_id"), normalize_plate(t.get("plate_number")))

    # dedupe by (canonical, source) keeping first native seen
    seen = set()
    uniq = []
    for row in batch:
        key = (row[0], row[1])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)

    if uniq:
        cur.executemany(
            """
            INSERT INTO dwh_plate_xref
              (plate_canonical, source_name, native_plate, source_pk, refreshed_at)
            VALUES (%s,%s,%s,%s,%s)
            """,
            uniq,
        )
    return len(uniq)


def rebuild_conflict_cache(cur, thefts, normalize_plate) -> int:
    """
    Full-pass conflicts (OK in ETL). UI only reads this cache.
    Ghost-plate: theft SHREDDED/STOLEN with report_date, then a later road capture.
    """
    now = datetime.now()
    cur.execute("DELETE FROM dwh_conflict_cache")
    rows = []

    theft_dates = {}
    for t in thefts or []:
        plate = normalize_plate(t.get("plate_number"))
        if not plate:
            continue
        rtype = str(t.get("report_type") or "").upper()
        if rtype not in ("STOLEN", "SHREDDED"):
            continue
        rdate = t.get("report_date")
        prev = theft_dates.get(plate)
        if prev is None or (rdate and rdate > prev[0]):
            theft_dates[plate] = (rdate, rtype)

    # Insured / listed but stolen
    cur.execute(
        """
        SELECT plate_canonical, insurer, theft_flag, status
        FROM vehicle_360
        WHERE (status LIKE '%%STOLEN%%' OR theft_flag IN ('STOLEN','SHREDDED'))
          AND insurer IS NOT NULL AND insurer <> ''
        """
    )
    for plate, insurer, theft_flag, status in cur.fetchall():
        rows.append(
            (
                "insured_but_stolen",
                plate,
                f"Insurer={insurer}; theft={theft_flag}; status={status}",
                now,
            )
        )

    # Ghost plate: theft filed, then later sighted on road
    cur.execute(
        """
        SELECT plate_canonical, last_seen_time, last_seen_location, theft_flag, status
        FROM vehicle_360
        WHERE status LIKE '%%STOLEN%%' OR theft_flag IN ('STOLEN','SHREDDED')
        """
    )
    for plate, seen_t, loc, tflag, status in cur.fetchall():
        if seen_t is None:
            continue
        tinfo = theft_dates.get(plate)
        if tinfo and tinfo[0] is not None:
            # Compare date portion of capture vs theft report_date
            seen_d = seen_t.date() if hasattr(seen_t, "date") else seen_t
            if seen_d > tinfo[0]:
                rows.append(
                    (
                        "ghost_plate_after_theft",
                        plate,
                        f"{tinfo[1]} on {tinfo[0]} but sighted later at {loc} @ {seen_t}",
                        now,
                    )
                )
        else:
            rows.append(
                (
                    "ghost_plate_after_theft",
                    plate,
                    f"Theft flag={tflag} but later sighted at {loc} @ {seen_t}",
                    now,
                )
            )

    # Capture without RTO owner
    cur.execute(
        """
        SELECT plate_canonical, last_seen_location, status
        FROM vehicle_360
        WHERE owner_name IS NULL OR owner_name = ''
        """
    )
    for plate, loc, status in cur.fetchall():
        rows.append(
            (
                "capture_without_rto",
                plate,
                f"Seen at {loc} with no RTO owner match; status={status}",
                now,
            )
        )

    if rows:
        cur.executemany(
            """
            INSERT INTO dwh_conflict_cache
              (conflict_type, plate_canonical, detail, refreshed_at)
            VALUES (%s,%s,%s,%s)
            """,
            rows,
        )
    return len(rows)


def log_etl_run(
    cur,
    *,
    started_at: datetime,
    finished_at: datetime,
    watermark_in,
    watermark_out,
    capture_events: int,
    plates_upserted: int,
    status_changes: int,
    note: str,
) -> int:
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    cur.execute(
        """
        INSERT INTO etl_run_log (
            started_at, finished_at, watermark_in, watermark_out,
            capture_events, plates_upserted, status_changes, duration_ms, note
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            started_at,
            finished_at,
            watermark_in,
            watermark_out,
            capture_events,
            plates_upserted,
            status_changes,
            duration_ms,
            note,
        ),
    )
    return cur.lastrowid


def log_changes(cur, etl_run_id: int | None, changes: list[tuple]) -> None:
    """changes: list of (plate, change_type, old_status, new_status)"""
    if not changes:
        return
    now = datetime.now()
    cur.executemany(
        """
        INSERT INTO dwh_change_log (
            changed_at, etl_run_id, plate_canonical, change_type, old_status, new_status
        ) VALUES (%s,%s,%s,%s,%s,%s)
        """,
        [(now, etl_run_id, p, t, o, n) for p, t, o, n in changes],
    )


def log_access(actor: str, action: str, target: str, detail: str = "") -> None:
    try:
        conn = connect("vehicle_dwh")
        cur = conn.cursor()
        ensure_ops_tables(cur)
        cur.execute(
            """
            INSERT INTO access_audit_log (accessed_at, actor, action, target, detail)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (datetime.now(), actor[:50], action[:40], target[:80], (detail or "")[:500]),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        # never break the GUI because logging failed
        pass


def read_summary() -> dict[str, int]:
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    ensure_ops_tables(cur)
    cur.execute("SELECT bucket, vehicle_count FROM dwh_status_summary")
    rows = dict(cur.fetchall())
    cur.close()
    conn.close()
    return {b: int(rows.get(b, 0)) for b in BUCKETS}


def fetch_log(table: str, limit: int = 100):
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    ensure_ops_tables(cur)
    cur.execute(f"SELECT * FROM `{table}` ORDER BY 1 DESC LIMIT %s", (int(limit),))
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return cols, rows
