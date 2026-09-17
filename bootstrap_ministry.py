"""bootstrap_ministry.py — create ministry DB + grants (needs MySQL root once)."""
import mysql.connector

PASSWORDS = ["root123", "", "root", "iia_app_pw"]

for pw in PASSWORDS:
    try:
        c = mysql.connector.connect(host="127.0.0.1", port=3306, user="root", password=pw)
        print("connected as root with password", repr(pw))
        cur = c.cursor()
        cur.execute("CREATE DATABASE IF NOT EXISTS ministry")
        for host in ("127.0.0.1", "%", "localhost"):
            try:
                cur.execute(
                    f"GRANT ALL PRIVILEGES ON ministry.* TO 'iia_app'@'{host}'"
                )
            except Exception as e:
                print("grant skip", host, e)
        cur.execute("FLUSH PRIVILEGES")
        c.commit()
        cur.close()
        c.close()
        print("ministry database ready")
        break
    except Exception as e:
        print("root fail", repr(pw), "->", e)
else:
    raise SystemExit("Could not connect as root to create ministry DB")
