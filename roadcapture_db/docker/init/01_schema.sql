-- Runs once on first container start (empty volume).

USE roadcapture;

CREATE TABLE IF NOT EXISTS capture_log (
    capture_id     INT PRIMARY KEY AUTO_INCREMENT,
    plate_no       VARCHAR(20)  NOT NULL,
    capture_time   DATETIME     NOT NULL,
    location       VARCHAR(100) NOT NULL,
    vehicle_color  VARCHAR(20)  NULL,
    vehicle_type   VARCHAR(20)  NULL,
    INDEX idx_plate (plate_no),
    INDEX idx_time  (capture_time)
);

CREATE USER IF NOT EXISTS 'iia_app'@'%' IDENTIFIED BY 'iia_app_pw';
GRANT ALL PRIVILEGES ON roadcapture.* TO 'iia_app'@'%';
FLUSH PRIVILEGES;
