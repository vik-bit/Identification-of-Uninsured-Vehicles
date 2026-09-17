"""
ingest_api.py — PUSH gateway into your warehouse machine.

Teammates keep MySQL on THEIR laptop. They run push_to_warehouse.py,
which POSTs rows here. You never open a connection into their network.

Run on the warehouse machine:
    pip install flask
    python ingest_api.py

Default: http://0.0.0.0:8080  (reachable if firewall / Tailscale / same Wi‑Fi allows)
API key header: X-API-Key: iia-ingest-key
"""

from __future__ import annotations

from flask import Flask, jsonify, request

from staging import STAGING_TABLE, ensure_staging_tables, replace_staging

API_KEY = "iia-ingest-key"
HOST = "0.0.0.0"
PORT = 8080

app = Flask(__name__)


def _authorized() -> bool:
    return request.headers.get("X-API-Key", "") == API_KEY


@app.get("/health")
def health():
    return jsonify({"ok": True, "role": "warehouse-ingest", "sources": list(STAGING_TABLE)})


@app.post("/ingest/<source_name>")
def ingest(source_name: str):
    if not _authorized():
        return jsonify({"error": "unauthorized — set header X-API-Key"}), 401
    if source_name not in STAGING_TABLE:
        return jsonify({"error": f"unknown source; use one of {list(STAGING_TABLE)}"}), 400

    body = request.get_json(silent=True) or {}
    rows = body.get("rows")
    if not isinstance(rows, list):
        return jsonify({"error": "JSON body must be {\"rows\": [ {...}, ... ] }"}), 400

    sender = body.get("sender", request.remote_addr or "")
    note = body.get("note", "")
    try:
        n = replace_staging(source_name, rows, sender=str(sender), note=str(note))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "source": source_name,
        "rows_ingested": n,
        "staging_table": STAGING_TABLE[source_name],
        "next_step": "On warehouse PC run: python run_etl.py",
    })


@app.get("/ingest/log")
def ingest_log():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    from db_config import connect

    ensure_staging_tables()
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    cur.execute(
        "SELECT push_id, received_at, source_name, row_count, sender, note "
        "FROM ingest_push_log ORDER BY push_id DESC LIMIT 50"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify({"pushes": rows})


if __name__ == "__main__":
    ensure_staging_tables()
    print(f"Ingest API listening on http://{HOST}:{PORT}")
    print(f"API key: {API_KEY}")
    print("Example: POST /ingest/insurance  with header X-API-Key")
    app.run(host=HOST, port=PORT, debug=False)
