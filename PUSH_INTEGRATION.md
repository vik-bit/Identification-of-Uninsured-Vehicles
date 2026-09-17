# Push integration (warehouse ingest API)

Sources stay on each student's machine. They **push** snapshots to your API.
You never open MySQL into their network.

```text
Teammate Docker DB  --HTTP POST-->  your ingest_api.py  -->  stg_* tables
                                                              |
                                                         run_etl.py
                                                              |
                                                         vehicle_dwh
```

## You (warehouse owner)

```powershell
pip install flask requests mysql-connector-python
python ingest_api.py
```

Leave it running. Share with teammate:

```text
WAREHOUSE_API=http://<YOUR_LAN_OR_TAILSCALE_IP>:8080
API_KEY=iia-ingest-key
```

Then after they push:

```powershell
python run_etl.py
```

`staging.py` → `EXTRACT_FROM_STAGING["insurance"] = True` means ETL reads pushed insurance rows.

## Teammate

See `teammate_push/README.md` + `TEAMMATE_DOCKER_SETUP.md`.
