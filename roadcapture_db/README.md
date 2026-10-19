# `roadcapture` source DB (your ownership — same pattern as `theft_records_db`)

Isolated Docker MySQL on host port **3307**.

| Item | Value |
|------|--------|
| Database | `roadcapture` |
| Table | `capture_log` |
| Port | **3307** |
| User / pass | `iia_app` / `iia_app_pw` |
| Plate format | UPPERCASE compact e.g. `DL5CX1234` |

## Start

```powershell
cd roadcapture_db
docker compose up -d
docker compose ps
```

Wait until healthy, then:

```powershell
python run_roadcapture_db.py
```

## Warehouse wiring

Repo-root `db_config.py` points `roadcapture` → `127.0.0.1:3307`.  
ETL reads it **LIVE** (same idea as theft on `:3310`).

## Optional: push into ingest staging

With `ingest_api.py` running on `:8080`:

```powershell
python push_my_roadcapture.py
```
