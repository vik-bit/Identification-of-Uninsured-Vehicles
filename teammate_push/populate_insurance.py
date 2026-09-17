"""
populate_insurance.py
---------------------
For the INSURANCE source owner (teammate).

Creates/refreshes `insurance.policies` using the IIA project schema and
loads realistic sample rows (Indian plate format with hyphens).

Prereq: Docker insurance MySQL running on port 3308
  (see TEAMMATE_DOCKER_SETUP.md / TEAMMATE_SETUP_DOCKER_TAILSCALE.md)

Usage:
    pip install mysql-connector-python
    python populate_insurance.py
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import mysql.connector

# ---- connection to YOUR local Docker insurance DB ----
HOST = "127.0.0.1"
PORT = 3308
USER = "iia_app"
PASSWORD = "iia_app_pw"
DATABASE = "insurance"

random.seed(42)
AS_OF = date(2026, 9, 12)
N_POLICIES = 500
EXPIRED_SHARE = 0.15

STATE_CODES = [
    "AN", "AP", "AR", "AS", "BR", "CH", "CG", "DD", "DL", "GA", "GJ", "HR",
    "HP", "JK", "JH", "KA", "KL", "LA", "LD", "MP", "MH", "MN", "ML", "MZ",
    "NL", "OD", "PY", "PB", "RJ", "SK", "TN", "TS", "TR", "UP", "UK", "WB",
]

INSURERS = [
    "ICICI Lombard", "HDFC Ergo", "Bajaj Allianz", "New India Assurance",
    "United India Insurance", "Oriental Insurance", "National Insurance",
    "Tata AIG", "Reliance General", "SBI General", "Go Digit", "Acko",
    "IFFCO Tokio", "Royal Sundaram", "Cholamandalam MS", "Liberty General",
]


def fmt_insurance(state: str, rto: int, series: str, number: int) -> str:
    """Insurance source plate format — hyphens (project convention)."""
    return f"{state}-{rto}-{series}-{number:04d}"


def random_plate(used: set) -> str:
    while True:
        state = random.choice(STATE_CODES)
        rto = random.randint(1, 50)
        series = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ", k=2))
        number = random.randint(1, 9999)
        key = (state, rto, series, number)
        if key not in used:
            used.add(key)
            return fmt_insurance(state, rto, series, number)


def main() -> None:
    used: set = set()
    rows = []
    for _ in range(N_POLICIES):
        term = timedelta(days=365)
        if random.random() < EXPIRED_SHARE:
            valid_to = AS_OF - timedelta(days=random.randint(1, 200))
        else:
            valid_to = AS_OF + timedelta(days=random.randint(10, 360))
        valid_from = valid_to - term
        rows.append(
            (
                random_plate(used),
                random.choice(INSURERS),
                valid_from,
                valid_to,
            )
        )

    print(f"Connecting to {HOST}:{PORT}/{DATABASE} ...")
    conn = mysql.connector.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE
    )
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS policies")
    cur.execute(
        """
        CREATE TABLE policies (
            policy_id      INT PRIMARY KEY AUTO_INCREMENT,
            vehicle_number VARCHAR(20) NOT NULL,
            insurer        VARCHAR(50) NOT NULL,
            valid_from     DATE NOT NULL,
            valid_to       DATE NOT NULL,
            INDEX idx_vehicle (vehicle_number)
        )
        """
    )
    cur.executemany(
        "INSERT INTO policies (vehicle_number, insurer, valid_from, valid_to) "
        "VALUES (%s, %s, %s, %s)",
        rows,
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM policies")
    total = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM policies WHERE valid_to < %s", (AS_OF,)
    )
    expired = cur.fetchone()[0]
    cur.execute(
        "SELECT policy_id, vehicle_number, insurer, valid_from, valid_to "
        "FROM policies ORDER BY policy_id LIMIT 5"
    )
    sample = cur.fetchall()
    cur.close()
    conn.close()

    print("insurance.policies ready")
    print(f"  total rows : {total}")
    print(f"  expired    : {expired} (as of {AS_OF})")
    print("  sample:")
    for r in sample:
        print(f"    {r}")
    print()
    print("Next: set WAREHOUSE_API in push_to_warehouse.py, then:")
    print("  python push_to_warehouse.py")


if __name__ == "__main__":
    main()
