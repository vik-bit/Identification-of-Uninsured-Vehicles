"""
ministry_ops.py
---------------
Source (5): Ministry reporting DB + hash-chained audit.
Also helpers for plate xref lookup used by the dossier UI.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from db_config import connect

GENESIS_HASH = "0" * 64


def ensure_ministry_tables() -> None:
    # Create DB if missing (needs privilege; ignore if already exists / denied)
    try:
        conn = connect("vehicle_dwh")
        cur = conn.cursor()
        cur.execute("CREATE DATABASE IF NOT EXISTS ministry")
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

    conn = connect("ministry")
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ministry_reports (
            report_id        INT PRIMARY KEY AUTO_INCREMENT,
            created_at       DATETIME NOT NULL,
            officer_id       VARCHAR(50) NOT NULL,
            plate_canonical  VARCHAR(20) NOT NULL,
            action_code      VARCHAR(40) NOT NULL,
            notes            VARCHAR(500) NULL,
            warehouse_status VARCHAR(100) NULL,
            source_snapshot  JSON NULL,
            INDEX idx_plate (plate_canonical),
            INDEX idx_created (created_at)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ministry_report_audit (
            audit_id     INT PRIMARY KEY AUTO_INCREMENT,
            report_id    INT NOT NULL,
            recorded_at  DATETIME NOT NULL,
            payload_json TEXT NOT NULL,
            prev_hash    CHAR(64) NOT NULL,
            entry_hash   CHAR(64) NOT NULL,
            INDEX idx_report (report_id)
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def _last_audit_hash(cur) -> str:
    cur.execute("SELECT entry_hash FROM ministry_report_audit ORDER BY audit_id DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else GENESIS_HASH


def file_ministry_report(
    *,
    officer_id: str,
    plate_canonical: str,
    action_code: str,
    notes: str = "",
    warehouse_status: str = "",
    source_snapshot: dict | None = None,
) -> dict[str, Any]:
    """Write a ministry report + append hash-chained audit entry. Returns ids/hashes."""
    ensure_ministry_tables()
    now = datetime.now()
    snap = json.dumps(source_snapshot or {}, default=str)

    conn = connect("ministry")
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ministry_reports (
            created_at, officer_id, plate_canonical, action_code,
            notes, warehouse_status, source_snapshot
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (now, officer_id[:50], plate_canonical[:20], action_code[:40],
         (notes or "")[:500], (warehouse_status or "")[:100], snap),
    )
    report_id = cur.lastrowid

    payload = {
        "report_id": report_id,
        "created_at": now.isoformat(sep=" ", timespec="seconds"),
        "officer_id": officer_id,
        "plate_canonical": plate_canonical,
        "action_code": action_code,
        "notes": notes,
        "warehouse_status": warehouse_status,
    }
    payload_json = json.dumps(payload, sort_keys=True, default=str)
    prev = _last_audit_hash(cur)
    entry = hashlib.sha256((prev + payload_json).encode("utf-8")).hexdigest()
    cur.execute(
        """
        INSERT INTO ministry_report_audit (
            report_id, recorded_at, payload_json, prev_hash, entry_hash
        ) VALUES (%s,%s,%s,%s,%s)
        """,
        (report_id, now, payload_json, prev, entry),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"report_id": report_id, "prev_hash": prev, "entry_hash": entry}


def list_reports_for_plate(plate_canonical: str, limit: int = 20) -> list[dict]:
    ensure_ministry_tables()
    conn = connect("ministry")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT report_id, created_at, officer_id, action_code, notes, warehouse_status
        FROM ministry_reports
        WHERE plate_canonical = %s
        ORDER BY report_id DESC LIMIT %s
        """,
        (plate_canonical, int(limit)),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def verify_audit_chain(limit: int = 500) -> dict[str, Any]:
    """Recompute hashes; returns ok + first break if any."""
    ensure_ministry_tables()
    conn = connect("ministry")
    cur = conn.cursor()
    cur.execute(
        "SELECT audit_id, payload_json, prev_hash, entry_hash "
        "FROM ministry_report_audit ORDER BY audit_id ASC LIMIT %s",
        (int(limit),),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    prev = GENESIS_HASH
    for audit_id, payload_json, prev_hash, entry_hash in rows:
        if prev_hash != prev:
            return {"ok": False, "break_at": audit_id, "reason": "prev_hash mismatch"}
        expect = hashlib.sha256((prev_hash + payload_json).encode("utf-8")).hexdigest()
        if expect != entry_hash:
            return {"ok": False, "break_at": audit_id, "reason": "entry_hash mismatch"}
        prev = entry_hash
    return {"ok": True, "entries_checked": len(rows)}
