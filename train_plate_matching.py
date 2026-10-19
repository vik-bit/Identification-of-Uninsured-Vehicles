"""
train_plate_matching.py — ML weights + OCR dictionary for plate matching.

Mirrors the electoral-roll road_type_lookup pattern and the matching_IIA logistic combo:

  1) Compact labelled plate pairs (noisy query vs candidate, 0/1)
  2) LogisticRegression on [ocr_sim, lev_sim, jaro_winkler, format_repair]
  3) Positive coefs → sum-to-1 weights written to match_weights.json
  4) Seed OCR look-alikes + discover char variants from true pairs (≥2 times)
     → plate_ocr_lookup.json  (variant → canonical, like Rd → Road)

No warehouse / Docker required. Re-run anytime:

    python train_plate_matching.py
    python train_plate_matching.py --n-plates 36 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import f1_score, precision_score, recall_score

import entity_matching as em

HERE = Path(__file__).resolve().parent
WEIGHTS_PATH = HERE / "match_weights.json"
LOOKUP_PATH = HERE / "plate_ocr_lookup.json"
TRAIN_CSV = HERE / "plate_match_train.csv"

# Seed look-up: surface glyph → preferred digit form (digit-slot repair), lecture-style T map.
SEED_LOOKUP: dict[str, str] = {
    "O": "0", "Q": "0", "D": "0",
    "I": "1", "L": "1",
    "S": "5",
    "B": "8",
    "Z": "2",
    "G": "6",
}
# Glyph families used by OCR-weighted distance (same spirit as road-type groups).
SEED_GROUPS = [
    ["0", "O", "Q", "D"],
    ["1", "I", "L"],
    ["5", "S"],
    ["8", "B"],
    ["2", "Z"],
    ["6", "G"],
]

STATES = [
    "DL", "MH", "KA", "TN", "UP", "GJ", "RJ", "WB", "TS", "AP",
    "HR", "PB", "KL", "MP", "CG", "OD", "BR", "JH", "UK", "HP",
]
SERIES_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
OCR_SUBS = {
    "0": "O", "O": "0", "1": "I", "I": "1", "5": "S", "S": "5",
    "8": "B", "B": "8", "2": "Z", "Z": "2", "6": "G", "G": "6", "D": "0",
}
FEATURE_NAMES = ["ocr_sim", "lev_sim", "jaro_winkler", "format_repair"]
NOISE_KINDS = ["ocr_1", "ocr_2", "swapped", "dropped", "unreadable", "format"]


def _rand_plate(rng: random.Random) -> str:
    for _ in range(80):
        st = rng.choice(STATES)
        rto = str(rng.randint(1, 99))
        ser_len = rng.choice([1, 2, 3])
        ser = "".join(rng.choice(SERIES_LETTERS) for _ in range(ser_len))
        num = f"{rng.randint(0, 9999):04d}"
        p = f"{st}{rto}{ser}{num}"
        if em.is_valid_plate(p):
            return em.normalize_plate(p)
    return "DL9CAF1234"


def _ocr_flip(s: str, k: int, rng: random.Random) -> str:
    idx = [i for i, c in enumerate(s) if c in OCR_SUBS]
    if not idx:
        return s
    chars = list(s)
    for i in rng.sample(idx, min(k, len(idx))):
        chars[i] = OCR_SUBS[chars[i]]
    return "".join(chars)


def corrupt(p: str, kind: str, rng: random.Random) -> str:
    if kind == "ocr_1":
        return _ocr_flip(p, 1, rng)
    if kind == "ocr_2":
        return _ocr_flip(p, 2, rng)
    if kind == "swapped":
        opts = [i for i in range(2, len(p) - 1) if p[i] != p[i + 1]] or [2]
        i = rng.choice(opts)
        return p[:i] + p[i + 1] + p[i] + p[i + 2:]
    if kind == "dropped":
        i = rng.randrange(2, len(p))
        return p[:i] + p[i + 1:]
    if kind == "unreadable":
        i = rng.randrange(2, len(p))
        return p[:i] + "?" + p[i + 1:]
    # format heterogeneity (not noise, but a real source spelling)
    return rng.choice([
        em.fmt_for_source(p, "insurance"),
        p.lower(),
        em.fmt_for_source(p, "theft_records"),
    ])


def neighbour(p: str, rng: random.Random) -> str:
    """Hard negative: same series, one digit off (realistic RTO neighbour)."""
    m = em.PLATE_RE.match(p)
    if not m:
        return _rand_plate(rng)
    s, r, ser, num = m.groups()
    digits = list(num)
    i = rng.randrange(4)
    digits[i] = rng.choice([d for d in "0123456789" if d != digits[i]])
    cand = f"{s}{r}{ser}{''.join(digits)}"
    return cand if em.is_valid_plate(cand) and cand != p else _rand_plate(rng)


def build_compact_dataset(n_plates: int, seed: int) -> list[dict]:
    """
    Compact but meaningful: each base plate × noise kinds → 1 positive + 2 hard negatives.
    ~ n_plates * 6 * 3 rows (e.g. 32×6×3 = 576).
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    seen_truth: set[str] = set()
    while len(seen_truth) < n_plates:
        seen_truth.add(_rand_plate(rng))
    for truth in sorted(seen_truth):
        for kind in NOISE_KINDS:
            q = corrupt(truth, kind, rng)
            if em.normalize_plate(q) == truth and kind != "format":
                q = corrupt(truth, "ocr_1", rng)
            rows.append({"query": q, "candidate": truth, "label": 1, "kind": kind})
            for _ in range(2):
                neg = neighbour(truth, rng)
                rows.append({"query": q, "candidate": neg, "label": 0, "kind": f"{kind}_neg"})
    return rows


