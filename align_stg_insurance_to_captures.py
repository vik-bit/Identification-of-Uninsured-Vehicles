"""
align_stg_insurance_to_captures.py
----------------------------------
Insurance pushed with random plates often won't match roadcapture plates, so
vehicle_360.insurer stays empty and Insurer gaps stays blank.

This rewrites stg_policies to cover a share of LIVE capture plates
(hyphen format), including some expired — then run_etl fills insurer gaps.

Usage:
    python align_stg_insurance_to_captures.py
    python run_etl.py
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from db_config import connect
from staging import ensure_staging_tables

random.seed(7)
AS_OF = date(2026, 9, 12)
COVER_SHARE = 0.85
EXPIRED_SHARE = 0.20

INSURERS = [
    "ICICI Lombard", "HDFC Ergo", "Bajaj Allianz", "New India Assurance",
    "Go Digit", "Acko", "Tata AIG", "SBI General",
]


def to_insurance_plate(compact: str) -> str:
    """AN14YZ1923 -> AN-14-YZ-1923 (best-effort for project formats)."""
    s = "".join(ch for ch in compact.upper() if ch.isalnum())
    # state 2 letters, then digits (rto), then 2 letters, then 4 digits
    import re

    m = re.match(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$", s)
    if not m:
        return compact
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}-{m.group(4)}"


def main() -> None:
    rc = connect("roadcapture")
    cur = rc.cursor()
    cur.execute("SELECT DISTINCT plate_no FROM capture_log")
    plates = [r[0] for r in cur.fetchall()]
    cur.close()
    rc.close()
    if not plates:
        raise SystemExit("No capture_log plates — populate roadcapture_db first.")

    random.shuffle(plates)
    n_cover = max(1, int(len(plates) * COVER_SHARE))
    chosen = plates[:n_cover]

    rows = []
    for i, plate in enumerate(chosen, start=1):
        term = timedelta(days=365)
        if random.random() < EXPIRED_SHARE:
            valid_to = AS_OF - timedelta(days=random.randint(1, 200))
        else:
            valid_to = AS_OF + timedelta(days=random.randint(10, 400))
        valid_from = valid_to - term
        rows.append(
            (
                i,
                to_insurance_plate(plate),
                random.choice(INSURERS),
                valid_from,
                valid_to,
                datetime.now(),
            )
        )

    wh = connect("vehicle_dwh")
    cur = wh.cursor()
    ensure_staging_tables(cur)
    cur.execute("DELETE FROM stg_policies")
    cur.executemany(
        "INSERT INTO stg_policies "
        "(policy_id, vehicle_number, insurer, valid_from, valid_to, ingested_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        rows,
    )
    wh.commit()
    cur.execute("SELECT COUNT(*) FROM stg_policies")
    n = cur.fetchone()[0]
    cur.close()
    wh.close()
    print(f"Wrote {n} stg_policies aligned to capture plates ({EXPIRED_SHARE:.0%} expired).")
    print("Next: python run_etl.py  → Analytics → Insurer gaps should fill.")


if __name__ == "__main__":
    main()
