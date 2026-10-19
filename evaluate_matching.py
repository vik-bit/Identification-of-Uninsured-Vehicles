"""
evaluate_matching.py — RUBRIC 1 evidence: which matching algorithm is most accurate?

Builds a labelled test set from the warehouse itself:
  take N real (trusted) vehicles, corrupt each plate the way a scanner / clerk would
  (OCR slip, two slips, dropped char, extra char, swapped chars, slip + drop,
  unreadable char '?', format),
  attach the vehicle's context (colour / type / model; 15% of the time the colour is
  WRONG, like a bad pixel estimate), then ask every algorithm to find the original
  among all registered vehicles.

Algorithms compared (all rank the same candidate pool):
  exact            normalise + exact lookup            (what phase 1 did)
  levenshtein      plain edit distance
  jaro_winkler     prefix-weighted similarity
  ocr_weighted     edit distance, OCR look-alikes cost 0.25
  combined         weighted sum of the four string signals (weights grid-searched on DEV)
  combined+context combined + colour/type/model agreement      <- what the app uses

Metrics (on the held-out TEST half): top-1 accuracy, top-3 recall, MRR.
Ties are counted pessimistically (a tie with a wrong plate is NOT a win).
Also scores the seeded noise from dwh_noisy_seed_manifest (the answer key).

    python evaluate_matching.py              # 600 synthetic queries + seeded noise
    python evaluate_matching.py --n 1000
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import entity_matching as em
from db_config import connect

HERE = Path(__file__).resolve().parent
RESULT_JSON = HERE / "match_eval_results.json"

NOISE_KINDS = ["ocr_1", "ocr_2", "dropped", "inserted", "swapped", "ocr+dropped", "unreadable", "format"]
OCR_SUBS = {"0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5", "8": "B", "B": "8",
            "2": "Z", "Z": "2", "6": "G", "G": "6", "D": "0"}
COLORS = ["White", "Black", "Silver", "Red", "Blue", "Grey"]


def corrupt(p: str, kind: str, rng: random.Random) -> str:
    def ocr(s, k):
        idx = [i for i, c in enumerate(s) if c in OCR_SUBS]
        chars = list(s)
        for i in rng.sample(idx, min(k, len(idx))):
            chars[i] = OCR_SUBS[chars[i]]
        return "".join(chars)

    def drop(s):
        i = rng.randrange(2, len(s))
        return s[:i] + s[i + 1:]

    if kind == "ocr_1":
        return ocr(p, 1)
    if kind == "ocr_2":
        return ocr(p, 2)
    if kind == "dropped":
        return drop(p)
    if kind == "inserted":
        i = rng.randrange(2, len(p) + 1)
        return p[:i] + rng.choice("0123456789ABCDEFGHJKLMNPRSTUVWXYZ") + p[i:]
    if kind == "swapped":
        opts = [i for i in range(2, len(p) - 1) if p[i] != p[i + 1]] or [2]
        i = rng.choice(opts)
        return p[:i] + p[i + 1] + p[i] + p[i + 2:]
    if kind == "ocr+dropped":
        return drop(ocr(p, 1))
    if kind == "unreadable":  # mud / glare: OCR confidence too low -> character marked '?'
        i = rng.randrange(2, len(p))
        return p[:i] + "?" + p[i + 1:]
    return rng.choice([em.fmt_for_source(p, "insurance"), p.lower(), em.fmt_for_source(p, "theft_records")])


def build_queries(u: em.Universe, n: int, rng: random.Random) -> list[dict]:
    refs = sorted(p for p, e in u.entities.items() if e.trusted and e.registered)
    rng.shuffle(refs)
    out = []
    for i, p in enumerate(refs[:n]):
        e = u.entities[p]
        kind = NOISE_KINDS[i % len(NOISE_KINDS)]
        color = e.color
        if color and rng.random() < 0.15:
            color = rng.choice([c for c in COLORS if c != color])
        out.append({"truth": p, "query": corrupt(p, kind, rng), "kind": kind,
                    "color": color, "type": e.vtype, "model": e.model})
    return out


MODELS = ["Maruti Swift", "Hyundai Creta", "Tata Nexon", "Honda City", "Mahindra Scorpio", "Kia Seltos"]
TYPES = ["Sedan", "SUV", "Hatchback"]


def lookalike_neighbours(p: str, rng: random.Random, k: int = 24) -> list[em.Entity]:
    """
    Realistic neighbours of a plate. RTOs allot numbers sequentially inside a series, so a
    real registry holds DL9CAF8887, DL9CAF8898, DL9CAF8888's series-mates, etc.
    Our synthetic data is random (sparse), so the benchmark adds them explicitly.
    """
    m = em.PLATE_RE.match(p)
    if not m:
        return []
    s, r, ser, num = m.groups()
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    out: set[str] = set()
    tries = 0
    while len(out) < k and tries < 400:
        tries += 1
        kind = rng.random()
        n = list(num)
        if kind < 0.45:                       # one digit different (sequential neighbour)
            i = rng.randrange(4)
            n[i] = rng.choice([d for d in "0123456789" if d != n[i]])
            cand = f"{s}{r}{ser}{''.join(n)}"
        elif kind < 0.60:                     # two digits different
            for i in rng.sample(range(4), 2):
                n[i] = rng.choice([d for d in "0123456789" if d != n[i]])
            cand = f"{s}{r}{ser}{''.join(n)}"
        elif kind < 0.70:                     # adjacent digits swapped (e.g. 8127 / 8172)
            i = rng.randrange(3)
            n[i], n[i + 1] = n[i + 1], n[i]
            cand = f"{s}{r}{ser}{''.join(n)}"
        elif kind < 0.90:                     # same number, neighbouring series letter
            sl = list(ser)
            i = rng.randrange(len(sl))
            sl[i] = rng.choice([c for c in letters if c != sl[i]])
            cand = f"{s}{r}{''.join(sl)}{num}"
        else:                                 # neighbouring RTO code
            rr = str(max(1, min(99, int(r) + rng.choice([-2, -1, 1, 2]))))
            cand = f"{s}{rr}{ser}{num}"
        if cand != p and em.is_valid_plate(cand):
            out.add(cand)
    ents = []
    for c in sorted(out):
        e = em.Entity(c, sources={"rto_registration", "roadcapture"}, n_records=2,
                      color=rng.choice(COLORS), vtype=rng.choice(TYPES), model=rng.choice(MODELS))
        ents.append(e)
    return ents


def features_for(u: em.Universe, q: dict, decoys: list[em.Entity] | None = None) -> list[dict]:
    """Per-candidate features for one query (pool = blocking index over trusted vehicles + neighbours)."""
    qn = em.normalize_plate(q["query"])
    repaired = em.repair_format(qn)
    pool = {p: u.entities[p] for p in (u.index.candidates(qn) | {p for p in repaired if p in u.entities})}
    for d in decoys or []:
        pool.setdefault(d.plate, d)
    rows = []
    for p, e in pool.items():
        if not e.trusted:
            continue
        f = em.string_features(qn, p, repaired)
        f["exact"] = 1.0 if qn == p else 0.0
        f["ctx"], _ = em.context_bonus(query_color=q["color"], query_type=q["type"], query_model=q["model"],
                                       cand_color=e.color, cand_type=e.vtype, cand_model=e.model)
        f["plate"] = p
        rows.append(f)
    return rows


def combined(f: dict, w: tuple, ctx: bool) -> float:
    if f["exact"]:
        s = 1.0
    else:
        s = w[0] * f["ocr_sim"] + w[1] * f["lev_sim"] + w[2] * f["jaro_winkler"] + w[3] * f["format_repair"]
    return s + (f["ctx"] if ctx else 0.0)


METHODS = {
    "exact": lambda f, w: f["exact"],
    "levenshtein": lambda f, w: f["lev_sim"],
    "jaro_winkler": lambda f, w: f["jaro_winkler"],
    "ocr_weighted": lambda f, w: f["ocr_sim"],
    "combined": lambda f, w: combined(f, w, False),
    "combined+context": lambda f, w: combined(f, w, True),
}


def rank_of_truth(cands: list[dict], truth: str, scorer, w) -> int | None:
    scored = [(scorer(f, w), f["plate"]) for f in cands]
    mine = next((s for s, p in scored if p == truth), None)
    if mine is None or mine <= 0:
        return None
    return 1 + sum(1 for s, p in scored if p != truth and s >= mine)  # pessimistic ties


def metrics(feats, queries, scorer, w) -> dict:
    top1 = top3 = 0
    rr = 0.0
    for f, q in zip(feats, queries):
        r = rank_of_truth(f, q["truth"], scorer, w)
        if r:
            top1 += r == 1
            top3 += r <= 3
            rr += 1 / r
    n = max(len(queries), 1)
    return {"top1": round(top1 / n, 4), "top3": round(top3 / n, 4), "mrr": round(rr / n, 4), "n": len(queries)}


def grid_search(feats, queries, scorer=None, tolerance: float = 0.005) -> tuple:
    """
    Try every weighting (step 0.1, sum = 1) on the DEV half. Many weightings tie, so among
    those within `tolerance` of the best top-1 we keep the most BALANCED one (closest to equal
    weights) — it relies on all four signals and is the most robust choice.
    """
    scorer = scorer or METHODS["combined+context"]
    steps = [i / 10 for i in range(11)]
    scored = []
    for a, b, c in itertools.product(steps, steps, steps):
        d = round(1 - a - b - c, 2)
        if d < 0:
            continue
        w = (a, b, c, d)
        m = metrics(feats, queries, scorer, w)
        scored.append((m["top1"], m["mrr"], w))
    best = max(t for t, _, _ in scored)
    near = [(t, r, w) for t, r, w in scored if t >= best - tolerance]
    near.sort(key=lambda x: (sum((wi - 0.25) ** 2 for wi in x[2]), -x[0], -x[1]))
    return near[0][2]


def eval_manifest(u: em.Universe) -> dict:
    """Seeded noise (answer key): can the app's matcher recover each clean plate?"""
    try:
        conn = connect("vehicle_dwh")
        cur = conn.cursor()
        cur.execute("SELECT noisy_plate, clean_plate, context_color, context_type, context_model, noise_kind "
                    "FROM dwh_noisy_seed_manifest WHERE noise_kind NOT LIKE 'context%%' AND noise_kind <> 'support_sighting'")
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception:
        return {}
    ok = 0
    by_kind = defaultdict(lambda: [0, 0])
    for noisy, clean, col, typ, model, kind in rows:
        hits = em.find_approximate_matches(noisy, top_k=3, min_score=0.0, query_color=col,
                                           query_type=typ, query_model=model)
        good = bool(hits) and hits[0].plate_canonical == clean
        ok += good
        by_kind[kind][0] += good
        by_kind[kind][1] += 1
    return {"n": len(rows), "top1": round(ok / max(len(rows), 1), 4),
            "by_kind": {k: f"{a}/{b}" for k, (a, b) in by_kind.items()}}


