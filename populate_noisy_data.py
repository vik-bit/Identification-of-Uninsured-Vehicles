"""
populate_noisy_data.py
----------------------
Put REALISTIC noise into the sources for the Phase-2 demo (the truth stays recoverable).

  roadcapture  extra sightings of real vehicles with a mis-read plate:
                 OCR slip (8->B, 0->O, 1->I ...), dropped character, swapped characters,
                 wrong format ("DL-12-AB-3456" in a compact-format source)
               + extra sightings with the WRONG COLOUR   (context anomaly)
  insurance    a real policy re-keyed under a mis-typed plate  -> vehicle looks UNINSURED
  RTO          a real registration re-keyed under a mis-typed plate -> sighting has no owner
  demo         DL9CAF8888 (Black SUV, Tata Nexon) + two look-alike decoys
               DL9CAF8883 (White Hatchback) and DL9CAF3888 (Red Sedan)
               scans: DL9CAF8B88 (OCR slip) and DL9CAF888 (lost digit: 3-way tie, context decides)

Every change is written to vehicle_dwh.dwh_noisy_seed_manifest (ground truth / answer key).
The matcher and the cleanser NEVER read that table; only evaluate_matching.py and
phase2_check.py use it to score themselves.

Re-running first UNDOES the previous seed (restores re-keyed rows, deletes inserted rows).

    python populate_noisy_data.py            # default 40 vehicles
    python populate_noisy_data.py --pairs 60
    python run_etl.py
"""

from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta

from db_config import connect
from entity_matching import PLATE_RE, fmt_for_source, is_valid_plate, normalize_plate
from staging import ensure_staging_tables

OCR_SUBS = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5",
            "8": "B", "B": "8", "2": "Z", "Z": "2", "6": "G", "G": "6", "D": "0"}
COLORS = ["White", "Black", "Silver", "Red", "Blue", "Grey"]
DEMO_LOCATION = "Phase2 Demo Gate"
DEMO_ID = 930000  # stg ids used by the demo vehicles

DEMO = [  # plate, owner, model, color, type, insurer
    ("DL9CAF8888", "Demo Owner (Nexon)", "Tata Nexon", "Black", "SUV", "ICICI Lombard"),
    ("DL9CAF8883", "Demo Decoy (Swift)", "Maruti Swift", "White", "Hatchback", "HDFC Ergo"),
    ("DL9CAF3888", "Demo Decoy (City)", "Honda City", "Red", "Sedan", "Tata AIG"),
]
DEMO_NOISY = [("DL9CAF8B88", "DL9CAF8888", "ocr_substitution"),
              ("DL9CAF888", "DL9CAF8888", "dropped_char")]


# --------------------------------------------------------------------------- manifest
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
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'dwh_noisy_seed_manifest'"
    )
    cols = {r[0].lower() for r in cur.fetchall()}
    for name, ddl in (("source_ref", "VARCHAR(80) NULL"), ("original_value", "VARCHAR(40) NULL"),
                      ("seed_action", "VARCHAR(20) NULL")):
        if name not in cols:
            cur.execute(f"ALTER TABLE dwh_noisy_seed_manifest ADD COLUMN {name} {ddl}")


