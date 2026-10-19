"""
populate_noisy_data.py
----------------------
Populate sources + warehouse staging with *intentional OCR / format noise*
for Phase-2 entity matching (prof: DBs should contain noisy data).

What it does:
  1. Picks clean plates already in LIVE roadcapture.
  2. Inserts extra capture_log rows with OCR-confused plates (0/O, 1/I, 8/B, …)
     keeping the SAME color / type / location (context for cleanse).
  3. Injects noisy spellings into stg_policies / stg_registrations for a subset
     (hyphens, lowercase, OCR slips) so insurance/RTO are noisy too.
  4. Writes vehicle_dwh.dwh_noisy_seed_manifest (clean ↔ noisy map for viva).

Safe to re-run: deletes previous rows tagged in the manifest first.

Usage:
    python populate_noisy_data.py
    python populate_noisy_data.py --pairs 40
    python run_etl.py
"""

from __future__ import annotations

import argparse
import random
import re
from datetime import date, datetime, timedelta

from db_config import connect
from entity_matching import normalize_plate
from staging import ensure_staging_tables

random.seed(20261018)

# OCR substitution families (same spirit as entity_matching.OCR_GROUPS)
OCR_SUBS = {
    "0": "O",
    "O": "0",
    "1": "I",
    "I": "1",
    "5": "S",
    "S": "5",
    "8": "B",
    "B": "8",
    "2": "Z",
    "Z": "2",
    "6": "G",
    "G": "6",
}


def ensure_manifest(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dwh_noisy_seed_manifest (
            manifest_id   INT PRIMARY KEY AUTO_INCREMENT,
            created_at    DATETIME NOT NULL,
            clean_plate   VARCHAR(20) NOT NULL,
            noisy_plate   VARCHAR(40) NOT NULL,
            noise_kind    VARCHAR(40) NOT NULL,
            source_name   VARCHAR(40) NOT NULL,
            context_color VARCHAR(20) NULL,
            context_type  VARCHAR(20) NULL,
            context_model VARCHAR(50) NULL,
            note          VARCHAR(200) NULL,
            INDEX idx_clean (clean_plate),
            INDEX idx_noisy (noisy_plate)
        )
        """
    )


def clear_previous_seed() -> None:
    """Remove prior noisy seed artefacts using the manifest."""
    wh = connect("vehicle_dwh")
    wcur = wh.cursor()
    ensure_staging_tables(wcur)
    ensure_manifest(wcur)
    wcur.execute(
        "SELECT noisy_plate, source_name FROM dwh_noisy_seed_manifest"
    )
    rows = wcur.fetchall()
    by_source: dict[str, list[str]] = {}
    for noisy, src in rows:
        by_source.setdefault(src, []).append(noisy)

    # roadcapture LIVE
    if by_source.get("roadcapture"):
        try:
            rc = connect("roadcapture")
            rcur = rc.cursor()
            for plate in by_source["roadcapture"]:
                rcur.execute("DELETE FROM capture_log WHERE plate_no=%s", (plate,))
            rc.commit()
            rcur.close()
            rc.close()
        except Exception as e:
            print(f"  [warn] clear roadcapture: {e}")

    for plate in by_source.get("insurance", []):
        wcur.execute("DELETE FROM stg_policies WHERE vehicle_number=%s", (plate,))
    for plate in by_source.get("rto_registration", []):
        wcur.execute("DELETE FROM stg_registrations WHERE reg_no=%s", (plate,))

    wcur.execute("DELETE FROM dwh_noisy_seed_manifest")
    wh.commit()
    wcur.close()
    wh.close()
    print(f"  cleared previous manifest rows: {len(rows)}")


def mutate_ocr(plate: str) -> str | None:
    """Flip 1–2 OCR-confusable characters; return None if nothing to flip."""
    chars = list(normalize_plate(plate))
    idxs = [i for i, ch in enumerate(chars) if ch in OCR_SUBS]
    if not idxs:
        return None
    k = min(len(idxs), random.choice([1, 1, 2]))
    for i in random.sample(idxs, k):
        chars[i] = OCR_SUBS[chars[i]]
    noisy = "".join(chars)
    if noisy == normalize_plate(plate):
        return None
    return noisy


def format_variants(canonical: str) -> list[tuple[str, str]]:
    """Return (noisy_or_alt_spelling, noise_kind) variants."""
    c = normalize_plate(canonical)
    out: list[tuple[str, str]] = []
    ocr = mutate_ocr(c)
    if ocr:
        out.append((ocr, "ocr_substitution"))
    # format noise (not OCR) — still "noisy recorded data"
    if len(c) >= 8:
        # DL9CAF8888 -> DL-9-CAF-8888 style guess
        m = re.match(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$", c)
        if m:
            st, rto, ser, num = m.groups()
            out.append((f"{st}-{rto}-{ser}-{num}", "hyphen_format"))
            out.append((f"{st} {rto} {ser} {num}", "spaced_format"))
            out.append((c.lower(), "lowercase_compact"))
    # dedupe
    seen = set()
    uniq = []
    for plate, kind in out:
        key = (normalize_plate(plate), kind)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((plate, kind))
    return uniq


def fetch_clean_capture_sample(n: int) -> list[dict]:
    conn = connect("roadcapture")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT plate_no, vehicle_color, vehicle_type, location
        FROM capture_log
        ORDER BY capture_time DESC
        """
    )
    rows = []
    seen = set()
    for plate, color, vtype, loc in cur.fetchall():
        key = normalize_plate(plate)
        if not key or key in seen:
            continue
        # skip already-noisy looking plates if they differ from alphanumeric only quirks
        seen.add(key)
        rows.append(
            {
                "plate": key,
                "color": color or "White",
                "vtype": vtype or "Sedan",
                "location": loc or "Phase2 Noise Cam",
            }
        )
        if len(rows) >= n:
            break
    cur.close()
    conn.close()
    return rows


