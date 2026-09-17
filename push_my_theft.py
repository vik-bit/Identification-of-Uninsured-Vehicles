"""Push YOUR theft Docker (:3310) → local ingest API → warehouse staging."""

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
        host="127.0.0.1", port=3310,
        user="iia_app", password="iia_app_pw", database="theft_records",
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT case_id, plate_number, report_type, report_date FROM theft_shredding"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, (safe(x) for x in r))) for r in cur.fetchall()]
    cur.close()
    conn.close()
    print(f"Read {len(rows)} rows from theft Docker")

    r = requests.post(
        f"{API}/ingest/theft_records",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        data=json.dumps({
            "rows": rows,
            "sender": "local-theft-docker",
            "note": "push from iia-theft-mysql",
        }),
        timeout=120,
    )
    print(r.status_code, r.text)
    r.raise_for_status()


if __name__ == "__main__":
    main()
