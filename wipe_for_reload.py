"""
wipe_for_reload.py
------------------
Wipe warehouse (+ optional local Docker sources) so you can reload from the
new ownership setup:

  LOCAL LIVE  → roadcapture (:3307), theft_records (:3310)
  EXTERNAL    → insurance / rto only via staging after teammate push

Usage (from repo root):

  # 1) Wipe warehouse integrated tables + staging + ETL watermark/logs
  python wipe_for_reload.py

  # 2) Also empty YOUR local Docker sources (roadcapture + theft)
  python wipe_for_reload.py --local-sources

  # 3) Dry-run (print what would be wiped)
  python wipe_for_reload.py --local-sources --dry-run

Then reload:
  cd roadcapture_db && python run_roadcapture_db.py && cd ..
  cd theft_records_db && python run_theft_db.py && cd ..
  # teammates push insurance + rto  OR  seed demo into staging:
  python seed_demo_ghost_plate.py   # optional
  python run_etl.py
  streamlit run app.py
"""

from __future__ import annotations

import argparse
import sys

from db_config import connect

# Tables inside vehicle_dwh that the UI / ETL depend on
WAREHOUSE_TABLES = [
    # fact + serving
    "vehicle_360",
    "dwh_status_summary",
    "dwh_flagged_cache",
    "dwh_plate_xref",
    "dwh_conflict_cache",
    "ministry_state_risk",
    "ministry_hotspots",
    "ministry_insurer_gap",
    "ministry_priority",
    # staging (external + optional local pushes)
    "stg_capture_log",
    "stg_policies",
    "stg_registrations",
    "stg_theft_shredding",
    "ingest_push_log",
    # ETL bookkeeping (forces full re-process of captures)
    "etl_watermark",
    "etl_run_log",
    "dwh_change_log",
    "access_audit_log",
]


def wipe_table(cur, name: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] TRUNCATE/DELETE {name}")
        return
    try:
        cur.execute(f"TRUNCATE TABLE `{name}`")
        print(f"  truncated {name}")
    except Exception:
        try:
            cur.execute(f"DELETE FROM `{name}`")
            print(f"  deleted all rows from {name}")
        except Exception as e:
            print(f"  skip {name}: {e}")


def wipe_warehouse(dry_run: bool) -> None:
    print("=== vehicle_dwh ===")
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    # Disable FK checks if any (usually none)
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for t in WAREHOUSE_TABLES:
        wipe_table(cur, t, dry_run)
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    if not dry_run:
        conn.commit()
    cur.close()
    conn.close()


def wipe_local_sources(dry_run: bool) -> None:
    print("=== LOCAL sources (your Docker) ===")
    # roadcapture :3307
    try:
        conn = connect("roadcapture")
        cur = conn.cursor()
        wipe_table(cur, "capture_log", dry_run)
        if not dry_run:
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  roadcapture wipe failed: {e}")

    # theft :3310
    try:
        conn = connect("theft_records")
        cur = conn.cursor()
        wipe_table(cur, "theft_shredding", dry_run)
        if not dry_run:
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  theft_records wipe failed: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Wipe warehouse for clean reload")
    ap.add_argument(
        "--local-sources",
        action="store_true",
        help="Also wipe roadcapture.capture_log and theft_records.theft_shredding",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions only; do not delete",
    )
    ap.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirm",
    )
    args = ap.parse_args()

    print("This will wipe integrated warehouse data used by the UI.")
    if args.local_sources:
        print("Also wiping LOCAL Docker tables: capture_log, theft_shredding.")
    print("Does NOT wipe teammate insurance/RTO Docker DBs (external).")
    print("Does NOT wipe ministry DB.")

    if not args.yes and not args.dry_run:
        ans = input("Type YES to continue: ").strip()
        if ans != "YES":
            print("Aborted.")
            sys.exit(1)

    wipe_warehouse(args.dry_run)
    if args.local_sources:
        wipe_local_sources(args.dry_run)

    print()
    print("Done." if not args.dry_run else "Dry-run done.")
    if not args.dry_run:
        print(
            "\nReload order:\n"
            "  1. cd roadcapture_db && python run_roadcapture_db.py && cd ..\n"
            "  2. cd theft_records_db && python run_theft_db.py && cd ..\n"
            "  3. Teammates: push insurance + rto  (or python seed_demo_ghost_plate.py)\n"
            "  4. python run_etl.py\n"
            "  5. streamlit run app.py\n"
        )


if __name__ == "__main__":
    main()
