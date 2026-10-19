"""
entity_matching.py — Phase-2 noisy plate matching + contextual cleanse.

Algorithms (combined score, not a single metric):
  1) normalize_plate          — strip separators / case (schema+value mapping)
  2) OCR confusion distance   — 0/O, 1/I/L, 5/S, 8/B, 2/Z (scan noise)
  3) Levenshtein (normalized) — insert/delete/substitute remaining errors
  4) Jaro-Winkler             — rewards shared prefixes (state code / RTO block)
  5) Contextual re-rank       — make_model, vehicle_color, vehicle_type

Why this combo (viva):
  - Pure Levenshtein treats O↔0 the same as A↔Z; OCR matrix models scanner mistakes.
  - Jaro-Winkler alone under-penalizes middle digit flips common on plates.
  - Context (colour/model) breaks ties when several plates are edit-distance close.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from db_config import connect

# Common Indian-plate OCR confusions (bidirectional)
OCR_GROUPS = [
    set("0OQD"),
    set("1IL|"),
    set("5S"),
    set("8B"),
    set("2Z"),
    set("6G"),
]


def normalize_plate(raw: str | None) -> str:
    if raw is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def _ocr_equivalent(a: str, b: str) -> bool:
    if a == b:
        return True
    for g in OCR_GROUPS:
        if a in g and b in g:
            return True
    return False


def ocr_aware_distance(a: str, b: str) -> int:
    """
    Levenshtein where OCR-confused substitutions cost 0 (same 'glyph family').
    Insert / delete / hard substitute still cost 1.
    """
    a, b = normalize_plate(a), normalize_plate(b)
    if a == b:
        return 0
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            sub = 0 if _ocr_equivalent(ca, cb) else 1
            cur.append(
                min(
                    prev[j] + 1,  # delete
                    cur[j - 1] + 1,  # insert
                    prev[j - 1] + sub,  # substitute / OCR-eq
                )
            )
        prev = cur
    return prev[m]


def levenshtein(a: str, b: str) -> int:
    a, b = normalize_plate(a), normalize_plate(b)
    n, m = len(a), len(b)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(
                min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1))
            )
        prev = cur
    return prev[m]


def jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    s1, s2 = normalize_plate(s1), normalize_plate(s2)
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    match_dist = max(len(s1), len(s2)) // 2 - 1
    match_dist = max(match_dist, 0)
    s1_match = [False] * len(s1)
    s2_match = [False] * len(s2)
    matches = 0
    transpositions = 0
    for i, c in enumerate(s1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len(s2))
        for j in range(start, end):
            if s2_match[j] or s2[j] != c:
                continue
            s1_match[i] = s2_match[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    for i, matched in enumerate(s1_match):
        if not matched:
            continue
        while not s2_match[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    jaro = (
        matches / len(s1)
        + matches / len(s2)
        + (matches - transpositions / 2) / matches
    ) / 3
    prefix = 0
    for i in range(min(4, len(s1), len(s2))):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * p * (1 - jaro)


def plate_similarity(query: str, candidate: str) -> dict[str, float | int]:
    """Combined similarity in [0,1]. Higher = closer."""
    q, c = normalize_plate(query), normalize_plate(candidate)
    if not q or not c:
        return {
            "score": 0.0,
            "ocr_dist": 99,
            "lev": 99,
            "jaro_winkler": 0.0,
            "exact": 0,
        }
    if q == c:
        return {
            "score": 1.0,
            "ocr_dist": 0,
            "lev": 0,
            "jaro_winkler": 1.0,
            "exact": 1,
        }
    ocr_d = ocr_aware_distance(q, c)
    lev = levenshtein(q, c)
    jw = jaro_winkler(q, c)
    maxlen = max(len(q), len(c), 1)
    ocr_sim = 1.0 - (ocr_d / maxlen)
    lev_sim = 1.0 - (lev / maxlen)
    # Weights: OCR-aware edit dominates scan noise; JW helps prefix structure.
    score = 0.45 * ocr_sim + 0.25 * lev_sim + 0.30 * jw
    return {
        "score": round(score, 4),
        "ocr_dist": ocr_d,
        "lev": lev,
        "jaro_winkler": round(jw, 4),
        "exact": 0,
    }


def context_bonus(
    *,
    query_color: str | None,
    query_model: str | None,
    query_type: str | None,
    cand_color: str | None,
    cand_model: str | None,
    cand_type: str | None,
) -> tuple[float, list[str]]:
    bonus = 0.0
    reasons: list[str] = []
    if query_color and cand_color and str(query_color).strip().lower() == str(cand_color).strip().lower():
        bonus += 0.08
        reasons.append("color match")
    if query_type and cand_type and str(query_type).strip().lower() == str(cand_type).strip().lower():
        bonus += 0.05
        reasons.append("type match")
    if query_model and cand_model:
        qm = str(query_model).strip().lower()
        cm = str(cand_model).strip().lower()
        if qm == cm or qm in cm or cm in qm:
            bonus += 0.10
            reasons.append("model match")
    return bonus, reasons


@dataclass
class MatchHit:
    plate_canonical: str
    native_plate: str
    source: str
    score: float
    ocr_dist: int
    lev: int
    jaro_winkler: float
    context_bonus: float
    context_reasons: list[str]
    owner_name: str | None = None
    make_model: str | None = None
    vehicle_color: str | None = None
    vehicle_type: str | None = None
    status: str | None = None


def _fetch_candidate_universe() -> list[dict[str, Any]]:
    """Candidates from warehouse + latest capture context (color/type)."""
    conn = connect("vehicle_dwh")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT plate_canonical, owner_name, make_model, status,
               last_seen_location, last_seen_time
        FROM vehicle_360
        """
    )
    rows = [
        {
            "plate_canonical": r[0],
            "owner_name": r[1],
            "make_model": r[2],
            "status": r[3],
            "last_seen_location": r[4],
            "source": "vehicle_360",
            "native_plate": r[0],
            "vehicle_color": None,
            "vehicle_type": None,
        }
        for r in cur.fetchall()
    ]
    cur.close()
    conn.close()

    # Enrich with latest capture colour/type when roadcapture is up
    try:
        c2 = connect("roadcapture")
        cur2 = c2.cursor()
        cur2.execute(
            """
            SELECT plate_no, vehicle_color, vehicle_type
            FROM capture_log
            ORDER BY capture_time DESC
            """
        )
        seen: set[str] = set()
        color_map: dict[str, tuple[str | None, str | None]] = {}
        for plate_no, color, vtype in cur2.fetchall():
            key = normalize_plate(plate_no)
            if key in seen:
                continue
            seen.add(key)
            color_map[key] = (color, vtype)
        cur2.close()
        c2.close()
        for r in rows:
            color, vtype = color_map.get(r["plate_canonical"], (None, None))
            r["vehicle_color"] = color
            r["vehicle_type"] = vtype
    except Exception:
        pass

    # Also include xref native spellings as alternate surfaces
    try:
        conn = connect("vehicle_dwh")
        cur = conn.cursor()
        cur.execute(
            "SELECT plate_canonical, source_name, native_plate FROM dwh_plate_xref"
        )
        by_plate = {r["plate_canonical"]: r for r in rows}
        extras = []
        for plate, source, native in cur.fetchall():
            base = by_plate.get(plate)
            if not base:
                continue
            if normalize_plate(native) == plate:
                continue
            extras.append(
                {
                    **base,
                    "source": f"xref:{source}",
                    "native_plate": native,
                }
            )
        cur.close()
        conn.close()
        rows.extend(extras)
    except Exception:
        pass
    return rows


