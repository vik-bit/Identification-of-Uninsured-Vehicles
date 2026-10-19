"""
populate_rto.py
---------------
For the RTO source owner (teammate).

Creates/refreshes `rto_registration.registrations` using the IIA **old/project**
schema and loads sample rows with lowercase compact plates (dl5cx1234).

Prereq: Docker RTO MySQL on port 3309
  (see TEAMMATE_DOCKER_SETUP_RTO.md)

Usage:
    pip install mysql-connector-python
    python populate_rto.py
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import mysql.connector

HOST = "127.0.0.1"
PORT = 3309
USER = "iia_app"
PASSWORD = "iia_app_pw"
DATABASE = "rto_registration"

random.seed(42)
AS_OF = date(2026, 9, 12)
N_REGS = 500

STATE_CODES = [
    "AN", "AP", "AR", "AS", "BR", "CH", "CG", "DD", "DL", "GA", "GJ", "HR",
    "HP", "JK", "JH", "KA", "KL", "LA", "LD", "MP", "MH", "MN", "ML", "MZ",
    "NL", "OD", "PY", "PB", "RJ", "SK", "TN", "TS", "TR", "UP", "UK", "WB",
]

MAKE_MODELS = [
    "Maruti Swift", "Maruti Baleno", "Hyundai Creta", "Hyundai i20",
    "Tata Nexon", "Tata Punch", "Mahindra Scorpio", "Honda City",
    "Toyota Innova", "Kia Seltos",
]

FIRST = ["Ravi", "Amit", "Priya", "Neha", "Suresh", "Anita", "Rahul", "Kavita"]
LAST = ["Kumar", "Sharma", "Singh", "Patel", "Reddy", "Nair", "Gupta", "Das"]


def fmt_registration(state: str, rto: int, series: str, number: int) -> str:
    """RTO old-schema plate format — lowercase compact (project convention)."""
    return f"{state}{rto}{series}{number:04d}".lower()


def random_plate(used: set) -> str:
    while True:
        state = random.choice(STATE_CODES)
        rto = random.randint(1, 50)
        series = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ", k=2))
        number = random.randint(1, 9999)
        key = (state, rto, series, number)
        if key not in used:
            used.add(key)
            return fmt_registration(state, rto, series, number)


def main() -> None:
    used: set = set()
    rows = []
    for _ in range(N_REGS):
        reg_date = AS_OF - timedelta(days=random.randint(30, 3500))
        rows.append(
            (
                random_plate(used),
                f"{random.choice(FIRST)} {random.choice(LAST)}",
                random.choice(MAKE_MODELS),
                reg_date,
            )
        )

    conn = mysql.connector.connect(
        host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE
    )
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS registrations (
            reg_id INT PRIMARY KEY AUTO_INCREMENT,
            reg_no VARCHAR(20) NOT NULL,
            owner_name VARCHAR(50) NOT NULL,
            make_model VARCHAR(50) NOT NULL,
            registration_date DATE NOT NULL,
            INDEX idx_reg_no (reg_no)
        )
        """
    )
    cur.execute("DELETE FROM registrations")
    cur.executemany(
        "INSERT INTO registrations (reg_no, owner_name, make_model, registration_date) "
        "VALUES (%s,%s,%s,%s)",
        rows,
    )
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM registrations")
    n = cur.fetchone()[0]
    cur.execute("SELECT reg_no, owner_name, make_model FROM registrations LIMIT 5")
    sample = cur.fetchall()
    cur.close()
    conn.close()

    print(f"OK: {n} rows in {DATABASE}.registrations @ {HOST}:{PORT}")
    print("Sample (lowercase compact plates):")
    for r in sample:
        print(" ", r)


if __name__ == "__main__":
    main()