def _restore_policies_lost_by_v1(wcur) -> None:
    wcur.execute("SELECT policy_id, vehicle_number FROM stg_policies WHERE insurer='NoiseDemoInsurer'")
    fakes = wcur.fetchall()
    if not fakes:
        return
    wcur.execute("SELECT vehicle_number FROM stg_policies WHERE insurer <> 'NoiseDemoInsurer' OR insurer IS NULL")
    have = {normalize_plate(v) for (v,) in wcur.fetchall()}
    lost = sorted({normalize_plate(v) for _, v in fakes if is_valid_plate(v)} - have)
    if not lost:
        return
    restored = 0
    try:  # local copy of the insurance source, if this machine has one
        ic = connect("insurance")
        icur = ic.cursor()
        icur.execute("SELECT policy_id, vehicle_number, insurer, valid_from, valid_to FROM policies")
        by_plate = {normalize_plate(r[1]): r for r in icur.fetchall()}
        icur.close(); ic.close()
        for p in lost:
            r = by_plate.get(p)
            if r:
                wcur.execute("INSERT INTO stg_policies (policy_id, vehicle_number, insurer, valid_from, valid_to, "
                             "ingested_at) VALUES (%s,%s,%s,%s,%s,%s)", (*r, datetime.now()))
                restored += 1
    except Exception as e:
        print(f"  [warn] insurance source not reachable ({e.__class__.__name__})")
    missing = len(lost) - restored
    if restored or missing:
        print(f"  first-version seed had overwritten real policies: restored {restored} from the insurance source")
    if missing and restored:
        print(f"  ({missing} fake policies had no real policy behind them -> simply removed)")
    elif missing:
        print(f"  [warn] {missing} vehicles may have lost their real policy; insurance source not reachable here.")
        print("         Ask the insurance teammate to re-push (teammate_push/push_to_warehouse.py), then re-run.")
        print("   ", ", ".join(lost[:20]) + (" ..." if len(lost) > 20 else ""))


def clear_previous_seed() -> None:
    """Undo the previous seed (old and new style) so re-runs are clean."""
    wh = connect("vehicle_dwh")
    wcur = wh.cursor()
    ensure_staging_tables(wcur)
    ensure_manifest(wcur)
    wcur.execute("SELECT noisy_plate, clean_plate, source_name, source_ref, original_value, seed_action, "
                 "noise_kind FROM dwh_noisy_seed_manifest")
    rows = wcur.fetchall()
    rc = connect("roadcapture")
    rcur = rc.cursor()
    ghosts = set()
    for noisy, clean, src, ref, orig, action, kind in rows:
        ghosts.add(normalize_plate(noisy))
        if src == "roadcapture":
            if action == "inserted" and ref:
                rcur.execute("DELETE FROM capture_log WHERE capture_id=%s", (int(ref),))
            elif not action:  # first-version manifest rows: delete by plate
                rcur.execute("DELETE FROM capture_log WHERE plate_no=%s", (noisy,))
        elif src == "insurance" and action == "rekeyed" and orig:
            pid = int(ref.split("|")[0])
            wcur.execute("UPDATE stg_policies SET vehicle_number=%s WHERE policy_id=%s", (orig, pid))
        elif src == "rto_registration" and action == "rekeyed" and orig:
            rid = int(ref.split("|")[0])
            wcur.execute("UPDATE stg_registrations SET reg_no=%s WHERE reg_id=%s", (orig, rid))
    # first-version seed: it DELETED the real policy when its "noisy" spelling equalled the real one,
    # then inserted a fake NoiseDemoInsurer row. Put the real policy back before removing the fake.
    _restore_policies_lost_by_v1(wcur)
    wcur.execute("DELETE FROM stg_policies WHERE insurer='NoiseDemoInsurer' OR policy_id BETWEEN 920000 AND 929999")
    wcur.execute("DELETE FROM stg_registrations WHERE owner_name LIKE 'Noise Owner %%' "
                 "OR reg_id BETWEEN 920000 AND 929999")
    # demo vehicles
    wcur.execute("DELETE FROM stg_policies WHERE policy_id BETWEEN %s AND %s", (DEMO_ID, DEMO_ID + 99))
    wcur.execute("DELETE FROM stg_registrations WHERE reg_id BETWEEN %s AND %s", (DEMO_ID, DEMO_ID + 99))
    rcur.execute("DELETE FROM capture_log WHERE location=%s", (DEMO_LOCATION,))
    for p, *_ in DEMO:
        rcur.execute("DELETE FROM capture_log WHERE plate_no=%s", (p,))
        ghosts.add(p)
    for p, *_ in DEMO_NOISY:
        rcur.execute("DELETE FROM capture_log WHERE plate_no=%s", (p,))
        ghosts.add(p)
    # ghost warehouse rows the noise created (noisy spelling != clean plate)
    demo_plates = {d[0] for d in DEMO} | {n for n, *_ in DEMO_NOISY}
    for noisy, clean, *_ in rows:
        g = normalize_plate(noisy)
        if g and g != normalize_plate(clean):
            wcur.execute("DELETE FROM vehicle_360 WHERE plate_canonical=%s", (g,))
    for g in demo_plates:
        wcur.execute("DELETE FROM vehicle_360 WHERE plate_canonical=%s", (g,))
    del ghosts
    wcur.execute("DELETE FROM dwh_noisy_seed_manifest")
    rc.commit()
    wh.commit()
    rcur.close(); rc.close(); wcur.close(); wh.close()
    print(f"  undid previous seed ({len(rows)} manifest rows)")