def find_approximate_matches(
    noisy_plate: str,
    *,
    top_k: int = 15,
    min_score: float = 0.72,
    query_color: str | None = None,
    query_model: str | None = None,
    query_type: str | None = None,
    include_exact: bool = True,
) -> list[MatchHit]:
    """Noisy/scanned plate → closest warehouse vehicles (+ vice-versa uses same fn)."""
    q = normalize_plate(noisy_plate)
    if not q:
        return []
    hits: list[MatchHit] = []
    for cand in _fetch_candidate_universe():
        cand_key = normalize_plate(cand["native_plate"])
        if not include_exact and cand_key == q:
            continue
        sims = plate_similarity(q, cand["native_plate"])
        bonus, reasons = context_bonus(
            query_color=query_color,
            query_model=query_model,
            query_type=query_type,
            cand_color=cand.get("vehicle_color"),
            cand_model=cand.get("make_model"),
            cand_type=cand.get("vehicle_type"),
        )
        final = min(1.0, float(sims["score"]) + bonus)
        if final < min_score:
            continue
        hits.append(
            MatchHit(
                plate_canonical=cand["plate_canonical"],
                native_plate=str(cand["native_plate"]),
                source=str(cand["source"]),
                score=round(final, 4),
                ocr_dist=int(sims["ocr_dist"]),
                lev=int(sims["lev"]),
                jaro_winkler=float(sims["jaro_winkler"]),
                context_bonus=round(bonus, 4),
                context_reasons=reasons,
                owner_name=cand.get("owner_name"),
                make_model=cand.get("make_model"),
                vehicle_color=cand.get("vehicle_color"),
                vehicle_type=cand.get("vehicle_type"),
                status=cand.get("status"),
            )
        )
    # Deduplicate by canonical keeping best score
    best: dict[str, MatchHit] = {}
    for h in hits:
        prev = best.get(h.plate_canonical)
        if prev is None or h.score > prev.score:
            best[h.plate_canonical] = h

    def sort_key(h: MatchHit):
        # Prefer non-exact near-misses with context (cleanse target), then score
        is_exact = 1 if normalize_plate(h.plate_canonical) == q else 0
        return (is_exact, -h.context_bonus, -h.score, h.ocr_dist, h.lev)

    ranked = sorted(best.values(), key=sort_key)
    return ranked[:top_k]


