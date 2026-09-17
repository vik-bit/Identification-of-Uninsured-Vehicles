"""
db_config.py — connection settings for the theft_records Docker MySQL.

Default: localhost port 3310 (see docker-compose.yml).
"""

import mysql.connector

MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 3310
MYSQL_USER = "iia_app"
MYSQL_PASSWORD = "iia_app_pw"
MYSQL_DATABASE = "theft_records"


def connect(database=MYSQL_DATABASE):
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database,
    )


def connect_server():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
    )