def feature_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for r in rows:
        f = em.string_features(r["query"], r["candidate"])
        X.append([f[n] for n in FEATURE_NAMES])
        y.append(r["label"])
    return np.asarray(X, dtype=float), np.asarray(y, dtype=int)


def train_logistic(X: np.ndarray, y: np.ndarray, seed: int) -> tuple[LogisticRegression, dict]:
    """Same idea as matching_IIA.ipynb cell 7: LR on stacked similarity scores."""
    clf = LogisticRegression(max_iter=4000, class_weight="balanced", random_state=seed)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)
    prob = cross_val_predict(clf, X, y, cv=cv, method="predict_proba")[:, 1]
    # threshold that maximises F1 on CV probabilities
    best_f1, best_t = 0.0, 0.5
    for t in np.arange(0.20, 0.90, 0.01):
        pred = (prob >= t).astype(int)
        f = f1_score(y, pred, zero_division=0)
        if f > best_f1:
            best_f1, best_t = f, float(t)
    pred = (prob >= best_t).astype(int)
    metrics = {
        "cv_f1": round(float(best_f1), 4),
        "cv_precision": round(float(precision_score(y, pred, zero_division=0)), 4),
        "cv_recall": round(float(recall_score(y, pred, zero_division=0)), 4),
        "cv_threshold": round(best_t, 3),
        "n_rows": int(len(y)),
        "n_pos": int(y.sum()),
        "n_neg": int(len(y) - y.sum()),
    }
    clf.fit(X, y)
    return clf, metrics


def coefs_to_weights(clf: LogisticRegression) -> dict[str, float]:
    raw = np.asarray(clf.coef_[0], dtype=float)
    # Similarity features should push toward match; clip negatives then L1-normalise.
    pos = np.maximum(raw, 0.0)
    if pos.sum() <= 1e-12:
        pos = np.ones_like(raw)
    pos = pos / pos.sum()
    return {name: round(float(w), 4) for name, w in zip(FEATURE_NAMES, pos)}


def _known_ocr_pair(a: str, b: str) -> bool:
    """Only true look-alikes (seed families or the OCR_SUBS map) — not random digit flips."""
    if a == b:
        return False
    if OCR_SUBS.get(a) == b or OCR_SUBS.get(b) == a:
        return True
    return any(a in g and b in g for g in SEED_GROUPS)


def discover_char_variants(rows: list[dict], min_count: int = 2) -> dict[str, str]:
    """
    Like discover_near_misses in road_type_lookup: from labelled *OCR* matches, reinforce
    glyph swaps seen ≥ min_count times. Only known look-alike pairs are accepted so
    dropped/swapped noise cannot invent N→8 style junk.
    """
    counts: Counter[tuple[str, str]] = Counter()
    for r in rows:
        if r["label"] != 1:
            continue
        # Prefer OCR-style rows; format/case rows rarely teach glyph families.
        if not str(r.get("kind", "")).startswith("ocr"):
            continue
        a, b = em.normalize_plate(r["query"]), em.normalize_plate(r["candidate"])
        if len(a) != len(b) or a == b:
            continue
        for ca, cb in zip(a, b):
            if ca == "?" or cb == "?" or not _known_ocr_pair(ca, cb):
                continue
            # Prefer digit as canonical when one side is a digit.
            if ca.isalpha() and cb.isdigit():
                counts[(ca, cb)] += 1
            elif ca.isdigit() and cb.isalpha():
                counts[(cb, ca)] += 1
    found: dict[str, str] = {}
    for (variant, canonical), n in counts.most_common():
        if n < min_count:
            continue
        if variant in SEED_LOOKUP and SEED_LOOKUP[variant] == canonical:
            continue  # already seeded; still counted in pair_counts
        # New surface form only if it shares a family with the canonical digit
        if any(canonical in g and variant in g for g in SEED_GROUPS):
            found[variant] = canonical
    return found


