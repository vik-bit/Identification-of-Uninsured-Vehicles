"""
run_theft_db.py
---------------
ONE script for this folder: creates/refreshes theft_records.theft_shredding
and prints live proof that the DB is populated.

Usage (from anywhere, with MySQL running and iia_app set up):

    cd theft_records_db
    python run_theft_db.py

Or from repo root:

    python theft_records_db/run_theft_db.py
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from db_config import MYSQL_DATABASE, connect, connect_server

random.seed(42)
AS_OF = date(2026, 9, 12)
N_FLAGGED = 225
N_PHANTOM = 3

STATE_CODES = [
    "AN", "AP", "AR", "AS", "BR", "CH", "CG", "DD", "DL", "GA", "GJ", "HR",
    "HP", "JK", "JH", "KA", "KL", "LA", "LD", "MP", "MH", "MN", "ML", "MZ",
    "NL", "OD", "PY", "PB", "RJ", "SK", "TN", "TS", "TR", "UP", "UK", "WB",
]
REPORT_TYPES = ["STOLEN", "SHREDDED"]


def fmt_theft(state: str, rto: int, series: str, number: int) -> str:
    """Native plate format for this source — spaces between parts."""
    return f"{state} {rto} {series} {number:04d}"


def random_plate(used: set) -> str:
    while True:
        state = random.choice(STATE_CODES)
        rto = random.randint(1, 50)
        series = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ", k=2))
        number = random.randint(1, 9999)
        key = (state, rto, series, number)
        if key not in used:
            used.add(key)
            return fmt_theft(state, rto, series, number)


def ensure_database() -> None:
    """Create theft_records DB if missing (needs privilege; else setup_mysql.sql)."""
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
        print("       If this fails later, run repo-root setup_mysql.sql as root once.")


def ensure_schema_and_populate() -> list:
    used: set = set()
    rows = []
    for _ in range(N_FLAGGED + N_PHANTOM):
        rows.append(
            (
                random_plate(used),
                random.choice(REPORT_TYPES),
                AS_OF - timedelta(days=random.randint(5, 500)),
            )
        )

    conn = connect()
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS theft_shredding")
    cur.execute(
        """
        CREATE TABLE theft_shredding (
            case_id      INT PRIMARY KEY AUTO_INCREMENT,
            plate_number VARCHAR(20) NOT NULL,
            report_type  VARCHAR(20) NOT NULL,
            report_date  DATE        NOT NULL,
            INDEX idx_plate (plate_number),
            INDEX idx_type  (report_type)
        )
        """
    )
    cur.executemany(
        "INSERT INTO theft_shredding (plate_number, report_type, report_date) "
        "VALUES (%s, %s, %s)",
        rows,
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"[ok] table theft_shredding created and loaded with {len(rows)} rows")
    return rows


def prove_live() -> None:
    conn = connect()
    cur = conn.cursor()

    cur.execute("SELECT DATABASE()")
    db = cur.fetchone()[0]

    cur.execute("SHOW TABLES")
    tables = [r[0] for r in cur.fetchall()]

    cur.execute("DESCRIBE theft_shredding")
    columns = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM theft_shredding")
    n = cur.fetchone()[0]

    cur.execute(
        "SELECT report_type, COUNT(*) FROM theft_shredding "
        "GROUP BY report_type ORDER BY report_type"
    )
    breakdown = cur.fetchall()

    cur.execute(
        "SELECT case_id, plate_number, report_type, report_date "
        "FROM theft_shredding ORDER BY report_date DESC LIMIT 8"
    )
    recent = cur.fetchall()

    cur.execute("SHOW CREATE TABLE theft_shredding")
    create_sql = cur.fetchone()[1]

    cur.close()
    conn.close()

    out_dir = Path(__file__).resolve().parent
    proof = []
    proof.append("=" * 60)
    proof.append("LIVE PROOF — theft_records.theft_shredding")
    proof.append("=" * 60)
    proof.append(f"Connected database : {db}")
    proof.append(f"Tables found       : {tables}")
    proof.append("")
    proof.append("Schema (DESCRIBE):")
    for col in columns:
        proof.append(f"  {col[0]:<15} {col[1]:<12} null={col[2]} key={col[3]}")
    proof.append("")
    proof.append(f"Row count          : {n}")
    proof.append("Breakdown:")
    for t, c in breakdown:
        proof.append(f"  {t:<12} {c}")
    proof.append("")
    proof.append("Recent cases:")
    for r in recent:
        proof.append(
            f"  case={r[0]:<5} plate={r[1]:<16} type={r[2]:<9} date={r[3]}"
        )
    proof.append("=" * 60)
    proof.append("OK — database is live and populated.")
    text = "\n".join(proof)
    print(text)

    (out_dir / "verify_output.txt").write_text(text + "\n", encoding="utf-8")
    (out_dir / "SHOW_CREATE_TABLE.txt").write_text(create_sql + "\n", encoding="utf-8")
    print(f"[ok] wrote {out_dir / 'verify_output.txt'}")
    print(f"[ok] wrote {out_dir / 'SHOW_CREATE_TABLE.txt'}")


def main() -> None:
    print("Building theft_records DB ...")
    ensure_database()
    ensure_schema_and_populate()
    prove_live()


if __name__ == "__main__":
    main()
