"""
db_config.py — MySQL connection details for all IIA sources + warehouse.

Each logical database can live on a different host/port (Docker or teammate machine).
Fill in teammate IP/port when their container is reachable.
"""

import mysql.connector

# Shared app credentials (change per-source password later if you want)
DEFAULT_USER = "iia_app"
DEFAULT_PASSWORD = "iia_app_pw"

# host/port per database — THIS is your integration connection map
SOURCES = {
    # Your theft source — Docker Compose in theft_records_db/ (port 3310)
    "theft_records": {
        "host": "127.0.0.1",
        "port": 3310,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    # Other sources: still your local MySQL :3306 until teammate Docker is wired
    "roadcapture": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    "insurance": {
        # When teammate is ready, change host to their IP and port to 3308
        "host": "127.0.0.1",
        "port": 3306,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    "rto_registration": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    # Warehouse — you own this (native MySQL :3306 for now)
    "vehicle_dwh": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    # Source (5): Ministry reporting DB (actions filed from the dossier UI)
    "ministry": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
}


def connect(database):
    cfg = SOURCES[database]
    return mysql.connector.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=database,
    )
