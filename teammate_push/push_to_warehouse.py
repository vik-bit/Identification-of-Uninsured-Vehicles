"""
push_to_warehouse.py
--------------------
Run on the SOURCE owner's machine (teammate).

Reads YOUR local Docker/MySQL source and POSTs rows to the warehouse ingest API.
You do not open your MySQL to the internet — only outbound HTTP to the integrator.

Usage:
    pip install mysql-connector-python requests
    python push_to_warehouse.py

Edit the CONFIG section below before running.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import mysql.connector
import requests

# ===================== EDIT THIS =====================
WAREHOUSE_API = "http://127.0.0.1:8080"   # integrator Tailscale/LAN IP later, e.g. http://100.x.x.x:8080
API_KEY = "iia-ingest-key"

# Your local source DB (Docker on YOUR machine)
SOURCE_NAME = "insurance"          # insurance | roadcapture | rto_registration | theft_records
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 3308                  # insurance guide uses 3308
LOCAL_USER = "iia_app"
LOCAL_PASSWORD = "iia_app_pw"
LOCAL_DATABASE = "insurance"
LOCAL_TABLE = "policies"
SENDER_NAME = "teammate-insurance"
# =====================================================

TABLE_SQL = {
    "insurance": "SELECT policy_id, vehicle_number, insurer, valid_from, valid_to FROM policies",
    "roadcapture": "SELECT capture_id, plate_no, capture_time, location, vehicle_color, vehicle_type FROM capture_log",
    "rto_registration": "SELECT reg_id, reg_no, owner_name, make_model, registration_date FROM registrations",
    "theft_records": "SELECT case_id, plate_number, report_type, report_date FROM theft_shredding",
}


def json_safe(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat(sep=" ", timespec="seconds") if isinstance(obj, datetime) else obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


def main():
    sql = TABLE_SQL.get(SOURCE_NAME)
    if not sql:
        raise SystemExit(f"Unknown SOURCE_NAME={SOURCE_NAME}")

    print(f"Reading local {LOCAL_DATABASE}.{LOCAL_TABLE} @ {LOCAL_HOST}:{LOCAL_PORT} ...")
    conn = mysql.connector.connect(
        host=LOCAL_HOST, port=LOCAL_PORT,
        user=LOCAL_USER, password=LOCAL_PASSWORD, database=LOCAL_DATABASE,
    )
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, (json_safe(v) for v in row))) for row in cur.fetchall()]
    cur.close()
    conn.close()
    print(f"  loaded {len(rows)} rows")

    url = f"{WAREHOUSE_API.rstrip('/')}/ingest/{SOURCE_NAME}"
    print(f"POST {url}")
    resp = requests.post(
        url,
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        data=json.dumps({"rows": rows, "sender": SENDER_NAME, "note": "push from source owner machine"}),
        timeout=120,
    )
    print(f"  status {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except Exception:
        print(resp.text)
    if resp.status_code >= 400:
        raise SystemExit("Push failed")


if __name__ == "__main__":
    main()