def populate(pairs: int = 40) -> None:
    print(f"Populating noisy data (~{pairs} clean bases)…")
    clear_previous_seed()

    bases = fetch_clean_capture_sample(pairs)
    if not bases:
        raise SystemExit("No capture_log rows — populate roadcapture first.")

    wh = connect("vehicle_dwh")
    wcur = wh.cursor()
    ensure_staging_tables(wcur)
    ensure_manifest(wcur)

    rc = connect("roadcapture")
    rcur = rc.cursor()

    now = datetime.now()
    # Captures must be *after* ETL watermark or incremental load skips them.
    try:
        wcur.execute(
            "SELECT last_processed_at FROM etl_watermark WHERE source_name='capture_log'"
        )
        wm = wcur.fetchone()
        watermark = wm[0] if wm and wm[0] else datetime(2000, 1, 1)
    except Exception:
        watermark = datetime(2000, 1, 1)
    t_base = max(now, watermark) + timedelta(minutes=1)

    manifest_rows = []
    cap_inserts = 0
    ins_inserts = 0
    rto_inserts = 0
    tick = 0

    def next_capture_time() -> datetime:
        nonlocal tick
        tick += 1
        return t_base + timedelta(seconds=tick)

    # Always keep the classic demo pair
    demo_clean = "DL9CAF8888"
    demo_noisy = "DL9CAF8B88"
    bases = [
        {
            "plate": demo_clean,
            "color": "Black",
            "vtype": "SUV",
            "location": "Phase2 Demo Gate",
        }
    ] + [b for b in bases if b["plate"] != demo_clean]

    for i, base in enumerate(bases[:pairs]):
        clean = base["plate"]
        variants = format_variants(clean)
        if clean == demo_clean:
            variants = [(demo_noisy, "ocr_substitution")] + [
                v for v in variants if normalize_plate(v[0]) != normalize_plate(demo_noisy)
            ]

        # Ensure clean capture exists for demo plate
        if clean == demo_clean:
            rcur.execute("DELETE FROM capture_log WHERE plate_no=%s", (clean,))
            rcur.execute(
                "INSERT INTO capture_log (plate_no, capture_time, location, vehicle_color, vehicle_type) "
                "VALUES (%s,%s,%s,%s,%s)",
                (clean, next_capture_time(), base["location"], base["color"], base["vtype"]),
            )

        for noisy, kind in variants[:2]:  # up to 2 noisy forms per clean plate
            # Avoid colliding with an existing different clean plate
            if normalize_plate(noisy) != clean:
                # capture noise (OCR / alt) — LIVE source the scanner would write
                if kind.startswith("ocr") or kind in ("hyphen_format", "spaced_format"):
                    # store native noisy spelling in capture
                    rcur.execute("DELETE FROM capture_log WHERE plate_no=%s", (noisy,))
                    rcur.execute(
                        "INSERT INTO capture_log "
                        "(plate_no, capture_time, location, vehicle_color, vehicle_type) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (
                            noisy,
                            next_capture_time(),
                            base["location"],
                            base["color"],
                            base["vtype"],
                        ),
                    )
                    cap_inserts += 1
                    manifest_rows.append(
                        (
                            now,
                            clean,
                            noisy,
                            kind,
                            "roadcapture",
                            base["color"],
                            base["vtype"],
                            None,
                            "OCR/format noise on capture",
                        )
                    )

            # Insurance staging noise (hyphen / OCR) for subset
            if i % 2 == 0 and kind in ("ocr_substitution", "hyphen_format"):
                ins_plate = noisy if "-" in noisy else f"{noisy[:2]}-{noisy[2:]}" if len(noisy) > 4 else noisy
                # prefer hyphen style for insurance
                m = re.match(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$", clean)
                if m and kind == "ocr_substitution":
                    st, rto, ser, num = m.groups()
                    # OCR on number part then hyphenate
                    nnum = mutate_ocr(num) or num
                    ins_plate = f"{st}-{rto}-{ser}-{nnum}"
                elif m:
                    st, rto, ser, num = m.groups()
                    ins_plate = f"{st}-{rto}-{ser}-{num}"
                wcur.execute("DELETE FROM stg_policies WHERE vehicle_number=%s", (ins_plate,))
                wcur.execute(
                    "INSERT INTO stg_policies "
                    "(policy_id, vehicle_number, insurer, valid_from, valid_to, ingested_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        920000 + i,
                        ins_plate,
                        "NoiseDemoInsurer",
                        date(2025, 1, 1),
                        date(2027, 1, 1),
                        now,
                    ),
                )
                ins_inserts += 1
                manifest_rows.append(
                    (
                        now,
                        clean,
                        ins_plate,
                        kind + "+insurance_format",
                        "insurance",
                        base["color"],
                        base["vtype"],
                        None,
                        "noisy insurance vehicle_number",
                    )
                )

            # RTO staging noise (lowercase / OCR) for subset
            if i % 3 == 0 and kind in ("ocr_substitution", "lowercase_compact"):
                rto_plate = normalize_plate(noisy).lower() if kind == "ocr_substitution" else clean.lower()
                if kind == "ocr_substitution":
                    rto_plate = normalize_plate(noisy).lower()
                wcur.execute("DELETE FROM stg_registrations WHERE reg_no=%s", (rto_plate,))
                wcur.execute(
                    "INSERT INTO stg_registrations "
                    "(reg_id, reg_no, owner_name, make_model, registration_date, ingested_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        920000 + i,
                        rto_plate,
                        f"Noise Owner {i}",
                        random.choice(
                            ["Maruti Swift", "Hyundai Creta", "Tata Nexon", "Honda City"]
                        ),
                        date(2020, 1, 1) + timedelta(days=i * 3),
                        now,
                    ),
                )
                rto_inserts += 1
                # fetch model back for manifest
                wcur.execute(
                    "SELECT make_model FROM stg_registrations WHERE reg_no=%s",
                    (rto_plate,),
                )
                model = (wcur.fetchone() or [None])[0]
                manifest_rows.append(
                    (
                        now,
                        clean,
                        rto_plate,
                        kind + "+rto_case",
                        "rto_registration",
                        base["color"],
                        base["vtype"],
                        model,
                        "noisy RTO reg_no",
                    )
                )

    if manifest_rows:
        wcur.executemany(
            """
            INSERT INTO dwh_noisy_seed_manifest
              (created_at, clean_plate, noisy_plate, noise_kind, source_name,
               context_color, context_type, context_model, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            manifest_rows,
        )

    rc.commit()
    wh.commit()
    rcur.close()
    rc.close()
    wcur.close()
    wh.close()

    print(f"  capture_log noisy inserts:     {cap_inserts}")
    print(f"  stg_policies noisy inserts:    {ins_inserts}")
    print(f"  stg_registrations noisy inserts: {rto_inserts}")
    print(f"  manifest rows:                 {len(manifest_rows)}")
    print()
    print("Next:")
    print("  python run_etl.py")
    print("  streamlit run app.py  -> Entity match")
    print(f"  Demo noisy plate: {demo_noisy}  (clean {demo_clean}, Black / SUV)")
    print("  SQL: SELECT * FROM dwh_noisy_seed_manifest LIMIT 20;")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=40, help="how many clean bases to noisify")
    args = ap.parse_args()
    populate(pairs=max(5, args.pairs))


if __name__ == "__main__":
    main()
