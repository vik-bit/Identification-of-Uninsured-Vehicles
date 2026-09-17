-- Runs once on first container start (empty volume).
-- Database `theft_records` is already created by MYSQL_DATABASE.

USE theft_records;

CREATE TABLE IF NOT EXISTS theft_shredding (
    case_id      INT PRIMARY KEY AUTO_INCREMENT,
    plate_number VARCHAR(20)  NOT NULL,
    report_type  VARCHAR(20)  NOT NULL,
    report_date  DATE         NOT NULL,
    INDEX idx_plate (plate_number),
    INDEX idx_type  (report_type)
);

-- Allow app user from any host (needed when integrator connects remotely later)
CREATE USER IF NOT EXISTS 'iia_app'@'%' IDENTIFIED BY 'iia_app_pw';
GRANT ALL PRIVILEGES ON theft_records.* TO 'iia_app'@'%';
FLUSH PRIVILEGES;
