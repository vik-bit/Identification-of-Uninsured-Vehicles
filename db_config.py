"""
db_config.py — MySQL connection details for all IIA sources + warehouse.

Ownership on the warehouse owner's machine:
  LOCAL LIVE (ETL reads these):  roadcapture (:3306), theft_records (:3310)
  EXTERNAL PUSH only (ETL ignores these hosts): insurance, rto_registration
    → teammates POST into ingest_api → stg_policies / stg_registrations
    → see staging.EXTRACT_FROM_STAGING

Do not rely on local insurance/rto DBs for ETL — even if old copies still exist
on this machine, staging flags keep them out of the warehouse.
"""

import mysql.connector

# Shared app credentials (change per-source password later if you want)
DEFAULT_USER = "iia_app"
DEFAULT_PASSWORD = "iia_app_pw"

# host/port per database — THIS is your integration connection map
SOURCES = {
    # LOCAL — you own these; ETL reads LIVE
    "theft_records": {
        "host": "127.0.0.1",
        "port": 3310,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    "roadcapture": {
        "host": "127.0.0.1",
        "port": 3307,  # Docker in roadcapture_db/ (same pattern as theft :3310)
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    # EXTERNAL teammates — NOT used by ETL (staging only).
    # Ports match teammate Docker guides; ETL never reads these when staging=True.
    "insurance": {
        "host": "127.0.0.1",
        "port": 3308,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    "rto_registration": {
        "host": "127.0.0.1",
        "port": 3309,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
    # Warehouse — you own this
    "vehicle_dwh": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
    },
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