def run(n: int = 600, seed: int = 7, save: bool = True, verbose: bool = True, dense: bool = True) -> dict:
    rng = random.Random(seed)
    u = em.get_universe(max_age=0)
    queries = build_queries(u, n, rng)
    feats = [features_for(u, q, lookalike_neighbours(q["truth"], rng) if dense else None) for q in queries]
    sparse = metrics([features_for(u, q) for q in queries], queries, METHODS["levenshtein"], None)
    half = len(queries) // 2
    dev_q, test_q, dev_f, test_f = queries[:half], queries[half:], feats[:half], feats[half:]

    w = grid_search(dev_f, dev_q)
    table = {name: metrics(test_f, test_q, fn, w) for name, fn in METHODS.items()}
    blocking_recall = sum(any(f["plate"] == q["truth"] for f in fs) for fs, q in zip(test_f, test_q)) / max(len(test_q), 1)

    per_kind = {}
    for kind in NOISE_KINDS:
        idx = [i for i, q in enumerate(test_q) if q["kind"] == kind]
        per_kind[kind] = {name: metrics([test_f[i] for i in idx], [test_q[i] for i in idx], fn, w)["top1"]
                          for name, fn in METHODS.items()}

    result = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "n_dev": len(dev_q), "n_test": len(test_q),
        "weights": {"ocr": w[0], "lev": w[1], "jw": w[2], "format": w[3]},
        "weights_in_app": {"ocr": em.W_OCR, "lev": em.W_LEV, "jw": em.W_JW, "format": em.W_FMT},
        "blocking_recall": round(blocking_recall, 4),
        "dense_registry": dense,
        "sparse_db_levenshtein_top1": sparse["top1"],
        "overall": table,
        "per_noise_kind_top1": per_kind,
        "seeded_noise": eval_manifest(u),
        "examples": [{k: q[k] for k in ("query", "truth", "kind", "color")} for q in test_q[:8]],
    }
    if save:
        RESULT_JSON.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        try:
            conn = connect("vehicle_dwh")
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS dwh_match_eval (
                eval_id INT PRIMARY KEY AUTO_INCREMENT, run_at DATETIME NOT NULL, method VARCHAR(40) NOT NULL,
                n_test INT NOT NULL, top1 DOUBLE NOT NULL, top3 DOUBLE NOT NULL, mrr DOUBLE NOT NULL,
                weights VARCHAR(120) NULL)""")
            for name, m in table.items():
                cur.execute("INSERT INTO dwh_match_eval (run_at, method, n_test, top1, top3, mrr, weights) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (datetime.now(), name, m["n"], m["top1"], m["top3"], m["mrr"], json.dumps(result["weights"])))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"(could not store dwh_match_eval: {e})")
    if verbose:
        print_report(result)
    return result


def print_report(r: dict) -> None:
    print(f"\nAlgorithm comparison — {r['n_test']} held-out noisy plates (weights tuned on {r['n_dev']} others)")
    if r.get("dense_registry"):
        print("each query competes with ~24 realistic look-alike vehicles (same series / nearby numbers);")
        print(f"(on the sparse synthetic DB alone even plain Levenshtein gets {r['sparse_db_levenshtein_top1']:.0%})")
    print(f"{'method':<18}{'top-1':>8}{'top-3':>8}{'MRR':>8}")
    for name, m in r["overall"].items():
        print(f"{name:<18}{m['top1']:>8.1%}{m['top3']:>8.1%}{m['mrr']:>8.3f}")
    print(f"\nblocking recall (true plate reached the candidate pool): {r['blocking_recall']:.1%}")
    print(f"grid-searched weights: {r['weights']}   (app uses {r['weights_in_app']})")
    print("\ntop-1 by noise type:")
    kinds = list(r["per_noise_kind_top1"])
    names = list(r["overall"])
    print(f"{'noise':<13}" + "".join(f"{n[:12]:>13}" for n in names))
    for k in kinds:
        print(f"{k:<13}" + "".join(f"{r['per_noise_kind_top1'][k][n]:>13.0%}" for n in names))
    s = r.get("seeded_noise") or {}
    if s:
        print(f"\nseeded noise in the DB (answer key): top-1 {s['top1']:.1%} of {s['n']}  {s['by_kind']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    run(a.n, a.seed)
