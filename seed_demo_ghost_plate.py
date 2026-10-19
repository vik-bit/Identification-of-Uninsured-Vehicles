"""
seed_demo_ghost_plate.py
------------------------
Viva demo: one plate that no single source can explain alone.

Matches warehouse ownership:
  LOCAL LIVE  → theft_records, roadcapture
  EXTERNAL    → insurance + rto only via staging (stg_*), not local MySQL

Then:  python run_etl.py
"""

from __future__ import annotations

from datetime import date, datetime

from db_config import connect
from staging import ensure_staging_tables

PLATE_CANONICAL = "DL9CAF9999"
NATIVE = {
    "theft": "DL 9 CAF 9999",
    "capture": "DL9CAF9999",  # roadcapture UPPERCASE compact
    "rto": "dl9caf9999",
    "insurance": "DL.9.CAF.9999",
}


def upsert_theft_live():
    conn = connect("theft_records")
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM theft_shredding WHERE plate_number = %s",
        (NATIVE["theft"],),
    )
    cur.execute(
        "INSERT INTO theft_shredding (plate_number, report_type, report_date) "
        "VALUES (%s, %s, %s)",
        (NATIVE["theft"], "SHREDDED", date(2025, 1, 10)),
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"  LOCAL theft_records: SHREDDED {NATIVE['theft']} @ 2025-01-10")


def upsert_capture():
    when = datetime.now()
    conn = connect("roadcapture")
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM capture_log WHERE plate_no = %s",
        (NATIVE["capture"],),
    )
    cur.execute(
        "INSERT INTO capture_log (plate_no, capture_time, location, vehicle_color, vehicle_type) "
        "VALUES (%s, %s, %s, %s, %s)",
        (NATIVE["capture"], when, "NH-48 Toll Plaza (ghost demo)", "Black", "SUV"),
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"  LOCAL roadcapture: sighted {NATIVE['capture']} @ {when}")


def upsert_rto_staging():
    """External RTO path — warehouse staging only (not local rto_registration)."""
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    ensure_staging_tables(cur)
    cur.execute(
        "DELETE FROM stg_registrations WHERE reg_no = %s",
        (NATIVE["rto"],),
    )
    cur.execute(
        "INSERT INTO stg_registrations "
        "(reg_id, reg_no, owner_name, make_model, registration_date, ingested_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            900001,
            NATIVE["rto"],
            "Demo Ghost Owner",
            "Tata Nexon",
            date(2022, 3, 15),
            datetime.now(),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"  EXTERNAL stg_registrations: {NATIVE['rto']}")


def upsert_insurance_staging():
    """External insurance path — warehouse staging only."""
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    ensure_staging_tables(cur)
    cur.execute(
        "DELETE FROM stg_policies WHERE vehicle_number = %s",
        (NATIVE["insurance"],),
    )
    cur.execute(
        "INSERT INTO stg_policies "
        "(policy_id, vehicle_number, insurer, valid_from, valid_to, ingested_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            900001,
            NATIVE["insurance"],
            "DemoSecure",
            date(2025, 1, 1),
            date(2027, 1, 1),
            datetime.now(),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"  EXTERNAL stg_policies: {NATIVE['insurance']}")


def main():
    print(f"Seeding ghost-plate demo for canonical {PLATE_CANONICAL}")
    upsert_theft_live()
    upsert_capture()
    upsert_rto_staging()
    upsert_insurance_staging()
    print()
    print("Next:")
    print("  1. python run_etl.py")
    print("  2. streamlit run app.py -> Records -> plate DL9CAF9999")
    print()
    print("Talking point: theft DB alone has SHREDDED; capture alone has a sighting;")
    print("only the integrated warehouse joins them (with pushed RTO/insurance).")


if __name__ == "__main__":
    main()
