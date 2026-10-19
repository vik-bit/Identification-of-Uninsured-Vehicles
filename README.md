# Identification of Uninsured Vehicles (IIA)

Four source databases + ETL into `vehicle_dwh` + Streamlit **warehouse dashboard**.

## Efficient warehouse design

| Layer | Table | Role |
|-------|--------|------|
| Fact | `vehicle_360` | Integrated vehicle row; PK lookup |
| Xref | `dwh_plate_xref` | Canonical plate ↔ each source’s original spelling |
| Serving | `dwh_status_summary` | KPI counts for the dashboard |
| Serving | `dwh_flagged_cache` | Non-clear vehicles |
| Serving | `dwh_conflict_cache` | Integration conflicts (ghost plate, etc.) |
| Analytics | `ministry_state_risk` / `hotspots` / `insurer_gap` / `priority` | Precomputed charts & queues |

ETL (`run_etl.py`) refreshes serving + analytics tables. The GUI talks to **`vehicle_dwh` only** (no source-DB queries, no ad-hoc SQL, no logs tab). CSV and PDF export on every view.

## Run

```powershell
python generate_data.py
python seed_demo_ghost_plate.py   # optional viva seed
python run_etl.py
streamlit run app.py
```

## Dashboard tabs

| Tab | Source |
|-----|--------|
| Overview | Summary KPIs, priority queue, plate lookup |
| Fleet | `vehicle_360` browse / filter |
| Flagged | `dwh_flagged_cache` |
| Analytics | state risk, hotspots, insurer gaps |
| Conflicts | `dwh_conflict_cache` |

## Viva demo (ghost plate)

```powershell
python seed_demo_ghost_plate.py
python run_etl.py
```

Open **Conflicts** (or Overview lookup) for `DL9CAF9999` — SHREDDED then later sighted; no single source DB shows both.

## Phase 2 — noisy plates (entity matching & cleansing)

See **PHASE2_VIVA.md**. One command checks every rubric item:

```powershell
python phase2_check.py --full
```
