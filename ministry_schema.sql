-- Source (5): Ministry reporting database + warehouse cross-ref / conflict cache
-- Run once as root (or app user with CREATE privilege), then re-run ETL.

CREATE DATABASE IF NOT EXISTS ministry;
GRANT ALL PRIVILEGES ON ministry.* TO 'iia_app'@'127.0.0.1';
GRANT ALL PRIVILEGES ON ministry.* TO 'iia_app'@'%';
FLUSH PRIVILEGES;

USE ministry;

CREATE TABLE IF NOT EXISTS ministry_reports (
    report_id        INT PRIMARY KEY AUTO_INCREMENT,
    created_at       DATETIME NOT NULL,
    officer_id       VARCHAR(50) NOT NULL,
    plate_canonical  VARCHAR(20) NOT NULL,
    action_code      VARCHAR(40) NOT NULL,
    -- DETAIN | NOTICE_NO_POLICY | NOTICE_EXPIRED | CLEAR_RELEASE | ESCALATE
    notes            VARCHAR(500) NULL,
    warehouse_status VARCHAR(100) NULL,
    source_snapshot  JSON NULL,
    INDEX idx_plate (plate_canonical),
    INDEX idx_created (created_at)
);

-- Hash-chained audit: each row seals the previous hash (tamper-evident for viva)
CREATE TABLE IF NOT EXISTS ministry_report_audit (
    audit_id     INT PRIMARY KEY AUTO_INCREMENT,
    report_id    INT NOT NULL,
    recorded_at  DATETIME NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash    CHAR(64) NOT NULL,
    entry_hash   CHAR(64) NOT NULL,
    INDEX idx_report (report_id)
);

USE vehicle_dwh;

-- Preserves each source's ORIGINAL plate spelling after normalize/join
CREATE TABLE IF NOT EXISTS dwh_plate_xref (
    plate_canonical   VARCHAR(20) NOT NULL,
    source_name       VARCHAR(40) NOT NULL,
    -- roadcapture | insurance | rto_registration | theft_records
    native_plate      VARCHAR(40) NOT NULL,
    source_pk         VARCHAR(40) NULL,
    refreshed_at      DATETIME NOT NULL,
    PRIMARY KEY (plate_canonical, source_name),
    INDEX idx_native (native_plate)
);

-- Precomputed conflicts (UI reads cache; ETL does the full pass)
CREATE TABLE IF NOT EXISTS dwh_conflict_cache (
    conflict_id       INT PRIMARY KEY AUTO_INCREMENT,
    conflict_type     VARCHAR(60) NOT NULL,
    plate_canonical   VARCHAR(20) NOT NULL,
    detail            VARCHAR(400) NOT NULL,
    refreshed_at      DATETIME NOT NULL,
    INDEX idx_type (conflict_type),
    INDEX idx_plate (plate_canonical)
);
