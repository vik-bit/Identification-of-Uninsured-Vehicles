"""
seed_demo_ghost_plate.py
------------------------
Viva demo: one plate that no single source can explain alone.

Seeds LIVE sources + staging mirrors (ETL currently reads insurance/theft from staging).
Capture time is set to NOW so the watermark ETL actually upserts vehicle_360.

Then run:  python run_etl.py
Open dossier for DL9CAF9999 -> Conflicts shows ghost_plate_after_theft.
"""

from __future__ import annotations

from datetime import date, datetime

from db_config import connect
from staging import ensure_staging_tables

PLATE_CANONICAL = "DL9CAF9999"
NATIVE = {
    "theft": "DL 9 CAF 9999",
    "capture": "DL-9-CAF-9999",
    "rto": "DL9CAF9999",
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
    print(f"  theft_records LIVE: SHREDDED {NATIVE['theft']} @ 2025-01-10")


def upsert_theft_staging():
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    ensure_staging_tables(cur)
    cur.execute(
        "DELETE FROM stg_theft_shredding WHERE plate_number = %s",
        (NATIVE["theft"],),
    )
    cur.execute(
        "INSERT INTO stg_theft_shredding "
        "(case_id, plate_number, report_type, report_date, ingested_at) "
        "VALUES (%s,%s,%s,%s,%s)",
        (900001, NATIVE["theft"], "SHREDDED", date(2025, 1, 10), datetime.now()),
    )
    conn.commit()
    cur.close()
    conn.close()
    print("  stg_theft_shredding: mirrored for ETL staging path")


def upsert_capture():
    # Must be AFTER current watermark or incremental ETL skips the upsert.
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
    print(f"  roadcapture: sighted {NATIVE['capture']} @ {when}")


def upsert_rto():
    conn = connect("rto_registration")
    cur = conn.cursor()
    cur.execute("DELETE FROM registrations WHERE reg_no = %s", (NATIVE["rto"],))
    cur.execute(
        "INSERT INTO registrations (reg_no, owner_name, make_model, registration_date) "
        "VALUES (%s, %s, %s, %s)",
        (NATIVE["rto"], "Demo Ghost Owner", "Tata Nexon", date(2022, 3, 15)),
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"  rto_registration: {NATIVE['rto']}")


def upsert_insurance_live():
    conn = connect("insurance")
    cur = conn.cursor()
    cur.execute("DELETE FROM policies WHERE vehicle_number = %s", (NATIVE["insurance"],))
    cur.execute(
        "INSERT INTO policies (vehicle_number, insurer, valid_from, valid_to) "
        "VALUES (%s, %s, %s, %s)",
        (NATIVE["insurance"], "DemoSecure", date(2025, 1, 1), date(2027, 1, 1)),
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"  insurance LIVE: {NATIVE['insurance']}")


def upsert_insurance_staging():
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
        (900001, NATIVE["insurance"], "DemoSecure", date(2025, 1, 1), date(2027, 1, 1), datetime.now()),
    )
    conn.commit()
    cur.close()
    conn.close()
    print("  stg_policies: mirrored for ETL staging path")


def main():
    print(f"Seeding ghost-plate demo for canonical {PLATE_CANONICAL}")
    upsert_theft_live()
    upsert_theft_staging()
    upsert_capture()
    upsert_rto()
    upsert_insurance_live()
    upsert_insurance_staging()
    print()
    print("Next:")
    print("  1. python run_etl.py")
    print("  2. streamlit run app.py -> Vehicle dossier -> Open DL9CAF9999")
    print("  3. Conflicts tab should list ghost_plate_after_theft (+ insured_but_stolen)")
    print()
    print("Talking point: theft DB alone has SHREDDED; capture DB alone has a sighting;")
    print("only the integrated warehouse joins them into one finding.")


if __name__ == "__main__":
    main()
