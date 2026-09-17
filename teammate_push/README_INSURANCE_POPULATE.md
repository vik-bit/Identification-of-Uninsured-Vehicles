# Insurance source — populate + push (for Ayush)

## 1. Docker must be running

```powershell
cd C:\iia-insurance-db
docker compose ps
```

If not up: `docker compose up -d` and wait until healthy.

## 2. Populate policies (this script)

```powershell
pip install mysql-connector-python
python populate_insurance.py
```

Creates table:

```text
policies(policy_id, vehicle_number, insurer, valid_from, valid_to)
```

Plate format: `DL-5-CX-1234` (hyphens) — matches the IIA project insurance schema.  
Loads **500** sample policies.

## 3. Push to warehouse

Edit `push_to_warehouse.py`:

```python
WAREHOUSE_API = "https://YOUR-TUNNEL-OR-IP"   # from warehouse owner
API_KEY = "iia-ingest-key"
SOURCE_NAME = "insurance"
LOCAL_PORT = 3308
LOCAL_DATABASE = "insurance"
```

```powershell
pip install requests
python push_to_warehouse.py
```

You want `"ok": true` and `"rows_ingested": 500` (or similar).
