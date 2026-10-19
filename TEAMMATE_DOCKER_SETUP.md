# Teammate guide: run YOUR source DB in Docker (so integrator can fill the warehouse)

This is for the **IIA** project. Each student owns **one source database**.  
The integrator (warehouse owner) will connect to your container using **your IP + port**.

---

## What you own (pick ONE)

| You own | Database name | Example table | Host port in this guide |
|---------|---------------|---------------|-------------------------|
| Insurance | `insurance` | `policies` | **3308** |
| Road capture | `roadcapture` | `capture_log` | **3307** |
| RTO | `rto_registration` | `registrations` | **3309** |
| Theft (already taken by teammate) | `theft_records` | `theft_shredding` | 3310 |

Below uses **insurance** as the example. Rename if you own another source.

**If you own RTO** (`rto_registration`, port **3309**, old project schema with lowercase `reg_no`): use **[`TEAMMATE_DOCKER_SETUP_RTO.md`](TEAMMATE_DOCKER_SETUP_RTO.md)** instead — do not invent a new table shape.

---

## 0. Install

1. [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)  
2. Start Docker Desktop (whale icon running)  
3. Open PowerShell  

---

## 1. Create a folder for your source

```powershell
mkdir C:\iia-insurance-db
cd C:\iia-insurance-db
mkdir docker\init
```

---

## 2. Create `docker-compose.yml`

Save this as `C:\iia-insurance-db\docker-compose.yml`:

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

---

## 3. Create schema init file

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

(Use **your** real schema if different — this is only a template.)

---

## 4. Start your container

```powershell
cd C:\iia-insurance-db
docker compose up -d
docker compose ps
```

Wait ~20–30 seconds until healthy.

---

## 5. Load some data (proof you populated it)

```powershell
docker exec -i iia-insurance-mysql mysql -uiia_app -piia_app_pw insurance -e "INSERT INTO policies (vehicle_number, insurer, valid_from, valid_to) VALUES ('DL-5-CX-1234', 'ICICI Lombard', '2025-01-01', '2026-12-31'), ('MH-12-AB-4455', 'HDFC Ergo', '2024-06-01', '2025-05-31'); SELECT COUNT(*) AS n FROM policies;"
```

---

## 6. Verify locally

```powershell
docker exec -it iia-insurance-mysql mysql -uiia_app -piia_app_pw insurance -e "SHOW TABLES; SELECT * FROM policies LIMIT 5;"
```

---

## 7. Send THIS to the warehouse owner (integrator)

Copy-paste and fill:

```text
Source owner: <your name / roll>
Database: insurance
Table: policies
Docker container: iia-insurance-mysql
Port on my machine: 3308
User: iia_app
Password: iia_app_pw

How to reach me (pick one and fill):
A) Same Wi-Fi LAN IPv4: _______________
B) Tailscale IP: _______________
C) For local-only demo on integrator PC: I'll send my docker folder / dump instead

Plate format I use: e.g. DL-5-CX-1234
```

Find LAN IP (only works on same Wi-Fi):

```powershell
ipconfig
```

Use the **IPv4 Address** of Wi-Fi / Ethernet.

---

## 8. Keep it running when integrator tests

- Docker Desktop open  
- `docker compose ps` shows container **running**  
- Firewall: allow port **3308** inbound if they connect over network  

---

## If you are NOT on the same network as the integrator

Easiest course options:

1. **Tailscale** (both install, share Tailscale IP + port 3308), or  
2. Zip your `iia-insurance-db` folder (or `docker exec` dump) and send it — integrator runs **your** compose on his PC on port 3308 and still lists you as owner of that source.

---

## Quick commands cheat sheet

```powershell
docker compose up -d          # start
docker compose down           # stop (keeps data volume)
docker compose down -v        # stop AND wipe data
docker compose logs -f        # debug
docker compose ps             # status
```

---

## What success looks like for the team

Integrator’s machine can run something like:

```text
Host: <your IP>
Port: 3308
DB: insurance
→ SELECT COUNT(*) FROM policies;  returns > 0
```

Then their ETL loads those rows into **`vehicle_dwh`** on their machine.