def find_noisy_near_clean(
    clean_plate: str,
    *,
    top_k: int = 15,
    min_score: float = 0.72,
) -> list[MatchHit]:
    """Vice-versa: clean plate → nearby noisy / alternate spellings in sources."""
    return find_approximate_matches(clean_plate, top_k=top_k, min_score=min_score)


def ensure_cleanse_log(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dwh_cleanse_log (
            cleanse_id INT PRIMARY KEY AUTO_INCREMENT,
            cleansed_at DATETIME NOT NULL,
            noisy_plate VARCHAR(40) NOT NULL,
            chosen_plate VARCHAR(20) NOT NULL,
            match_score DOUBLE NOT NULL,
            context_json VARCHAR(500) NULL,
            action VARCHAR(40) NOT NULL,
            note VARCHAR(400) NULL
        )
        """
    )


def cleanse_noisy_record(
    *,
    noisy_plate: str,
    chosen_plate: str,
    match_score: float,
    query_color: str | None = None,
    query_model: str | None = None,
    query_type: str | None = None,
    note: str = "",
    fix_capture: bool = True,
) -> dict[str, Any]:
    """
    Cleanse: rewrite noisy capture plate → chosen canonical plate when context agrees,
    and log the decision. Removes the anomaly from recorded capture data.
    """
    noisy_n = normalize_plate(noisy_plate)
    chosen_n = normalize_plate(chosen_plate)
    if not noisy_n or not chosen_n:
        raise ValueError("noisy_plate and chosen_plate required")

    ctx = {
        "color": query_color,
        "model": query_model,
        "type": query_type,
        "score": match_score,
    }
    actions: list[str] = []

    # 1) Fix LIVE roadcapture rows whose normalized plate == noisy
    capture_fixed = 0
    if fix_capture:
        try:
            conn = connect("roadcapture")
            cur = conn.cursor()
            cur.execute("SELECT capture_id, plate_no FROM capture_log")
            for cid, plate_no in cur.fetchall():
                if normalize_plate(plate_no) != noisy_n:
                    continue
                # Keep a compact uppercase clean spelling in source
                cur.execute(
                    "UPDATE capture_log SET plate_no=%s WHERE capture_id=%s",
                    (chosen_n, cid),
                )
                capture_fixed += 1
            conn.commit()
            cur.close()
            conn.close()
            if capture_fixed:
                actions.append(f"updated {capture_fixed} capture_log row(s)")
        except Exception as e:
            actions.append(f"capture fix skipped: {e}")

    # 2) Merge / retarget vehicle_360 if a noisy canonical row exists
    wh = connect("vehicle_dwh")
    wcur = wh.cursor()
    ensure_cleanse_log(wcur)
    wcur.execute(
        "SELECT plate_canonical FROM vehicle_360 WHERE plate_canonical=%s", (noisy_n,)
    )
    noisy_exists = wcur.fetchone() is not None
    wcur.execute(
        "SELECT plate_canonical FROM vehicle_360 WHERE plate_canonical=%s", (chosen_n,)
    )
    chosen_exists = wcur.fetchone() is not None

    if noisy_exists and noisy_n != chosen_n:
        if chosen_exists:
            wcur.execute("DELETE FROM vehicle_360 WHERE plate_canonical=%s", (noisy_n,))
            actions.append("removed anomalous vehicle_360 row for noisy plate")
        else:
            wcur.execute(
                "UPDATE vehicle_360 SET plate_canonical=%s WHERE plate_canonical=%s",
                (chosen_n, noisy_n),
            )
            actions.append("renamed vehicle_360 plate_canonical noisy→clean")

    # Refresh colour context onto chosen row note only (ETL still owns full dims)
    import json

    wcur.execute(
        """
        INSERT INTO dwh_cleanse_log
          (cleansed_at, noisy_plate, chosen_plate, match_score, context_json, action, note)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            datetime.now(),
            noisy_plate[:40],
            chosen_n[:20],
            float(match_score),
            json.dumps(ctx)[:500],
            "; ".join(actions)[:40] or "logged",
            (note or "phase2 cleanse")[:400],
        ),
    )
    wh.commit()
    wcur.close()
    wh.close()
    return {
        "noisy_normalized": noisy_n,
        "chosen_plate": chosen_n,
        "capture_fixed": capture_fixed,
        "actions": actions,
    }
