# Teammate guide: RTO source DB in Docker (old / project schema)

For the student who owns **`rto_registration`**.

This uses the **same schema as the IIA project** (`generate_data.py`), not a redesigned table:

| Column | Type | Notes |
|--------|------|--------|
| `reg_id` | INT AUTO_INCREMENT PK | |
| `reg_no` | VARCHAR(20) | **lowercase compact** plates, e.g. `dl5cx1234` |
| `owner_name` | VARCHAR(50) | |
| `make_model` | VARCHAR(50) | |
| `registration_date` | DATE | |

Plate style is intentional (heterogeneity): insurance uses `DL-5-CX-1234`, capture uses `DL5CX1234`, RTO uses `dl5cx1234`.

Host port in this guide: **3309**.

---

## 0. Install

1. [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)  
2. Start Docker Desktop  
3. Open PowerShell  

---

## 1. Create folder

```powershell
mkdir C:\iia-rto-db
cd C:\iia-rto-db
mkdir docker\init
```

---

## 2. `docker-compose.yml`

Save as `C:\iia-rto-db\docker-compose.yml`:

```yaml
services:
  rto-mysql:
    image: mysql:8.0
    container_name: iia-rto-mysql
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: rto_root_pw
      MYSQL_DATABASE: rto_registration
      MYSQL_USER: iia_app
      MYSQL_PASSWORD: iia_app_pw
    ports:
      - "3309:3306"
    volumes:
      - rto_mysql_data:/var/lib/mysql
      - ./docker/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-uiia_app", "-piia_app_pw"]
      interval: 5s
      timeout: 5s
      retries: 20

volumes:
  rto_mysql_data:
```

---

## 3. Schema init (old / project schema)

Save as `C:\iia-rto-db\docker\init\01_schema.sql`:

```sql
USE rto_registration;

CREATE TABLE IF NOT EXISTS registrations (
    reg_id             INT PRIMARY KEY AUTO_INCREMENT,
    reg_no             VARCHAR(20) NOT NULL,
    owner_name         VARCHAR(50) NOT NULL,
    make_model         VARCHAR(50) NOT NULL,
    registration_date  DATE NOT NULL,
    INDEX idx_reg_no (reg_no)
);

CREATE USER IF NOT EXISTS 'iia_app'@'%' IDENTIFIED BY 'iia_app_pw';
GRANT ALL PRIVILEGES ON rto_registration.* TO 'iia_app'@'%';
FLUSH PRIVILEGES;
```

---

## 4. Start container

```powershell
cd C:\iia-rto-db
docker compose up -d
docker compose ps
```

Wait until healthy (~20–30s).

---

## 5. Load sample data

**Option A — quick insert**

```powershell
docker exec -i iia-rto-mysql mysql -uiia_app -piia_app_pw rto_registration -e "INSERT INTO registrations (reg_no, owner_name, make_model, registration_date) VALUES ('dl5cx1234', 'Ravi Kumar', 'Maruti Swift', '2022-03-15'), ('mh12ab4455', 'Priya Sharma', 'Hyundai Creta', '2021-08-01'); SELECT COUNT(*) AS n FROM registrations;"
```

**Option B — populate script (from repo)**

On a machine with this repo + Python:

```powershell
pip install mysql-connector-python
python teammate_push\populate_rto.py
```

(Script targets `127.0.0.1:3309` by default.)

---

## 6. Verify locally

```powershell
docker exec -it iia-rto-mysql mysql -uiia_app -piia_app_pw rto_registration -e "SHOW TABLES; DESCRIBE registrations; SELECT * FROM registrations LIMIT 5;"
```

Confirm `reg_no` values look like `dl5cx1234` (lowercase, no hyphens/spaces).

---

## 7. Send THIS to the warehouse owner

```text
Source owner: <your name / roll>
Database: rto_registration
Table: registrations
Schema: reg_id, reg_no, owner_name, make_model, registration_date
Plate format: lowercase compact (dl5cx1234)
Docker container: iia-rto-mysql
Port on my machine: 3309
User: iia_app
Password: iia_app_pw

How to reach me:
A) Same Wi-Fi IPv4: _______________
B) Tailscale IP: _______________
C) I'll zip C:\iia-rto-db / dump for local demo
```

LAN IP:

```powershell
ipconfig
```

---

## 8. Keep running while integrator tests

- Docker Desktop open  
- `docker compose ps` → **running**  
- Firewall: allow inbound **3309** if connecting over network  

---

## Not on the same network?

1. **Tailscale** (share Tailscale IP + port 3309), or  
2. Zip `C:\iia-rto-db` / send a dump — integrator runs your compose on **3309**.

---

## Cheat sheet

```powershell
docker compose up -d
docker compose down
docker compose down -v
docker compose logs -f
docker compose ps
```

---

## Success check for integrator

```text
Host: <your IP>
Port: 3309
DB: rto_registration
→ SELECT COUNT(*) FROM registrations;  returns > 0
→ plates look like dl5cx1234
```

ETL on the warehouse machine normalizes `reg_no` into `plate_canonical` and joins with the other sources.
