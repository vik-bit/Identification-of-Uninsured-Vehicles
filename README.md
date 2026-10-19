# Identification of Uninsured Vehicles (IIA)

Team project: four **independent** MySQL source databases → ETL → warehouse (`vehicle_dwh`) → Streamlit dashboard.

This README is for **teammates**. Read the ownership section first.

---

## Who owns what

On the **warehouse machine** (Akash), only **two** source DBs run live:

| Role | Database | How it reaches the warehouse |
|------|----------|------------------------------|
| **Warehouse owner (local)** | `roadcapture` | LIVE MySQL (Docker `:3307`) |
| **Warehouse owner (local)** | `theft_records` | LIVE MySQL (Docker `:3310`) |
| **Teammate** | `insurance` | **Push only** → `stg_policies` |
| **Teammate** | `rto_registration` | **Push only** → `stg_registrations` |
| **Warehouse owner** | `vehicle_dwh` | ETL output + dashboard |

You do **not** need SSH into Akash’s MySQL for insurance/RTO.  
You run your own Docker DB, then **POST rows** to the ingest API.

Configured in `staging.py`:

```python
EXTRACT_FROM_STAGING = {
    "roadcapture": False,       # local LIVE
    "theft_records": False,     # local LIVE
    "insurance": True,          # teammate PUSH
    "rto_registration": True,   # teammate PUSH
}
```

---

## What you need (teammates)

1. Docker Desktop  
2. Python 3.10+ with: `pip install mysql-connector-python requests`  
3. The ingest URL from the warehouse owner (localhost tunnel / Tailscale / LAN)  
4. Your source ownership: **insurance** *or* **RTO**

### Insurance owner

Follow **[TEAMMATE_DOCKER_SETUP.md](TEAMMATE_DOCKER_SETUP.md)** (port **3308**).

```powershell
cd teammate_push
python populate_insurance.py
# edit push_to_warehouse.py → SOURCE_NAME = "insurance", set BASE_URL
python push_to_warehouse.py
```

### RTO owner

Follow **[TEAMMATE_DOCKER_SETUP_RTO.md](TEAMMATE_DOCKER_SETUP_RTO.md)** (port **3309**, lowercase `reg_no`).

```powershell
cd teammate_push
python populate_rto.py
# edit push_to_warehouse.py → SOURCE_NAME = "rto_registration", set BASE_URL
python push_to_warehouse.py
```

Push helpers live in [`teammate_push/`](teammate_push/).  
Network options: [TEAMMATE_SETUP_DOCKER_TAILSCALE.md](TEAMMATE_SETUP_DOCKER_TAILSCALE.md) · [FREE_TUNNEL_SETUP.md](FREE_TUNNEL_SETUP.md) · [PUSH_INTEGRATION.md](PUSH_INTEGRATION.md).

---

## Warehouse owner quick start (2 local DBs)

```powershell
# 1) Start your two source containers
cd roadcapture_db
docker compose up -d
python run_roadcapture_db.py

cd ..\theft_records_db
docker compose up -d
python run_theft_db.py

# 2) Warehouse + ingest API (from repo root)
cd ..
pip install -r requirements.txt   # or: mysql-connector-python pandas streamlit fpdf2 altair pillow pytesseract requests
python ingest_api.py              # keep running — teammates push here

# 3) After teammates push (or for a solo demo with placeholders)
python run_etl.py
streamlit run app.py
```

Optional Phase-2 noisy demo:

```powershell
python populate_noisy_data.py
python run_etl.py
python generate_sample_plate_images.py
streamlit run app.py
# → tab "Entity match" → upload sample_plates/scan_DL9CAF8B88.png
```

Plate-match weights + OCR dictionary (ML, not hand-tuned):

```powershell
pip install -r requirements.txt
python train_plate_matching.py          # writes match_weights.json + plate_ocr_lookup.json
```

---

## Architecture (short)

```
roadcapture (LIVE) ──┐
theft_records (LIVE)─┼─► run_etl.py ─► vehicle_360 + serving tables ─► Streamlit
insurance (PUSH) ────┤         ▲
rto (PUSH) ──────────┘         │
                     ingest_api.py ← teammate POST
```

- **Schema matching:** four different plate column names mean the same thing  
  (`plate_no` / `vehicle_number` / `reg_no` / `plate_number`).
- **Schema mapping:** `normalize_plate()` → `plate_canonical` in `vehicle_360`.
- **Phase 2:** fuzzy match under OCR noise + cleanse (see [PHASE2_VIVA.md](PHASE2_VIVA.md)).

---

## Dashboard tabs

| Tab | Purpose |
|-----|---------|
| How to use | Short guide |
| Overview | KPIs + priority queue |
| Records | Browse `vehicle_360`, plate filter, warehouse SQL |
| Entity match | Upload plate image → approximate match → cleanse |
| Analytics | State risk / hotspots / insurer gaps |
| Feedback | UI feedback form |

---

## Important files

| Path | Purpose |
|------|---------|
| `db_config.py` | Hosts/ports for all logical DBs |
| `staging.py` | LIVE vs PUSH switches |
| `ingest_api.py` | HTTP push endpoint for teammates |
| `run_etl.py` | Incremental watermark ETL |
| `app.py` | Streamlit dashboard |
| `entity_matching.py` / `plate_ocr.py` | Phase-2 matching + image extract |
| `train_plate_matching.py` | Train ML weights + OCR glyph dictionary |
| `match_weights.json` / `plate_ocr_lookup.json` | Trained weights + look-up (from train script) |
| `teammate_push/` | Scripts **teammates** run on their machines |
| `roadcapture_db/` / `theft_records_db/` | Docker packs for the **two local** DBs |

---

## Plate formats (do not “fix” these)

| Source | Example | Notes |
|--------|---------|--------|
| roadcapture | `DL5CX1234` | UPPER compact |
| insurance | `DL-5-CX-1234` | hyphens |
| rto_registration | `dl5cx1234` | lowercase |
| theft_records | `DL 5 CX 1234` | spaces |

ETL joins after normalization. Different spellings are intentional.

---

## Checklist before the Phase-2 demo

- [ ] Both local DBs up (`roadcapture`, `theft_records`)  
- [ ] Teammates have pushed insurance + RTO (or staging seeded)  
- [ ] `python run_etl.py` succeeds  
- [ ] `streamlit run app.py` shows Records + Entity match  
- [ ] Sample plate image uploads and finds a near match  
- [ ] Cleanse writes `dwh_cleanse_log`  

Questions for the warehouse owner: ingest URL, whether the tunnel is up, last successful push id in `ingest_push_log`.