# --------------------------------------------------------------------------- noise makers
def ocr_slip(plate: str, rng: random.Random, k: int = 1) -> str | None:
    """Swap k look-alike characters. Prefer a result that breaks the plate grammar
    (like a real mis-read B in a digit slot) so the noise is recognisable, not a new valid plate."""
    idx = [i for i, c in enumerate(plate) if c in OCR_SUBS]
    if not idx:
        return None
    out = None
    for _ in range(12):
        chars = list(plate)
        for i in rng.sample(idx, min(k, len(idx))):
            chars[i] = OCR_SUBS[chars[i]]
        out = "".join(chars)
        if out != plate and not is_valid_plate(out):
            return out
    return out


def dropped_char(plate: str, rng: random.Random) -> str:
    i = rng.randrange(2, len(plate))
    return plate[:i] + plate[i + 1:]


def swapped_chars(plate: str, rng: random.Random) -> str | None:
    opts = [i for i in range(2, len(plate) - 1) if plate[i] != plate[i + 1]]
    if not opts:
        return None
    i = rng.choice(opts)
    return plate[:i] + plate[i + 1] + plate[i] + plate[i + 2:]


def hyphenate_like(clean: str, noisy: str) -> str:
    """Insurance spelling of a noisy plate, keeping the clean plate's part lengths."""
    m = PLATE_RE.match(clean)
    if not m or len(noisy) != len(clean):
        return noisy
    a, b, c, _ = (len(g) for g in m.groups())
    return f"{noisy[:a]}-{noisy[a:a + b]}-{noisy[a + b:a + b + c]}-{noisy[a + b + c:]}"


# --------------------------------------------------------------------------- populate
def _fetch_bases(n: int, rng: random.Random, known: set[str]) -> list[dict]:
    rc = connect("roadcapture")
    rcur = rc.cursor()
    rcur.execute("SELECT capture_id, plate_no, vehicle_color, vehicle_type, location FROM capture_log")
    caps: dict[str, list] = {}
    for cid, plate, col, typ, loc in rcur.fetchall():
        caps.setdefault(normalize_plate(plate), []).append((cid, col, typ, loc))
    rcur.close(); rc.close()

    wh = connect("vehicle_dwh")
    wcur = wh.cursor()
    wcur.execute("SELECT reg_id, reg_no, make_model FROM stg_registrations")
    regs = {normalize_plate(r[1]): r for r in wcur.fetchall()}
    wcur.execute("SELECT policy_id, vehicle_number FROM stg_policies")
    pols: dict[str, list] = {}
    for pid, vn in wcur.fetchall():
        pols.setdefault(normalize_plate(vn), []).append((pid, vn))
    wcur.close(); wh.close()

    eligible = sorted(p for p in caps if is_valid_plate(p) and p in regs and p not in known)
    rng.shuffle(eligible)
    out = []
    for p in eligible[:n]:
        c = caps[p]
        out.append({"plate": p, "color": c[0][1] or "White", "vtype": c[0][2] or "Sedan",
                    "location": c[0][3], "n_caps": len(c), "reg": regs[p],
                    "policies": [x for x in pols.get(p, []) if x[0] is not None]})
    return out


