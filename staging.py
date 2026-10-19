"""
staging.py — landing tables inside vehicle_dwh for PUSH integration.

Teammates (and you) POST rows to ingest_api.py → these tables.
ETL can read from staging instead of connecting to their MySQL.
"""

from __future__ import annotations

from datetime import date, datetime

from db_config import connect

# ---------------------------------------------------------------------------
# Source ownership for THIS warehouse machine
# ---------------------------------------------------------------------------
# Local (LIVE MySQL on this PC)     → roadcapture, theft_records
# External teammates (PUSH only)    → insurance, rto_registration
#
# True  = ETL reads stg_* in vehicle_dwh (ignore any local copy of that DB)
# False = ETL connects live to db_config.SOURCES[name]
EXTRACT_FROM_STAGING = {
    "roadcapture": False,       # YOU — local MySQL
    "theft_records": False,     # YOU — Docker :3310
    "insurance": True,          # TEAMMATE — push → stg_policies only
    "rto_registration": True,   # TEAMMATE — push → stg_registrations only
}

# Hard rule used by ETL: never fall back to LIVE for external sources
EXTERNAL_STAGING_ONLY = ("insurance", "rto_registration")
LOCAL_LIVE_ONLY = ("roadcapture", "theft_records")


STAGING_TABLE = {
    "roadcapture": "stg_capture_log",
    "insurance": "stg_policies",
    "rto_registration": "stg_registrations",
    "theft_records": "stg_theft_shredding",
}

# column order for inserts (must match push payload / live SELECT *)
STAGING_COLUMNS = {
    "roadcapture": [
        "capture_id", "plate_no", "capture_time", "location",
        "vehicle_color", "vehicle_type",
    ],
    "insurance": [
        "policy_id", "vehicle_number", "insurer", "valid_from", "valid_to",
    ],
    "rto_registration": [
        "reg_id", "reg_no", "owner_name", "make_model", "registration_date",
    ],
    "theft_records": [
        "case_id", "plate_number", "report_type", "report_date",
    ],
}


def ensure_staging_tables(cur=None) -> None:
    own = cur is None
    if own:
        conn = connect("vehicle_dwh")
        cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stg_capture_log (
            capture_id INT NULL,
            plate_no VARCHAR(20),
            capture_time DATETIME,
            location VARCHAR(100),
            vehicle_color VARCHAR(20),
            vehicle_type VARCHAR(20),
            ingested_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stg_policies (
            policy_id INT NULL,
            vehicle_number VARCHAR(20),
            insurer VARCHAR(50),
            valid_from DATE,
            valid_to DATE,
            ingested_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stg_registrations (
            reg_id INT NULL,
            reg_no VARCHAR(20),
            owner_name VARCHAR(50),
            make_model VARCHAR(50),
            registration_date DATE,
            ingested_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stg_theft_shredding (
            case_id INT NULL,
            plate_number VARCHAR(20),
            report_type VARCHAR(20),
            report_date DATE,
            ingested_at DATETIME NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_push_log (
            push_id INT PRIMARY KEY AUTO_INCREMENT,
            received_at DATETIME NOT NULL,
            source_name VARCHAR(50) NOT NULL,
            row_count INT NOT NULL,
            sender VARCHAR(100) NULL,
            note VARCHAR(255) NULL
        )
        """
    )

    if own:
        conn.commit()
        cur.close()
        conn.close()


def _parse_value(col: str, val):
    if val is None or val == "":
        return None
    if col.endswith("_time") or col == "capture_time":
        if isinstance(val, datetime):
            return val
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(val)[:19].replace("T", " "), fmt if fmt != "%Y-%m-%d" else "%Y-%m-%d")
            except ValueError:
                continue
        return val
    if col.endswith("_date") or col in ("valid_from", "valid_to", "registration_date", "report_date"):
        if isinstance(val, date) and not isinstance(val, datetime):
            return val
        if isinstance(val, datetime):
            return val.date()
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    return val


def replace_staging(source_name: str, rows: list[dict], sender: str = "", note: str = "") -> int:
    if source_name not in STAGING_TABLE:
        raise ValueError(f"Unknown source: {source_name}")
    table = STAGING_TABLE[source_name]
    cols = STAGING_COLUMNS[source_name]
    now = datetime.now()

    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    ensure_staging_tables(cur)
    cur.execute(f"DELETE FROM `{table}`")

    if rows:
        placeholders = ", ".join(["%s"] * (len(cols) + 1))
        col_sql = ", ".join(f"`{c}`" for c in cols) + ", ingested_at"
        sql = f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders})"
        batch = []
        for r in rows:
            batch.append(tuple(_parse_value(c, r.get(c)) for c in cols) + (now,))
        cur.executemany(sql, batch)

    cur.execute(
        """
        INSERT INTO ingest_push_log (received_at, source_name, row_count, sender, note)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (now, source_name, len(rows), sender[:100], (note or "")[:255]),
    )
    conn.commit()
    cur.close()
    conn.close()
    return len(rows)


def fetch_staging(source_name: str) -> list[dict]:
    table = STAGING_TABLE[source_name]
    cols = STAGING_COLUMNS[source_name]
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    ensure_staging_tables(cur)
    col_sql = ", ".join(f"`{c}`" for c in cols)
    cur.execute(f"SELECT {col_sql} FROM `{table}`")
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows
