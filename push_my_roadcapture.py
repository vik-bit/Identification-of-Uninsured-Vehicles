"""Push YOUR roadcapture Docker (:3307) → local ingest API → warehouse staging."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import mysql.connector
import requests

API = "http://127.0.0.1:8080"
API_KEY = "iia-ingest-key"


def safe(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def main():
    conn = mysql.connector.connect(
        host="127.0.0.1",
        port=3307,
        user="iia_app",
        password="iia_app_pw",
        database="roadcapture",
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT capture_id, plate_no, capture_time, location, vehicle_color, vehicle_type "
        "FROM capture_log"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, (safe(x) for x in r))) for r in cur.fetchall()]
    cur.close()
    conn.close()
    print(f"Read {len(rows)} rows from roadcapture Docker")

    r = requests.post(
        f"{API}/ingest/roadcapture",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        data=json.dumps(
            {
                "rows": rows,
                "sender": "local-roadcapture-docker",
                "note": "push from iia-roadcapture-mysql",
            }
        ),
        timeout=120,
    )
    print(r.status_code, r.text)
    r.raise_for_status()


if __name__ == "__main__":
    main()
