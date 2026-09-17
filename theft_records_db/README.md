# `theft_records_db` — my IIA source database

**Owner:** MT25021  
**MySQL database:** `theft_records`  
**Table:** `theft_shredding`

This folder is a **standalone proof of concept** for the theft / shredding source system. Schema design, population, and verification all live here. The shared Streamlit UI only *reads* this data after ETL — it does not own this schema.

---

## Quick start with Docker (recommended)

```powershell
cd theft_records_db
docker compose up -d
# wait ~20s for healthy
python run_theft_db.py
```

- MySQL container: `iia-theft-mysql`  
- Reachable at `127.0.0.1:3310`  
- DB `theft_records` / user `iia_app` / password `iia_app_pw`  

Warehouse ETL on this machine should point `theft_records` → port **3310** (see root `db_config.py`).

---

## Quick start (makes the DB live)


MySQL must be running, and the `iia_app` user must exist (from the repo’s `setup_mysql.sql`).

```powershell
cd theft_records_db
python run_theft_db.py
```

That one script will:

1. Ensure database `theft_records` exists  
2. Create table `theft_shredding`  
3. Populate ~228 synthetic cases (`STOLEN` / `SHREDDED`)  
4. Print a live proof and save `verify_output.txt` + `SHOW_CREATE_TABLE.txt`

---

## Schema

| Column | Type | Meaning |
|--------|------|---------|
| `case_id` | INT PK AI | Case id |
| `plate_number` | VARCHAR(20) | Plate in **this** source’s format: `DL 5 CX 1234` |
| `report_type` | VARCHAR(20) | `STOLEN` or `SHREDDED` |
| `report_date` | DATE | When the report was filed |

DDL also in [`schema.sql`](schema.sql).

### Why a separate format?

Other teammates’ sources use different plate strings on purpose:

| Source | Example |
|--------|---------|
| roadcapture | `DL5CX1234` |
| insurance | `DL-5-CX-1234` |
| rto_registration | `dl5cx1234` |
| **theft_records (me)** | `DL 5 CX 1234` |

ETL must canonicalize before joining — that’s the integration problem.

---

## Files in this folder

| File | Role |
|------|------|
| `README.md` | This document |
| `run_theft_db.py` | **Run this** → schema + data + live proof |
| `db_config.py` | MySQL connection for this source |
| `schema.sql` | Standalone DDL |
| `sample_theft_shredding.csv` | Offline sample rows |
| `verify_output.txt` | Generated proof after a successful run |
| `SHOW_CREATE_TABLE.txt` | Generated `SHOW CREATE TABLE` dump |

---

## Design notes

- Own database (`theft_records`), not a table inside someone else’s DB  
- Only two report types — clean rule for the warehouse decision logic  
- A few “phantom” plates that may not appear in capture_log (join edge case)  
- Indexes on plate + type for realistic lookup  

---

## After this DB is up

The rest of the IIA pipeline (teammates’ sources + ETL + UI) consumes it via:

- `check_connections.py` → expects `theft_records.theft_shredding`  
- `run_etl.py` → extracts and joins on normalized plate  
- `app.py` → shows theft as highest severity  
