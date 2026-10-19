"""
phase2_check.py — one command that checks every Phase-2 rubric item against YOUR databases.

    python phase2_check.py           read-only: what is in the DB right now + rubric evidence
    python phase2_check.py --fix     also cleanse every anomaly, refresh ETL, check again
    python phase2_check.py --full    rehearsal: re-seed noise -> ETL -> match -> cleanse -> verify
                                     against the answer key -> simulate the evaluator's live
                                     INSERT / UPDATE / DELETE -> cleanse again -> verify

Prints [PASS] / [FAIL] / [INFO] per item and a summary at the end.
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
RESULTS: list[tuple[str, str, str]] = []


def mark(item: str, ok: bool | None, detail: str = "") -> None:
    tag = "INFO" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append((tag, item, detail))
    print(f"  [{tag}] {item}" + (f" — {detail}" if detail else ""))


def header(t: str) -> None:
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


# --------------------------------------------------------------------------- setup
def check_connections() -> bool:
    from db_config import connect
    ok = True
    for db in ("vehicle_dwh", "roadcapture", "theft_records"):
        try:
            connect(db).close()
            mark(f"connect {db}", True)
        except Exception as e:
            mark(f"connect {db}", False, str(e)[:120])
            ok = False
    return ok


def run_py(script: str, *args: str) -> str:
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    res = subprocess.run([sys.executable, str(HERE / script), *args], cwd=str(HERE), env=env,
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"{script} failed:\n{res.stdout[-800:]}\n{res.stderr[-800:]}")
    return res.stdout


# --------------------------------------------------------------------------- rubric 1
def rubric1() -> None:
    import entity_matching as em
    from plate_ocr import DEMO_SCANS, SAMPLE_DIR, extract_from_image, render_plate_image

    header("RUBRIC 1 — scanned image -> approximately matching vehicles (and vice-versa)")
    em.invalidate_cache()

    # 1a image -> plate (pixels)
    for plate, color, vtype in DEMO_SCANS:
        path = SAMPLE_DIR / f"scan_{plate}.png"
        if not path.exists():
            render_plate_image(plate, vehicle_color=color, vehicle_type=vtype)
        r = extract_from_image(path.read_bytes(), path.name)
        pixel = "pixels" in r["method"]
        mark(f"image {path.name} -> plate", r["plate"] == plate and pixel,
             f"read '{r['plate']}' via {r['method']}, colour≈{r['color']}, type={r['type'] or '-'}")

    # every sample scan: does the pixel reader reproduce the plate printed on it?
    import re as _re
    scans = sorted(SAMPLE_DIR.glob("scan_*.png"))
    ok_n, bad = 0, []
    for p in scans:
        want = _re.sub(r"[^A-Z0-9]", "", p.stem[5:].upper())
        r = extract_from_image(p.read_bytes(), p.name)
        if r["plate"] == want and "pixels" in r["method"]:
            ok_n += 1
        else:
            bad.append(f"{p.name}->{r['plate']} ({r['method']})")
    if scans:
        mark("all sample scans read from pixels", ok_n == len(scans),
             f"{ok_n}/{len(scans)} correct" + (f"; misses: {bad[:4]}" if bad else ""))

    from plate_ocr import tesseract_status
    ts = tesseract_status()
    mark("real-photo OCR engine (Tesseract)", ts.startswith("ready") or None, ts)

    # 1a' a CLEAN plate damaged like a bad camera scan: the OCR itself produces the noise
    from PIL import Image
    from plate_ocr import degrade_image, template_ocr_detail
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "p2_clean_scan.png"
    render_plate_image("DL9CAF8888", vehicle_color="Black", vehicle_type="SUV", out_path=tmp)
    bad = degrade_image(Image.open(tmp), blur=1.0, dust=0.01, smudge_char=8)
    text, confs = template_ocr_detail(bad)
    wild = "".join(ch if c >= 0.60 else "?" for ch, c in zip(text, confs)) if len(text) == len(confs) else text
    hits = em.find_approximate_matches(wild, query_color="Black", query_type="SUV")
    mark("damaged scan of a clean plate -> OCR misread -> still matched", text != "DL9CAF8888" and bool(hits)
         and hits[0].plate_canonical == "DL9CAF8888",
         f"OCR read '{text}' (min conf {min(confs or [0]):.2f}) -> used '{wild}' -> "
         + ", ".join(f"{h.plate_canonical}:{h.score:.2f}" for h in hits[:3]))
    tmp.unlink(missing_ok=True)

    # 1b noisy -> vehicles
    hits = em.find_approximate_matches("DL9CAF8B88", query_color="Black", query_type="SUV", query_model="Tata Nexon")
    mark("noisy DL9CAF8B88 -> DL9CAF8888 (OCR 8<->B)", bool(hits) and hits[0].plate_canonical == "DL9CAF8888",
         ", ".join(f"{h.plate_canonical}:{h.score:.2f}" for h in hits[:3]))
    no_ctx = em.find_approximate_matches("DL9CAF888", min_score=0.5)
    with_ctx = em.find_approximate_matches("DL9CAF888", min_score=0.5, query_color="Black", query_type="SUV",
                                           query_model="Tata Nexon")
    tie = len(no_ctx) >= 3 and abs(no_ctx[0].score - no_ctx[2].score) < 0.02
    mark("lost digit DL9CAF888: string alone is a 3-way tie", tie,
         ", ".join(f"{h.plate_canonical}:{h.score:.2f}" for h in no_ctx[:3]))
    mark("... colour/type/model context picks DL9CAF8888", bool(with_ctx) and with_ctx[0].plate_canonical == "DL9CAF8888",
         ", ".join(f"{h.plate_canonical}:{h.score:.2f}({','.join(h.context_reasons)})" for h in with_ctx[:3]))

    # 1c vice-versa
    rows = em.find_records_near_plate("DL9CAF8888")
    noisy = [r for r in rows if r["verdict"].startswith("noisy")]
    imgs = [r for r in rows if r["source"] == "scanned_image"]
    mark("vice-versa DL9CAF8888 -> noisy records / scans", len(noisy) > 0,
         f"{len(noisy)} noisy ({len(imgs)} scanned images): "
         + ", ".join(f"{r['source']}:{r['native_plate']}" for r in noisy[:5]))

    # 1d accuracy benchmark
    import evaluate_matching as ev
    res = ev.run(n=600, save=True, verbose=False)
    ov = res["overall"]
    best = max(ov, key=lambda k: (ov[k]["top1"], ov[k]["mrr"]))
    print("\n    method            top-1   top-3    MRR")
    for k, m in ov.items():
        print(f"    {k:<17}{m['top1']:>6.1%}{m['top3']:>8.1%}{m['mrr']:>7.3f}")
    mark("combined+context is the most accurate algorithm", best == "combined+context",
         f"top-1 {ov['combined+context']['top1']:.1%} vs Levenshtein {ov['levenshtein']['top1']:.1%}, "
         f"Jaro-Winkler {ov['jaro_winkler']['top1']:.1%}")
    mark("accuracy >= 95% on held-out noisy plates", ov["combined+context"]["top1"] >= 0.95)
    s = res.get("seeded_noise") or {}
    if s.get("n"):
        mark("seeded noise in the DB recovered (answer key)", s["top1"] >= 0.95, f"{s['top1']:.1%} of {s['n']}")


# --------------------------------------------------------------------------- rubric 2
def verify_against_answer_key() -> None:
    """Every seeded noisy record must now carry the clean plate (and the right colour)."""
    from db_config import connect
    from entity_matching import normalize_plate

    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    try:
        cur.execute("SELECT clean_plate, noisy_plate, noise_kind, source_name, context_color, source_ref, seed_action "
                    "FROM dwh_noisy_seed_manifest")
        rows = cur.fetchall()
    except Exception:
        rows = []
    if not rows:
        mark("answer-key verification", None, "no manifest (run populate_noisy_data.py)")
        return
    rc = connect("roadcapture")
    rcur = rc.cursor()
    bad = []
    for clean, noisy, kind, src, color, ref, action in rows:
        if src == "roadcapture" and ref:
            rcur.execute("SELECT plate_no, vehicle_color FROM capture_log WHERE capture_id=%s", (int(ref),))
            r = rcur.fetchone()
            if r is None:
                continue  # evaluator deleted it — fine
            if r[0] != clean or (kind.startswith("context") and r[1] != color):
                bad.append(f"capture#{ref}={r[0]}/{r[1]} (want {clean}/{color})")
        elif src in ("insurance", "rto_registration") and ref:
            table, idc, pc = (("stg_policies", "policy_id", "vehicle_number") if src == "insurance"
                              else ("stg_registrations", "reg_id", "reg_no"))
            cur.execute(f"SELECT {pc} FROM {table} WHERE {idc}=%s", (int(ref.split('|')[0]),))
            got = [normalize_plate(x[0]) for x in cur.fetchall()]
            if got and clean not in got:
                bad.append(f"{src}#{ref.split('|')[0]}={got} (want {clean})")
    rcur.close(); rc.close(); cur.close(); conn.close()
    mark("cleansed values equal the TRUE values (answer key)", not bad,
         f"{len(rows)} seeded changes checked" + (f"; wrong: {bad[:5]}" if bad else ""))


def rubric2(fix: bool) -> None:
    import entity_matching as em

    header("RUBRIC 2 — cleanse recorded data using image + context, no anomalies left")
    em.invalidate_cache()
    anoms = em.detect_anomalies()
    summ = em.anomaly_summary(anoms)
    mark("anomaly scan of all 4 sources + warehouse", None, f"{sum(summ.values())} found {summ}")
    for a in anoms[:6]:
        print(f"      {a.kind:<20}{a.source:<17}{a.current:<16}-> {a.suggested or '?':<16}{a.decision:<7}{a.reason[:60]}")

    # one interactive cleanse with image context (what the evaluator will watch)
    if any(a.kind == "NOISY_PLATE" and em.normalize_plate(a.current) == "DL9CAF8B88" for a in anoms) and fix:
        r = em.cleanse_noisy_record(noisy_plate="DL9CAF8B88", chosen_plate="DL9CAF8888", match_score=0.97,
                                    query_color="Black", query_type="SUV", query_model="Tata Nexon",
                                    note="phase2_check demo")
        mark("cleanse one noisy record (DL9CAF8B88 -> DL9CAF8888)", r["changes"] > 0, "; ".join(r["actions"])[:160])

    if not fix:
        mark("no anomalies left", not anoms, "" if not anoms else "run with --fix (or Cleanse all in the app)")
        return
    res = em.cleanse_all()
    mark("cleanse_all applied fixes", res["applied"] > 0 or not anoms,
         f"{res['applied']} changes in {res['passes']} pass(es); {res['etl']}")
    left = res["after"]
    mark("no anomalies left after cleansing", not left, str(left) if left else "0 remaining")
    for a in res["remaining"][:5]:
        print(f"      left: {a['kind']} {a['source']} {a['current']} ({a['reason']})")
    verify_against_answer_key()


def evaluator_simulation() -> None:
    """Do what the evaluator may do live, then prove the cleanse handles it."""
    import entity_matching as em
    from db_config import connect

    header("RUBRIC 2 — evaluator live edits (INSERT wrong plate / UPDATE colour / DELETE row)")
    em.invalidate_cache()
    u = em.get_universe(max_age=0)
    rng = random.Random(2026)
    pool = sorted(p for p, e in u.entities.items()
                  if e.trusted and e.registered and e.color and e.vtype and "roadcapture" in e.sources
                  and len(u.color_votes.get(p, {})) == 1)
    if len(pool) < 2:
        mark("evaluator simulation", False, "not enough captured + registered vehicles in the DB")
        return
    target_insert, target_color = rng.sample(pool, 2)
    true_color = u.entities[target_color].color

    def misread(p: str) -> str:
        """A camera mis-read that breaks the plate grammar: a digit read as its look-alike letter."""
        look = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
        for i in range(len(p) - 1, len(p) - 5, -1):
            if p[i] in look:
                return p[:i] + look[p[i]] + p[i + 1:]
        return p[:-1]  # no look-alike digit: the last digit was not read

    noisy = misread(target_insert)
    support_ids: list[int] = []
    rc = connect("roadcapture")
    cur = rc.cursor()
    when = datetime.now() + timedelta(days=1)
    cur.execute("INSERT INTO capture_log (plate_no, capture_time, location, vehicle_color, vehicle_type) "
                "VALUES (%s,%s,%s,%s,%s)", (noisy, when, "Evaluator Test Gate", u.entities[target_insert].color,
                                             u.entities[target_insert].vtype))
    ins_id = cur.lastrowid
    # the colour majority needs >= 2 other correct sightings; add them if the vehicle was seen less
    n_seen = sum(u.color_votes[target_color].values())
    for _ in range(max(0, 3 - n_seen)):
        cur.execute("INSERT INTO capture_log (plate_no, capture_time, location, vehicle_color, vehicle_type) "
                    "VALUES (%s,%s,%s,%s,%s)", (target_color, when, "Evaluator Test Gate", true_color,
                                                 u.entities[target_color].vtype))
        support_ids.append(cur.lastrowid)
    cur.execute("SELECT capture_id FROM capture_log WHERE plate_no=%s ORDER BY capture_id LIMIT 1", (target_color,))
    col_id = cur.fetchone()[0]
    wrong = "Red" if true_color != "Red" else "Blue"
    cur.execute("UPDATE capture_log SET vehicle_color=%s WHERE capture_id=%s", (wrong, col_id))
    rc.commit()
    cur.close(); rc.close()
    print(f"  INSERT capture#{ins_id} plate {noisy}   (really {target_insert})")
    print(f"  UPDATE capture#{col_id} colour {true_color} -> {wrong}   (vehicle {target_color})")
    print("  ETL picks the noisy sighting up as a new (ghost) vehicle ...")
    run_py("run_etl.py")
    em.invalidate_cache()
    hits = em.find_approximate_matches(noisy, query_color=u.entities[target_insert].color,
                                       query_type=u.entities[target_insert].vtype)
    mark(f"inserted noisy plate {noisy} matches {target_insert}", bool(hits) and hits[0].plate_canonical == target_insert,
         ", ".join(f"{h.plate_canonical}:{h.score:.2f}" for h in hits[:3]))
    found = {(a.kind, a.record_ref) for a in em.detect_anomalies()}
    mark("live INSERT detected as NOISY_PLATE", ("NOISY_PLATE", str(ins_id)) in found)
    mark("live UPDATE detected as CONTEXT_CONFLICT", ("CONTEXT_CONFLICT", str(col_id)) in found)
    res = em.cleanse_all()
    mark("cleanse after live edits -> no anomalies", not res["after"], f"{res['applied']} changes; {res['etl']}")
    rc = connect("roadcapture")
    cur = rc.cursor()
    cur.execute("SELECT plate_no FROM capture_log WHERE capture_id=%s", (ins_id,))
    p1 = cur.fetchone()[0]
    cur.execute("SELECT vehicle_color FROM capture_log WHERE capture_id=%s", (col_id,))
    c1 = cur.fetchone()[0]
    cur.close(); rc.close()
    mark("inserted row now carries the true plate", p1 == target_insert, f"{p1}")
    mark("modified colour restored from the other sightings", c1 == true_color, f"{c1}")
    wh = connect("vehicle_dwh")
    wc = wh.cursor()
    wc.execute("SELECT COUNT(*) FROM vehicle_360 WHERE plate_canonical=%s", (em.normalize_plate(noisy),))
    mark("no ghost warehouse row left for the noisy plate", wc.fetchone()[0] == 0)
    wc.close(); wh.close()

    # DELETE: remove the only sighting of a vehicle -> warehouse row must not linger as a ghost
    rc = connect("roadcapture")
    cur = rc.cursor()
    cur.execute("DELETE FROM capture_log WHERE capture_id=%s", (ins_id,))
    for sid in support_ids:  # test helper rows
        cur.execute("DELETE FROM capture_log WHERE capture_id=%s", (sid,))
    rc.commit()
    cur.close(); rc.close()
    em.invalidate_cache()
    mark("live DELETE of a row -> data still consistent", not em.detect_anomalies(),
         "warehouse still backed by the vehicle's other records")


# --------------------------------------------------------------------------- rubric 3
def rubric3() -> None:
    header("RUBRIC 3 — prompt (vibe coding), generated code, highlighted changes")
    vibe = HERE / "phase2_vibe"
    need = ["PROMPTS.md", "CHANGES.md", "generated/entity_matching.py", "generated/populate_noisy_data.py",
            "generated/plate_ocr.py"]
    for n in need:
        mark(f"phase2_vibe/{n}", (vibe / n).exists())
    try:
        out = run_py("phase2_vibe/show_changes.py")
        mark("diff report generated (phase2_vibe/changes.html)", (vibe / "changes.html").exists(),
             out.strip().splitlines()[-1] if out.strip() else "")
    except Exception as e:
        mark("diff report generated", False, str(e)[:120])


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true", help="cleanse every anomaly and re-check")
    ap.add_argument("--full", action="store_true", help="re-seed noise and rehearse the whole viva")
    ap.add_argument("--pairs", type=int, default=40)
    a = ap.parse_args()

    header(f"Phase-2 check  ({datetime.now():%Y-%m-%d %H:%M})  mode={'full' if a.full else 'fix' if a.fix else 'read-only'}")
    if not check_connections():
        print("\nStart the MySQL containers first (roadcapture_db/, theft_records_db/, warehouse MySQL).")
        sys.exit(1)
    if a.full:
        print("  re-seeding noise + ETL ...")
        print("  " + run_py("populate_noisy_data.py", "--pairs", str(a.pairs)).strip().replace("\n", "\n  "))
        run_py("run_etl.py")
        from plate_ocr import generate_samples_from_manifest
        n = len(generate_samples_from_manifest(30))
        mark("sample scans regenerated (sample_plates/)", n > 0, f"{n} images")

    rubric1()
    rubric2(fix=a.fix or a.full)
    if a.full:
        evaluator_simulation()
    rubric3()

    header("SUMMARY")
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"  {sum(r[0] == 'PASS' for r in RESULTS)} passed, {len(fails)} failed, "
          f"{sum(r[0] == 'INFO' for r in RESULTS)} info")
    for _, item, detail in fails:
        print(f"  FAIL: {item} — {detail}")
    if not fails:
        print("  All rubric checks pass.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
