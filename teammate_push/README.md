# Push your source DB → warehouse (no Tailscale MySQL required)

You run MySQL/Docker **on your laptop**.  
The warehouse owner runs a small **HTTP API**.  
You only **send data out** (push). They never log into your machine.

---

## 1. On your machine — run your source (Docker)

Follow `TEAMMATE_DOCKER_SETUP.md` first (e.g. insurance on port 3308) and load data.

---

## 2. Get the warehouse API address from the integrator

They will send something like:

```text
WAREHOUSE_API=http://100.x.x.x:8080
# or same Wi‑Fi: http://192.168.x.x:8080
# or for a test when files are on their PC: http://127.0.0.1:8080
API_KEY=iia-ingest-key
```

Their API must be running: `python ingest_api.py`

---

## 3. Edit and run the push script

Copy `teammate_push/push_to_warehouse.py` to your PC (or clone the repo).

Edit the CONFIG block:

- `WAREHOUSE_API` → address they gave you  
- `SOURCE_NAME` → `insurance` (or your source)  
- `LOCAL_PORT` → `3308`  
- `LOCAL_DATABASE` / `LOCAL_TABLE` → your DB  

```powershell
pip install mysql-connector-python requests
python push_to_warehouse.py
```

You should see `"ok": true` and `rows_ingested`.

---

## 4. What happens next (their side)

1. Rows land in their warehouse staging table (e.g. `stg_policies`)  
2. They run `python run_etl.py`  
3. Integrated results appear in `vehicle_360` / UI  

You keep owning schema + data on **your** machine. Integration is via **push**, not opening MySQL.

---

## Firewall note

You only need **outbound** HTTPS/HTTP.  
**They** may need to allow inbound port **8080** (or use Tailscale only for the API, not for MySQL).
