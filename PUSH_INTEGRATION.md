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

`staging.py` → extract ownership on the warehouse machine:

| Source | ETL reads |
|--------|-----------|
| `roadcapture` | **LIVE** local |
| `theft_records` | **LIVE** local Docker `:3310` |
| `insurance` | **STAGING only** (`stg_policies`) — teammate push |
| `rto_registration` | **STAGING only** (`stg_registrations`) — teammate push |

Local leftover `insurance` / `rto_registration` DBs on this PC are **ignored** by ETL.

## Teammate

See `teammate_push/README.md` + `TEAMMATE_DOCKER_SETUP.md`.
