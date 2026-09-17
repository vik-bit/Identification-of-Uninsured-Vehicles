-- =============================================================================
-- Source system: theft_records.theft_shredding
-- Owner folder: theft_records_db/
--
-- Designed in isolation from the other three IIA source databases.
-- Plate format is deliberately different (spaces between parts).
-- =============================================================================

CREATE DATABASE IF NOT EXISTS theft_records;
USE theft_records;

DROP TABLE IF EXISTS theft_shredding;

CREATE TABLE theft_shredding (
    case_id      INT PRIMARY KEY AUTO_INCREMENT,
    plate_number VARCHAR(20)  NOT NULL,   -- e.g. "DL 5 CX 1234"
    report_type  VARCHAR(20)  NOT NULL,   -- STOLEN | SHREDDED
    report_date  DATE         NOT NULL,
    INDEX idx_plate (plate_number),
    INDEX idx_type  (report_type)
);