def populate(pairs: int = 40, seed: int = 20261018) -> dict:
    rng = random.Random(seed)
    print(f"Populating noisy data for ~{pairs} real vehicles (+ demo set)…")
    clear_previous_seed()

    wh = connect("vehicle_dwh")
    wcur = wh.cursor()
    ensure_staging_tables(wcur)
    ensure_manifest(wcur)
    rc = connect("roadcapture")
    rcur = rc.cursor()

    try:
        wcur.execute("SELECT last_processed_at FROM etl_watermark WHERE source_name='capture_log'")
        wm = wcur.fetchone()
        watermark = wm[0] if wm and wm[0] else datetime(2000, 1, 1)
    except Exception:
        watermark = datetime(2000, 1, 1)
    t = [max(datetime.now(), watermark) + timedelta(minutes=1)]

    def tick() -> datetime:
        t[0] += timedelta(seconds=7)
        return t[0]

    now = datetime.now()
    manifest: list[tuple] = []
    counts = {"capture": 0, "context": 0, "insurance": 0, "rto": 0}

    def add_capture(plate, color, vtype, loc) -> int:
        rcur.execute("INSERT INTO capture_log (plate_no, capture_time, location, vehicle_color, vehicle_type) "
                     "VALUES (%s,%s,%s,%s,%s)", (plate, tick(), loc, color, vtype))
        return rcur.lastrowid

    def note(clean, noisy, kind, src, color, vtype, model, ref, orig, action, text):
        manifest.append((now, clean, noisy, kind, src, color, vtype, model, text, ref, orig, action))

    # ---- demo vehicles (registered + insured + seen twice) ----
    for k, (plate, owner, model, color, vtype, insurer) in enumerate(DEMO):
        wcur.execute("INSERT INTO stg_registrations (reg_id, reg_no, owner_name, make_model, registration_date, "
                     "ingested_at) VALUES (%s,%s,%s,%s,%s,%s)",
                     (DEMO_ID + k, fmt_for_source(plate, "rto_registration"), owner, model, date(2022, 3, 15), now))
        wcur.execute("INSERT INTO stg_policies (policy_id, vehicle_number, insurer, valid_from, valid_to, ingested_at) "
                     "VALUES (%s,%s,%s,%s,%s,%s)",
                     (DEMO_ID + k, fmt_for_source(plate, "insurance"), insurer, date(2026, 1, 1), date(2027, 1, 1), now))
        for _ in range(2):
            add_capture(plate, color, vtype, DEMO_LOCATION)
    for noisy, clean, kind in DEMO_NOISY:
        cid = add_capture(noisy, "Black", "SUV", DEMO_LOCATION)
        note(clean, noisy, kind, "roadcapture", "Black", "SUV", "Tata Nexon", str(cid), None, "inserted",
             "demo scan")
        counts["capture"] += 1

    # ---- real vehicles ----
    known = {d[0] for d in DEMO}
    bases = _fetch_bases(pairs, rng, known)
    for i, b in enumerate(bases):
        clean, model = b["plate"], b["reg"][2]
        # 1) a mis-read sighting (every vehicle)
        kind_cycle = ["ocr_substitution", "ocr_substitution", "dropped_char", "ocr_substitution",
                      "swapped_chars", "ocr_double", "format_variant", "ocr_substitution"]
        kind = kind_cycle[i % len(kind_cycle)]
        noisy = {
            "ocr_substitution": lambda: ocr_slip(clean, rng, 1),
            "ocr_double": lambda: ocr_slip(clean, rng, 2),
            "dropped_char": lambda: dropped_char(clean, rng),
            "swapped_chars": lambda: swapped_chars(clean, rng),
            "format_variant": lambda: fmt_for_source(clean, "insurance"),
        }[kind]() or ocr_slip(clean, rng, 1)
        if noisy and normalize_plate(noisy) not in known:
            cid = add_capture(noisy, b["color"], b["vtype"], b["location"])
            note(clean, noisy, kind, "roadcapture", b["color"], b["vtype"], model, str(cid), None, "inserted",
                 "mis-read sighting")
            counts["capture"] += 1

        # 2) wrong colour on an extra sighting. A majority needs >= 2 correct sightings, so for a
        #    vehicle seen once we first add one more correct sighting (removed again by --undo).
        if i % 5 == 1:
            for _ in range(max(0, 2 - b["n_caps"])):
                cid = add_capture(clean, b["color"], b["vtype"], b["location"])
                note(clean, clean, "support_sighting", "roadcapture", b["color"], b["vtype"], model, str(cid),
                     None, "inserted", "extra correct sighting so the colour majority is defined")
            wrong = rng.choice([c for c in COLORS if c != b["color"]])
            cid = add_capture(clean, wrong, b["vtype"], b["location"])
            note(clean, clean, "context_color", "roadcapture", b["color"], b["vtype"], model, str(cid), None,
                 "inserted", f"recorded colour {wrong}, true {b['color']}")
            counts["context"] += 1

        # 3) insurance re-keyed under a typo (never together with an RTO typo)
        if i % 2 == 0 and b["policies"]:
            pid, native = b["policies"][0]
            slip = ocr_slip(clean, rng, 1)
            if slip:
                bad = hyphenate_like(clean, slip)
                wcur.execute("UPDATE stg_policies SET vehicle_number=%s WHERE policy_id=%s AND vehicle_number=%s",
                             (bad, pid, native))
                note(clean, bad, "ocr_substitution", "insurance", b["color"], b["vtype"], model,
                     f"{pid}|{bad}", native, "rekeyed", "policy filed under mis-typed plate")
                counts["insurance"] += 1
        # 4) RTO re-keyed under a typo
        elif i % 3 == 1:
            rid, native, _ = b["reg"]
            slip = ocr_slip(clean, rng, 1)
            if slip and rid is not None:
                bad = slip.lower()
                wcur.execute("UPDATE stg_registrations SET reg_no=%s WHERE reg_id=%s AND reg_no=%s",
                             (bad, rid, native))
                note(clean, bad, "ocr_substitution", "rto_registration", b["color"], b["vtype"], model,
                     f"{rid}|{bad}", native, "rekeyed", "registration keyed with typo")
                counts["rto"] += 1

    wcur.executemany(
        """
        INSERT INTO dwh_noisy_seed_manifest
          (created_at, clean_plate, noisy_plate, noise_kind, source_name, context_color, context_type,
           context_model, note, source_ref, original_value, seed_action)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        manifest,
    )
    rc.commit()
    wh.commit()
    rcur.close(); rc.close(); wcur.close(); wh.close()

    print(f"  noisy sightings (capture_log):        {counts['capture']}")
    print(f"  wrong-colour sightings (capture_log): {counts['context']}")
    print(f"  policies re-keyed (stg_policies):     {counts['insurance']}")
    print(f"  registrations re-keyed (stg_regs):    {counts['rto']}")
    print(f"  manifest rows (answer key):           {len(manifest)}")
    print("\nNext:  python run_etl.py   then   streamlit run app.py  ->  Entity match")
    print("Demo scans: DL9CAF8B88 (OCR slip) and DL9CAF888 (lost digit)  ->  DL9CAF8888 Black SUV Tata Nexon")
    return {**counts, "manifest": len(manifest)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=40, help="how many real vehicles to make noisy")
    ap.add_argument("--undo", action="store_true", help="only undo the previous seed")
    args = ap.parse_args()
    if args.undo:
        clear_previous_seed()
        return
    populate(pairs=max(5, args.pairs))


if __name__ == "__main__":
    main()
