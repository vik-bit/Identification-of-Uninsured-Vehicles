"""
run_roadcapture_db.py
---------------------
Creates/refreshes roadcapture.capture_log and prints proof.

Usage:
    cd roadcapture_db
    python run_roadcapture_db.py
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from db_config import MYSQL_DATABASE, connect, connect_server

random.seed(42)
AS_OF = date(2026, 9, 12)
N_EVENTS = 800

STATE_CODES = [
    "AN", "AP", "AR", "AS", "BR", "CH", "CG", "DD", "DL", "GA", "GJ", "HR",
    "HP", "JK", "JH", "KA", "KL", "LA", "LD", "MP", "MH", "MN", "ML", "MZ",
    "NL", "OD", "PY", "PB", "RJ", "SK", "TN", "TS", "TR", "UP", "UK", "WB",
]
COLORS = ["White", "Silver", "Black", "Red", "Blue", "Grey"]
VTYPES = ["Hatchback", "Sedan", "SUV", "MUV", "Bike"]
LOCS = [
    "NH-48 Toll Plaza", "MG Road Signal", "Airport Road Cam",
    "Ring Road Checkpoint", "City Mall Junction", "IT Park Gate",
]


def fmt_capture(state: str, rto: int, series: str, number: int) -> str:
    """Native plate format for roadcapture — compact UPPERCASE (DL5CX1234)."""
    return f"{state}{rto}{series}{number:04d}"


def random_plate(used: set) -> str:
    while True:
        state = random.choice(STATE_CODES)
        rto = random.randint(1, 50)
        series = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ", k=2))
        number = random.randint(1, 9999)
        key = (state, rto, series, number)
        if key not in used:
            used.add(key)
            return fmt_capture(state, rto, series, number)


def ensure_database() -> None:
    try:
        conn = connect_server()
        cur = conn.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_DATABASE}")
        conn.commit()
        cur.close()
        conn.close()
        print(f"[ok] database `{MYSQL_DATABASE}` exists")
    except Exception as e:
        print(f"[warn] could not CREATE DATABASE ({e})")


def ensure_schema_and_populate() -> list:
    used: set = set()
    as_of_dt = datetime(AS_OF.year, AS_OF.month, AS_OF.day, 23, 0, 0)
    rows = []
    for _ in range(N_EVENTS):
        ts = as_of_dt - timedelta(
            days=random.randint(0, 60),
            hours=random.randint(0, 23),
            minutes=random.randint(0, 59),
        )
        rows.append(
            (
                random_plate(used),
                ts,
                random.choice(LOCS),
                random.choice(COLORS),
                random.choice(VTYPES),
            )
        )

    conn = connect()
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS capture_log")
    cur.execute(
        """
        CREATE TABLE capture_log (
            capture_id     INT PRIMARY KEY AUTO_INCREMENT,
            plate_no       VARCHAR(20)  NOT NULL,
            capture_time   DATETIME     NOT NULL,
            location       VARCHAR(100) NOT NULL,
            vehicle_color  VARCHAR(20)  NULL,
            vehicle_type   VARCHAR(20)  NULL,
            INDEX idx_plate (plate_no),
            INDEX idx_time  (capture_time)
        )
        """
    )
    cur.executemany(
        "INSERT INTO capture_log (plate_no, capture_time, location, "
        "vehicle_color, vehicle_type) VALUES (%s,%s,%s,%s,%s)",
        rows,
    )
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM capture_log")
    n = cur.fetchone()[0]
    cur.execute(
        "SELECT capture_id, plate_no, capture_time, location FROM capture_log "
        "ORDER BY capture_time DESC LIMIT 5"
    )
    sample = cur.fetchall()
    cur.close()
    conn.close()
    print(f"[ok] {n} rows in capture_log")
    print("Sample (UPPERCASE compact plates):")
    for r in sample:
        print(" ", r)
    return sample


def main():
    print("=== roadcapture_db populate ===")
    ensure_database()
    ensure_schema_and_populate()
    print("Done. ETL reads LIVE from 127.0.0.1:3307 (see repo db_config.py).")
    print("Optional push to ingest: python push_my_roadcapture.py (repo root)")


if __name__ == "__main__":
    main()