def build_lookup_payload(discovered: dict[str, str], pair_counts: dict[str, int]) -> dict:
    lookup = {k.lower(): v for k, v in SEED_LOOKUP.items()}
    notes = {k.lower(): "seed: OCR digit look-alike" for k in SEED_LOOKUP}
    groups = [list(g) for g in SEED_GROUPS]
    for variant, canonical in discovered.items():
        lookup.setdefault(variant.lower(), canonical)
        notes.setdefault(variant.lower(), "added from training pairs (seen ≥2, aligned)")
        # extend the group that contains the canonical digit
        placed = False
        for g in groups:
            if canonical in g:
                if variant.upper() not in g:
                    g.append(variant.upper())
                placed = True
                break
        if not placed:
            groups.append([canonical, variant.upper()])
    return {
        "source": "compact synthetic plate OCR pairs (train_plate_matching.py)",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "lookup": lookup,
        "groups": groups,
        "notes": notes,
        "pair_counts": pair_counts,
        "discovered": discovered,
    }


def pair_count_table(rows: list[dict]) -> dict[str, int]:
    """Evidence counts for known OCR look-alikes only (keeps the JSON meaningful)."""
    c: Counter[str] = Counter()
    for r in rows:
        if r["label"] != 1 or not str(r.get("kind", "")).startswith("ocr"):
            continue
        a, b = em.normalize_plate(r["query"]), em.normalize_plate(r["candidate"])
        if len(a) != len(b):
            continue
        for ca, cb in zip(a, b):
            if ca != cb and ca != "?" and cb != "?" and _known_ocr_pair(ca, cb):
                c[f"{ca}->{cb}"] += 1
    return dict(c.most_common(40))


def write_train_csv(rows: list[dict]) -> None:
    import csv
    with TRAIN_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["query", "candidate", "label", "kind"])
        w.writeheader()
        w.writerows(rows)


def run(n_plates: int = 32, seed: int = 7) -> dict:
    rows = build_compact_dataset(n_plates, seed)
    write_train_csv(rows)
    X, y = feature_matrix(rows)
    clf, metrics = train_logistic(X, y, seed)
    weights = coefs_to_weights(clf)
    # map feature names → app keys
    app_weights = {
        "ocr": weights["ocr_sim"],
        "lev": weights["lev_sim"],
        "jw": weights["jaro_winkler"],
        "format": weights["format_repair"],
    }
    raw_coefs = {n: round(float(c), 6) for n, c in zip(FEATURE_NAMES, clf.coef_[0])}
    discovered = discover_char_variants(rows, min_count=2)
    counts = pair_count_table(rows)
    lookup_payload = build_lookup_payload(discovered, counts)

    weights_payload = {
        "source": "LogisticRegression on compact plate pairs (train_plate_matching.py)",
        "method": "sklearn.linear_model.LogisticRegression",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "features": FEATURE_NAMES,
        "raw_coefficients": raw_coefs,
        "intercept": round(float(clf.intercept_[0]), 6),
        "weights": app_weights,
        "cv_metrics": metrics,
        "n_plates": n_plates,
        "seed": seed,
        "note": "Positive LR coefs L1-normalised to sum=1 for the app's weighted string score.",
    }
    WEIGHTS_PATH.write_text(json.dumps(weights_payload, indent=2), encoding="utf-8")
    LOOKUP_PATH.write_text(json.dumps(lookup_payload, indent=2), encoding="utf-8")

    print(f"training rows: {metrics['n_rows']}  (pos {metrics['n_pos']} / neg {metrics['n_neg']})")
    print(f"5-fold CV  F1={metrics['cv_f1']:.3f}  P={metrics['cv_precision']:.3f}  "
          f"R={metrics['cv_recall']:.3f}  thr={metrics['cv_threshold']}")
    print(f"raw LR coefs: {raw_coefs}")
    print(f"app weights (sum=1): {app_weights}")
    print(f"discovered glyph variants: {discovered or '(none beyond seed)'}")
    print(f"wrote {WEIGHTS_PATH.name}")
    print(f"wrote {LOOKUP_PATH.name}")
    print(f"wrote {TRAIN_CSV.name}")
    return {"weights": weights_payload, "lookup": lookup_payload, "metrics": metrics}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Train plate-match weights + OCR dictionary")
    ap.add_argument("--n-plates", type=int, default=32, help="distinct clean plates in the compact set")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    run(args.n_plates, args.seed)
