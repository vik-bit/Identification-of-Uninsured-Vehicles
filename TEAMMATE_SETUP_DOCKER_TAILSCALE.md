# Teammate setup: Docker + Tailscale + push to warehouse

You own **one source database** on **your laptop**.  
The warehouse owner does **not** log into your MySQL.  
You only **push** your data to their ingest API over Tailscale.

---

## What you need

1. [Docker Desktop](https://www.docker.com/products/docker-desktop/) for Windows  
2. [Tailscale](https://tailscale.com/download) for Windows  
3. Python 3 + pip  

Ask the warehouse owner for:

```text
1. Tailscale invite (join their tailnet)
2. WAREHOUSE_API = http://100.x.x.x:8080
3. API_KEY = iia-ingest-key
4. Which source you own: insurance | roadcapture | rto_registration
```

---

## Part A — Tailscale

1. Install Tailscale and sign in with **your own** email (not theirs).  
2. Accept their **tailnet invite**.  
3. Open PowerShell:

```powershell
tailscale status
tailscale ip -4
```

4. Confirm:
   - Status looks connected  
   - You see **their** machine in `tailscale status`  
   - You have a `100.x.x.x` IP  

5. From your PC, test their API (they must have `ingest_api.py` running):

```powershell
curl http://100.x.x.x:8080/health
```

Replace `100.x.x.x` with the IP they gave you.  
You want JSON like `{"ok": true, ...}`.

---

## Part B — Docker (your source DB)

Example below = **insurance** on port **3308**.  
If you own another source, change names/ports as noted.

### B1. Create folder

```powershell
mkdir C:\iia-insurance-db
cd C:\iia-insurance-db
mkdir docker\init
```

### B2. Create `docker-compose.yml`

Save as `C:\iia-insurance-db\docker-compose.yml`:

```yaml
services:
  insurance-mysql:
    image: mysql:8.0
    container_name: iia-insurance-mysql
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: insurance_root_pw
      MYSQL_DATABASE: insurance
      MYSQL_USER: iia_app
      MYSQL_PASSWORD: iia_app_pw
    ports:
      - "3308:3306"
    volumes:
      - insurance_mysql_data:/var/lib/mysql
      - ./docker/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-uiia_app", "-piia_app_pw"]
      interval: 5s
      timeout: 5s
      retries: 20

volumes:
  insurance_mysql_data:
```

**If `docker compose up` can’t pull `mysql:8.0`:**

```powershell
docker pull public.ecr.aws/docker/library/mysql:8.0
docker tag public.ecr.aws/docker/library/mysql:8.0 mysql:8.0
```

### B3. Create schema

Save as `C:\iia-insurance-db\docker\init\01_schema.sql`:

```sql
USE insurance;

CREATE TABLE IF NOT EXISTS policies (
    policy_id      INT PRIMARY KEY AUTO_INCREMENT,
    vehicle_number VARCHAR(20) NOT NULL,
    insurer        VARCHAR(50) NOT NULL,
    valid_from     DATE NOT NULL,
    valid_to       DATE NOT NULL,
    INDEX idx_vehicle (vehicle_number)
);

CREATE USER IF NOT EXISTS 'iia_app'@'%' IDENTIFIED BY 'iia_app_pw';
GRANT ALL PRIVILEGES ON insurance.* TO 'iia_app'@'%';
FLUSH PRIVILEGES;
```

(Use your real schema if the team agreed on different columns.)

### B4. Start and wait

```powershell
cd C:\iia-insurance-db
docker compose up -d
docker compose ps
```

Wait until status is **healthy** (~20–40 seconds).

### B5. Add sample data

```powershell
docker exec -i iia-insurance-mysql mysql -uiia_app -piia_app_pw insurance -e "INSERT INTO policies (vehicle_number, insurer, valid_from, valid_to) VALUES ('DL-5-CX-1234', 'ICICI Lombard', '2025-01-01', '2026-12-31'), ('MH-12-AB-4455', 'HDFC Ergo', '2024-06-01', '2026-05-31'); SELECT COUNT(*) AS n FROM policies;"
```

### B6. Verify locally

```powershell
docker exec -it iia-insurance-mysql mysql -uiia_app -piia_app_pw insurance -e "SHOW TABLES; SELECT * FROM policies LIMIT 5;"
```

---

## Part C — Push to warehouse

### C1. Get the push script

Copy from the project repo:

- `teammate_push/push_to_warehouse.py`

Or ask the warehouse owner to send that file.

### C2. Edit the CONFIG at the top of the script

```python
WAREHOUSE_API = "http://100.x.x.x:8080"   # their Tailscale IP
API_KEY = "iia-ingest-key"

SOURCE_NAME = "insurance"
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 3308
LOCAL_USER = "iia_app"
LOCAL_PASSWORD = "iia_app_pw"
LOCAL_DATABASE = "insurance"
LOCAL_TABLE = "policies"
SENDER_NAME = "your-name-insurance"
```

### C3. Run the push

```powershell
pip install mysql-connector-python requests
python push_to_warehouse.py
```

Success looks like:

```json
{"ok": true, "rows_ingested": ..., "source": "insurance", ...}
```

You do **not** need to open MySQL to the internet. Only outbound HTTP to their API.

---

## Port cheat sheet

| Source you own        | Database           | Docker host port |
|-----------------------|--------------------|------------------|
| Insurance             | `insurance`        | 3308             |
| Road capture          | `roadcapture`      | 3307             |
| RTO                   | `rto_registration` | 3309             |
| Theft (already taken) | `theft_records`    | 3310             |

---

## Checklist before telling them “done”

- [ ] Tailscale connected; you can see their machine  
- [ ] `curl http://<their-100.x.x.x>:8080/health` works  
- [ ] `docker compose ps` shows your MySQL **running/healthy**  
- [ ] Local `SELECT COUNT(*)` on your table returns rows  
- [ ] `push_to_warehouse.py` returns `"ok": true`  

Then they run on their PC: `python run_etl.py` to load the warehouse.

---

## Common problems

| Problem | Fix |
|---------|-----|
| `health` fails | They must start `python ingest_api.py`; both on same Tailscale tailnet |
| Docker pull TLS error | Use the `public.ecr.aws/...` pull + tag commands above |
| Push `unauthorized` | Wrong `API_KEY` / missing `X-API-Key` |
| Push connection error | Wrong `WAREHOUSE_API` IP; Tailscale disconnected; their API not running |
| Empty push | Your table has no rows — insert sample data first |

---

## What you send back to the warehouse owner

```text
Source owner: <name / roll>
Source: insurance
Docker: running on my PC (port 3308)
Push: succeeded to http://100.x.x.x:8080/ingest/insurance
Rows pushed: <n>
My Tailscale IP: <optional>
```
