"""
entity_matching.py — Phase-2: approximate plate matching + contextual cleansing.

    RUBRIC 1  find_approximate_matches()   scanned/noisy plate -> registered vehicles
              find_records_near_plate()    registered plate   -> noisy records + scanned images
    RUBRIC 2  detect_anomalies()           every noisy / inconsistent record in the sources
              cleanse_noisy_record()       fix one noisy plate everywhere it was recorded
              cleanse_all()                fix every confident anomaly, leave the rest for review

HOW A MATCH IS SCORED (justification in PHASE2_VIVA.md, measured by evaluate_matching.py)
  1. normalize_plate        strip separators / case        "DL-9-CAF-8888" == "dl9caf8888"
  2. OCR-weighted distance  0<->O, 1<->I, 8<->B, 5<->S ...   cost 0.25 instead of 1
                            (+ adjacent transposition, Damerau)
                            glyph families come from plate_ocr_lookup.json (seed + discovered)
  3. Levenshtein            plain edit distance (insert / drop / wrong char)
  4. Jaro-Winkler           rewards a shared state/RTO prefix
  5. Format repair          Indian plate grammar  SS 99 XXX 9999 : a letter sitting in a
                            digit slot (DL9CAF8B88) is repaired to the digit it looks like
  6. Context                colour / vehicle type (camera) + model (RTO) agree or disagree

  string score = W_OCR*ocr_sim + W_LEV*lev_sim + W_JW*jw + W_FMT*format_repair_hit
  final score  = string score + context bonus (or penalty)

  Weights: LogisticRegression on compact labelled pairs (train_plate_matching.py) →
  match_weights.json. Falls back to equal-ish defaults if that file is missing.

Speed: candidates come from an "OCR-skeleton + symmetric-delete" index (blocking), so a
query only scores the few dozen plates that are within ~2 edits, not all 5000+.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from db_config import connect

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- weights + OCR dictionary
# Defaults used only when match_weights.json / plate_ocr_lookup.json are absent.
_DEFAULT_W = (0.30, 0.30, 0.30, 0.10)
_DEFAULT_OCR_GROUPS = [set("0OQD"), set("1IL"), set("5S"), set("8B"), set("2Z"), set("6G")]
_DEFAULT_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}

WEIGHTS_META: dict[str, Any] = {}
LOOKUP_META: dict[str, Any] = {}


def _load_trained_weights() -> tuple[float, float, float, float]:
    """Positive LR coefs from train_plate_matching.py, already normalised to sum=1."""
    global WEIGHTS_META
    path = HERE / "match_weights.json"
    if not path.exists():
        return _DEFAULT_W
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        w = payload.get("weights") or {}
        vals = (float(w["ocr"]), float(w["lev"]), float(w["jw"]), float(w["format"]))
        if abs(sum(vals) - 1.0) > 0.05 or any(v < 0 for v in vals):
            return _DEFAULT_W
        WEIGHTS_META = {
            "source": payload.get("source"),
            "cv_metrics": payload.get("cv_metrics"),
            "built_at": payload.get("built_at"),
        }
        return vals
    except Exception:
        return _DEFAULT_W


def _load_ocr_dictionary() -> tuple[list[set[str]], dict[str, str], dict[str, list[str]]]:
    """
    Road-type-lookup style dictionary for plates: variant glyph → canonical digit,
    plus glyph families for OCR-weighted distance. Built by train_plate_matching.py.
    """
    global LOOKUP_META
    path = HERE / "plate_ocr_lookup.json"
    groups = [set(g) for g in _DEFAULT_OCR_GROUPS]
    to_digit = dict(_DEFAULT_TO_DIGIT)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            LOOKUP_META = {
                "source": payload.get("source"),
                "discovered": payload.get("discovered") or {},
                "built_at": payload.get("built_at"),
            }
            if payload.get("groups"):
                groups = [set(str(c).upper() for c in g) for g in payload["groups"] if g]
            lookup = payload.get("lookup") or {}
            for variant, canonical in lookup.items():
                v, c = str(variant).upper(), str(canonical).upper()
                if len(v) == 1 and len(c) == 1 and c.isdigit():
                    to_digit[v] = c
        except Exception:
            pass
    # reverse map for format repair (digit slot → letter options)
    to_letter: dict[str, list[str]] = {}
    for letter, digit in to_digit.items():
        to_letter.setdefault(digit, [])
        if letter.isalpha() and letter not in to_letter[digit]:
            to_letter[digit].append(letter)
    return groups, to_digit, to_letter


W_OCR, W_LEV, W_JW, W_FMT = _load_trained_weights()
OCR_GROUPS, TO_DIGIT, TO_LETTER = _load_ocr_dictionary()
_GROUP_OF = {ch: i for i, g in enumerate(OCR_GROUPS) for ch in g}

CTX_MATCH = {"color": 0.06, "type": 0.04, "model": 0.08}
CTX_MISMATCH = {"color": -0.04, "type": -0.03, "model": -0.06}

AUTO_MIN_SCORE = 0.80   # cleanse_all only auto-fixes above this score ...
AUTO_MIN_MARGIN = 0.04  # ... and when the runner-up is at least this far behind

# --------------------------------------------------------------------------- plate grammar
STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CH", "CG", "DD", "DL", "GA", "GJ", "HR", "HP", "JK",
    "JH", "KA", "KL", "LA", "LD", "MP", "MH", "MN", "ML", "MZ", "NL", "OD", "PY", "PB",
    "RJ", "SK", "TN", "TS", "TR", "UP", "UK", "WB",
}
PLATE_RE = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$")


def reload_matching_artifacts() -> dict[str, Any]:
    """Re-read match_weights.json + plate_ocr_lookup.json (after retrain)."""
    global W_OCR, W_LEV, W_JW, W_FMT, OCR_GROUPS, TO_DIGIT, TO_LETTER, _GROUP_OF
    W_OCR, W_LEV, W_JW, W_FMT = _load_trained_weights()
    OCR_GROUPS, TO_DIGIT, TO_LETTER = _load_ocr_dictionary()
    _GROUP_OF = {ch: i for i, g in enumerate(OCR_GROUPS) for ch in g}
    return {
        "weights": {"ocr": W_OCR, "lev": W_LEV, "jw": W_JW, "format": W_FMT},
        "weights_meta": WEIGHTS_META,
        "lookup_meta": LOOKUP_META,
        "ocr_groups": [sorted(g) for g in OCR_GROUPS],
    }


def normalize_plate(raw: Any) -> str:
    """Upper-case, drop separators. '?' is kept: it marks a character the OCR could not read."""
    if raw is None:
        return ""
    return re.sub(r"[^A-Z0-9?]", "", str(raw).upper())


def is_valid_plate(p: str) -> bool:
    """Indian format SS 9[9] X[XX] 9999, real state code, series never uses I or O."""
    m = PLATE_RE.match(normalize_plate(p))
    if not m:
        return False
    state, _rto, series, _num = m.groups()
    return state in STATE_CODES and not (set(series) & {"I", "O"})


def repair_format(p: str) -> set[str]:
    """
    Positional repair: try every legal layout (RTO 1-2 digits, series 1-3 letters) and swap
    look-alike characters into the class that slot needs.  DL9CAF8B88 -> {DL9CAF8888}.
    """
    p = normalize_plate(p)
    out: set[str] = set()
    if p.count("?") > 1:
        return out
    for r, s in itertools.product((1, 2), (1, 2, 3)):
        if 2 + r + s + 4 != len(p):
            continue
        layout = "LL" + "D" * r + "L" * s + "DDDD"
        options: list[list[str]] = []
        ok = True
        for ch, want in zip(p, layout):
            if ch == "?":  # unreadable: any character of the class this slot needs
                options.append(list("0123456789") if want == "D" else list("ABCDEFGHJKLMNPRSTUVWXYZ"))
                continue
            if want == "D":
                opts = [ch] if ch.isdigit() else ([TO_DIGIT[ch]] if ch in TO_DIGIT else [])
            else:
                opts = [ch] if ch.isalpha() else TO_LETTER.get(ch, [])
            if not opts:
                ok = False
                break
            options.append(opts)
        if not ok:
            continue
        for combo in itertools.product(*options):
            cand = "".join(combo)
            if is_valid_plate(cand):
                out.add(cand)
    return out


def ocr_skeleton(p: str) -> str:
    """Collapse every OCR family to one symbol: DL9CAF8B88 and DL9CAF8888 share a key."""
    return "".join(sorted(OCR_GROUPS[_GROUP_OF[c]])[0] if c in _GROUP_OF else c for c in normalize_plate(p))


# --------------------------------------------------------------------------- source formats
def _parts(plate: str):
    m = PLATE_RE.match(normalize_plate(plate))
    return m.groups() if m else None


def fmt_for_source(plate: str, source: str) -> str:
    """Each source keeps its own convention (heterogeneity is not noise)."""
    p = normalize_plate(plate)
    parts = _parts(p)
    if source == "roadcapture" or not parts:
        return p if source != "rto_registration" else p.lower()
    s, r, ser, n = parts
    if source == "insurance":
        return f"{s}-{r}-{ser}-{n}"
    if source == "rto_registration":
        return p.lower()
    if source == "theft_records":
        return f"{s} {r} {ser} {n}"
    return p


# --------------------------------------------------------------------------- similarity
def _ocr_sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if a == "?" or b == "?":  # unreadable character: cheap, like a look-alike
        return 0.25
    ga, gb = _GROUP_OF.get(a), _GROUP_OF.get(b)
    return 0.25 if ga is not None and ga == gb else 1.0


def ocr_weighted_distance(a: str, b: str) -> float:
    """Damerau-Levenshtein where OCR look-alike substitutions cost 0.25."""
    a, b = normalize_plate(a), normalize_plate(b)
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return float(max(n, m))
    d = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = float(i)
    for j in range(m + 1):
        d[0][j] = float(j)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(
                d[i - 1][j] + 1,
                d[i][j - 1] + 1,
                d[i - 1][j - 1] + _ocr_sub_cost(a[i - 1], b[j - 1]),
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[n][m]


# kept for backwards compatibility with the first version
def ocr_aware_distance(a: str, b: str) -> float:
    return ocr_weighted_distance(a, b)


def align(query: str, candidate: str) -> list[tuple[str, str, str]]:
    """
    Character alignment used by the UI: list of (query_char, candidate_char, op) with
    op in match | lookalike | unreadable | different | missing (in scan) | extra (in scan).
    """
    a, b = normalize_plate(query), normalize_plate(candidate)
    n, m = len(a), len(b)
    d = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = float(i)
    for j in range(m + 1):
        d[0][j] = float(j)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + _ocr_sub_cost(a[i - 1], b[j - 1]))
    out = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and abs(d[i][j] - (d[i - 1][j - 1] + _ocr_sub_cost(a[i - 1], b[j - 1]))) < 1e-9:
            ca, cb = a[i - 1], b[j - 1]
            op = ("match" if ca == cb else "unreadable" if "?" in (ca, cb)
                  else "lookalike" if _ocr_sub_cost(ca, cb) < 1 else "different")
            out.append((ca, cb, op))
            i, j = i - 1, j - 1
        elif j > 0 and abs(d[i][j] - (d[i][j - 1] + 1)) < 1e-9:
            out.append(("", b[j - 1], "missing"))
            j -= 1
        else:
            out.append((a[i - 1], "", "extra"))
            i -= 1
    return out[::-1]


def score_breakdown(hit: "MatchHit", query: str) -> dict[str, float]:
    """Weighted contribution of every signal to a hit's final score (for the UI chart)."""
    q, c = normalize_plate(query), hit.plate_canonical
    maxlen = max(len(q), len(c), 1)
    if q == c:
        parts = {"exact match": 1.0}
    else:
        parts = {
            "OCR-weighted": W_OCR * max(0.0, 1 - hit.ocr_dist / maxlen),
            "Levenshtein": W_LEV * max(0.0, 1 - hit.lev / maxlen),
            "Jaro-Winkler": W_JW * hit.jaro_winkler,
            "plate grammar": W_FMT * hit.format_repair,
        }
    parts["context"] = hit.context_bonus
    return {k: round(v, 4) for k, v in parts.items()}


def records_for_plates(plates: set[str]) -> list[dict]:
    """Current raw rows (all sources) whose plate is one of `plates` — for before/after views."""
    want = {normalize_plate(p) for p in plates}
    return [{"source": r.source, "record_ref": r.ref, "plate": r.native, "colour": r.color,
             "type": r.vtype, "model": r.model, "owner": r.owner}
            for r in load_records() if r.plate in want]


def levenshtein(a: str, b: str) -> int:
    a, b = normalize_plate(a), normalize_plate(b)
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return max(n, m)
    prev = list(range(m + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[m]


def jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    s1, s2 = normalize_plate(s1), normalize_plate(s2)
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    match_dist = max(max(len(s1), len(s2)) // 2 - 1, 0)
    s1_match = [False] * len(s1)
    s2_match = [False] * len(s2)
    matches = 0
    for i, c in enumerate(s1):
        for j in range(max(0, i - match_dist), min(i + match_dist + 1, len(s2))):
            if not s2_match[j] and s2[j] == c:
                s1_match[i] = s2_match[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    t, k = 0, 0
    for i, matched in enumerate(s1_match):
        if matched:
            while not s2_match[k]:
                k += 1
            t += s1[i] != s2[k]
            k += 1
    jaro = (matches / len(s1) + matches / len(s2) + (matches - t / 2) / matches) / 3
    prefix = 0
    for i in range(min(4, len(s1), len(s2))):
        if s1[i] != s2[i]:
            break
        prefix += 1
    return jaro + prefix * p * (1 - jaro)


def string_features(query: str, candidate: str, repaired: set[str] | None = None) -> dict:
    q, c = normalize_plate(query), normalize_plate(candidate)
    maxlen = max(len(q), len(c), 1)
    if repaired is None:
        repaired = repair_format(q)
    ocr_d = ocr_weighted_distance(q, c)
    lev = levenshtein(q, c)
    return {
        "ocr_dist": round(ocr_d, 2),
        "lev": lev,
        "ocr_sim": max(0.0, 1 - ocr_d / maxlen),
        "lev_sim": max(0.0, 1 - lev / maxlen),
        "jaro_winkler": jaro_winkler(q, c),
        "format_repair": 1.0 if (c == q and is_valid_plate(c)) or c in repaired else 0.0,
    }


def plate_similarity(query: str, candidate: str) -> dict:
    """Combined string score in [0,1] (no context)."""
    f = string_features(query, candidate)
    if normalize_plate(query) == normalize_plate(candidate):
        score = 1.0
    else:
        score = (W_OCR * f["ocr_sim"] + W_LEV * f["lev_sim"]
                 + W_JW * f["jaro_winkler"] + W_FMT * f["format_repair"])
    f["score"] = round(score, 4)
    f["exact"] = int(score == 1.0)
    return f


def _same(a, b) -> bool | None:
    if not a or not b:
        return None
    a, b = str(a).strip().lower(), str(b).strip().lower()
    return a == b or a in b or b in a


def context_bonus(*, query_color=None, query_model=None, query_type=None,
                  cand_color=None, cand_model=None, cand_type=None) -> tuple[float, list[str]]:
    bonus, reasons = 0.0, []
    for key, q, c in (("color", query_color, cand_color), ("type", query_type, cand_type),
                      ("model", query_model, cand_model)):
        same = _same(q, c)
        if same is True:
            bonus += CTX_MATCH[key]
            reasons.append(f"{key} ✓")
        elif same is False:
            bonus += CTX_MISMATCH[key]
            reasons.append(f"{key} ✗ ({c})")
    return round(bonus, 4), reasons


# --------------------------------------------------------------------------- data access
def _rows(db: str, sql: str, params=()) -> list[tuple]:
    conn = connect(db)
    cur = conn.cursor()
    cur.execute(sql, params)
    out = cur.fetchall()
    cur.close()
    conn.close()
    return out


@dataclass
class Record:
    """One raw row in one source that carries a plate."""
    source: str
    ref: str           # how to find the row again (capture_id / policy_id|native / ...)
    native: str        # plate exactly as stored
    color: str | None = None
    vtype: str | None = None
    model: str | None = None
    owner: str | None = None
    when: Any = None
    where: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def plate(self) -> str:
        return normalize_plate(self.native)


def load_records() -> list[Record]:
    recs: list[Record] = []
    try:
        for cid, plate, t, loc, col, typ in _rows(
            "roadcapture",
            "SELECT capture_id, plate_no, capture_time, location, vehicle_color, vehicle_type FROM capture_log",
        ):
            recs.append(Record("roadcapture", str(cid), str(plate), col, typ, when=t, where=loc))
    except Exception as e:  # source down -> still work with the rest
        print(f"[entity_matching] roadcapture unavailable: {e}")
    for pid, plate, ins, vf, vt in _rows(
        "vehicle_dwh", "SELECT policy_id, vehicle_number, insurer, valid_from, valid_to FROM stg_policies"
    ):
        recs.append(Record("insurance", f"{pid}|{plate}", str(plate),
                           extra={"insurer": ins, "valid_from": vf, "valid_to": vt}))
    for rid, plate, owner, model, rdate in _rows(
        "vehicle_dwh", "SELECT reg_id, reg_no, owner_name, make_model, registration_date FROM stg_registrations"
    ):
        recs.append(Record("rto_registration", f"{rid}|{plate}", str(plate), model=model, owner=owner,
                           extra={"registration_date": rdate}))
    try:
        for cid, plate, rtype, rdate in _rows(
            "theft_records", "SELECT case_id, plate_number, report_type, report_date FROM theft_shredding"
        ):
            recs.append(Record("theft_records", str(cid), str(plate), extra={"report_type": rtype}, when=rdate))
    except Exception as e:
        print(f"[entity_matching] theft_records unavailable: {e}")
    return [r for r in recs if r.plate]


@dataclass
class Entity:
    plate: str
    sources: set = field(default_factory=set)
    n_records: int = 0
    color: str | None = None
    vtype: str | None = None
    model: str | None = None
    owner: str | None = None
    status: str | None = None

    @property
    def registered(self) -> bool:
        return "rto_registration" in self.sources

    @property
    def format_valid(self) -> bool:
        return is_valid_plate(self.plate)

    shadowed: bool = False  # set by Universe: unregistered OCR-twin of a registered plate

    @property
    def trusted(self) -> bool:
        """Reference entity = valid format AND corroborated by >= 2 independent sources
        AND not merely an OCR-twin of a registered vehicle (same typo in two sources)."""
        return self.format_valid and len(self.sources) >= 2 and not self.shadowed


class PlateIndex:
    """Blocking: OCR skeleton + symmetric deletes (<= 2) -> plate keys."""

    def __init__(self, plates):
        self.buckets: dict[str, set[str]] = defaultdict(set)
        for p in plates:
            for k in self._keys(p):
                self.buckets[k].add(p)

    @staticmethod
    def _keys(p: str, max_del: int = 2) -> set[str]:
        sk = ocr_skeleton(p)
        keys = {sk}
        frontier = {sk}
        for _ in range(max_del):
            nxt = set()
            for s in frontier:
                for i in range(len(s)):
                    nxt.add(s[:i] + s[i + 1:])
            keys |= nxt
            frontier = nxt
        return keys

    def candidates(self, query: str) -> set[str]:
        out: set[str] = set()
        for k in self._keys(normalize_plate(query)):
            out |= self.buckets.get(k, set())
        return out


class Universe:
    def __init__(self):
        self.records = load_records()
        self.entities: dict[str, Entity] = {}
        colors: dict[str, Counter] = defaultdict(Counter)
        types: dict[str, Counter] = defaultdict(Counter)
        for r in self.records:
            e = self.entities.setdefault(r.plate, Entity(r.plate))
            e.sources.add(r.source)
            e.n_records += 1
            if r.color:
                colors[r.plate][r.color] += 1
            if r.vtype:
                types[r.plate][r.vtype] += 1
            if r.model and not e.model:
                e.model, e.owner = r.model, r.owner
        for p, c in colors.items():
            self.entities[p].color = c.most_common(1)[0][0]
        for p, c in types.items():
            self.entities[p].vtype = c.most_common(1)[0][0]
        try:
            for plate, status in _rows("vehicle_dwh", "SELECT plate_canonical, status FROM vehicle_360"):
                if plate in self.entities:
                    self.entities[plate].status = status
        except Exception:
            pass
        self.color_votes, self.type_votes = colors, types
        self.index = PlateIndex(self.entities.keys())
        # an UNregistered plate that is only an OCR slip away from a registered one is suspect
        by_skel: dict[str, list[Entity]] = defaultdict(list)
        for e in self.entities.values():
            by_skel[ocr_skeleton(e.plate)].append(e)
        self.by_skeleton = by_skel
        for group in by_skel.values():
            # only a CORROBORATED registered twin makes the other spelling suspect
            if len(group) < 2 or not any(g.registered and g.format_valid and len(g.sources) >= 2 for g in group):
                continue
            for g in group:
                if not g.registered:
                    g.shadowed = True
        self.loaded_at = time.time()


_CACHE: dict[str, Universe] = {}


def get_universe(max_age: float = 30.0) -> Universe:
    u = _CACHE.get("u")
    if u is None or time.time() - u.loaded_at > max_age:
        u = Universe()
        _CACHE["u"] = u
    return u


def invalidate_cache() -> None:
    _CACHE.clear()


# --------------------------------------------------------------------------- RUBRIC 1
@dataclass
class MatchHit:
    plate_canonical: str
    native_plate: str
    source: str
    score: float
    string_score: float
    ocr_dist: float
    lev: int
    jaro_winkler: float
    format_repair: float
    context_bonus: float
    context_reasons: list[str]
    owner_name: str | None = None
    make_model: str | None = None
    vehicle_color: str | None = None
    vehicle_type: str | None = None
    status: str | None = None
    trusted: bool = True


def score_candidate(query: str, ent: Entity, *, color=None, model=None, vtype=None,
                    repaired: set[str] | None = None) -> MatchHit:
    sim = string_features(query, ent.plate, repaired)
    if normalize_plate(query) == ent.plate:
        s_score = 1.0
    else:
        s_score = (W_OCR * sim["ocr_sim"] + W_LEV * sim["lev_sim"]
                   + W_JW * sim["jaro_winkler"] + W_FMT * sim["format_repair"])
    bonus, reasons = context_bonus(query_color=color, query_model=model, query_type=vtype,
                                   cand_color=ent.color, cand_model=ent.model, cand_type=ent.vtype)
    return MatchHit(
        plate_canonical=ent.plate, native_plate=ent.plate, source=",".join(sorted(ent.sources)),
        score=round(max(0.0, min(1.0, s_score + bonus)), 4), string_score=round(s_score, 4),
        ocr_dist=sim["ocr_dist"], lev=sim["lev"], jaro_winkler=round(sim["jaro_winkler"], 4),
        format_repair=sim["format_repair"], context_bonus=bonus, context_reasons=reasons,
        owner_name=ent.owner, make_model=ent.model, vehicle_color=ent.color, vehicle_type=ent.vtype,
        status=ent.status, trusted=ent.trusted,
    )


def find_approximate_matches(
    noisy_plate: str,
    *,
    top_k: int = 15,
    min_score: float = 0.70,
    query_color: str | None = None,
    query_model: str | None = None,
    query_type: str | None = None,
    include_exact: bool = True,
    include_untrusted: bool = False,
    exhaustive: bool = False,
) -> list[MatchHit]:
    """
    Scanned/noisy plate -> ALL registered vehicles that approximately match, best first.
    Untrusted entities (seen in only one source / invalid format = the noisy records
    themselves) are hidden unless include_untrusted=True.
    """
    q = normalize_plate(noisy_plate)
    if not q:
        return []
    u = get_universe()
    pool = set(u.entities) if exhaustive else u.index.candidates(q)
    repaired = repair_format(q)
    pool |= {p for p in repaired if p in u.entities}
    hits = []
    for p in pool:
        ent = u.entities[p]
        # hide the noisy records themselves (invalid format / OCR-twin of a registered plate)
        if not include_untrusted and (not ent.format_valid or ent.shadowed):
            continue
        if not include_exact and p == q:
            continue
        h = score_candidate(q, ent, color=query_color, model=query_model, vtype=query_type, repaired=repaired)
        if not ent.trusted:  # valid plate but seen in one source only -> small penalty
            h.score = round(max(0.0, h.score - 0.03), 4)
        if h.score >= min_score:
            hits.append(h)
    hits.sort(key=lambda h: (-h.score, h.ocr_dist, h.lev, h.plate_canonical))
    return hits[:top_k]


def find_noisy_near_clean(clean_plate: str, *, top_k: int = 30, min_score: float = 0.70):
    """Backwards-compatible alias for the vice-versa direction."""
    return find_records_near_plate(clean_plate, top_k=top_k, min_score=min_score)


def find_records_near_plate(clean_plate: str, *, top_k: int = 50, min_score: float = 0.70,
                            include_images: bool = True) -> list[dict]:
    """
    Vice-versa: a recorded vehicle -> every RAW record / scanned image whose plate is close
    to it (other spellings, OCR slips, dropped characters), with a verdict per row.
    """
    q = normalize_plate(clean_plate)
    if not q:
        return []
    u = get_universe()
    target = u.entities.get(q, Entity(q))
    near = u.index.candidates(q) | {q}
    out = []
    for r in u.records:
        if r.plate not in near:
            continue
        sim = plate_similarity(r.plate, q)
        if sim["score"] < min_score:
            continue
        bonus, reasons = context_bonus(query_color=r.color, query_type=r.vtype, query_model=r.model,
                                       cand_color=target.color, cand_type=target.vtype,
                                       cand_model=target.model)
        other = u.entities.get(r.plate)
        if r.plate == q:
            verdict = "same vehicle" + ("" if r.native == fmt_for_source(q, r.source) else " (format variant)")
        elif other is not None and other.trusted:
            verdict = "look-alike: another registered vehicle"
        else:
            verdict = "noisy spelling → cleanse"
        out.append({
            "source": r.source, "record_ref": r.ref, "native_plate": r.native, "normalized": r.plate,
            "score": round(min(1.0, sim["score"] + (bonus if r.plate != q else 0)), 4),
            "ocr_dist": sim["ocr_dist"], "context": ", ".join(reasons) or "-",
            "color": r.color, "type": r.vtype, "model": r.model, "when": r.when, "where": r.where,
            "verdict": verdict,
        })
    if include_images:
        try:
            from plate_ocr import scan_image_folder
            for img in scan_image_folder():
                ip = normalize_plate(img["plate"])
                if not ip:
                    continue
                sim = plate_similarity(ip, q)
                if sim["score"] < min_score:
                    continue
                bonus, reasons = context_bonus(query_color=img.get("color"), query_type=img.get("type"),
                                               cand_color=target.color, cand_type=target.vtype)
                out.append({
                    "source": "scanned_image", "record_ref": img["file"], "native_plate": img["plate"],
                    "normalized": ip, "score": round(min(1.0, sim["score"] + (bonus if ip != q else 0)), 4),
                    "ocr_dist": sim["ocr_dist"], "context": ", ".join(reasons) or "-",
                    "color": img.get("color"), "type": img.get("type"), "model": None, "when": None,
                    "where": img["file"], "verdict": "same vehicle" if ip == q else "noisy scan of this vehicle",
                })
        except Exception as e:
            print(f"[entity_matching] image scan skipped: {e}")
    order = {"noisy": 0, "same": 1, "look": 2}
    out.sort(key=lambda d: (order.get(d["verdict"].split()[0].rstrip(":").replace("look-alike", "look"), 0), -d["score"]))
    return out[:top_k]


# --------------------------------------------------------------------------- RUBRIC 2
@dataclass
class Anomaly:
    kind: str          # NOISY_PLATE | FORMAT_VARIANT | CONTEXT_CONFLICT | GHOST_WAREHOUSE_ROW | DUPLICATE_RECORD
    source: str
    record_ref: str
    field: str
    current: str
    suggested: str | None
    score: float
    margin: float
    decision: str      # AUTO | REVIEW
    reason: str
    plate: str = ""    # the vehicle this anomaly is about (after fixing)


def _best_reference(u: Universe, rec: Record) -> tuple[MatchHit | None, float, list[MatchHit]]:
    """Best trusted entity for an untrusted record (+ margin over the runner-up)."""
    q = rec.plate
    ent_q = u.entities.get(q)
    repaired = repair_format(q)
    pool = (u.index.candidates(q) | repaired) - {q}
    hits = []
    for p in pool:
        ent = u.entities.get(p)
        if ent is None or not ent.format_valid:
            continue
        # prefer corroborated references; a lone valid plate only counts against an invalid one
        if not ent.trusted and (ent_q is not None and ent_q.format_valid):
            continue
        h = score_candidate(q, ent, color=rec.color, model=rec.model, vtype=rec.vtype, repaired=repaired)
        h.score = round(min(1.0, h.score + (0.02 if ent.trusted else 0.0)), 4)
        hits.append(h)
    hits.sort(key=lambda h: (-h.score, h.ocr_dist))
    if not hits:
        return None, 0.0, []
    margin = hits[0].score - (hits[1].score if len(hits) > 1 else 0.0)
    return hits[0], round(margin, 4), hits


def detect_anomalies(include_format_variants: bool = True) -> list[Anomaly]:
    u = get_universe(max_age=0)
    out: list[Anomaly] = []

    # (a) noisy plates: record not corroborated, but a trusted vehicle is within reach
    for rec in u.records:
        ent = u.entities[rec.plate]
        if ent.trusted:
            if include_format_variants and rec.native != fmt_for_source(rec.plate, rec.source):
                out.append(Anomaly("FORMAT_VARIANT", rec.source, rec.ref, "plate", rec.native,
                                   fmt_for_source(rec.plate, rec.source), 1.0, 1.0, "AUTO",
                                   f"same vehicle, but not in {rec.source}'s own format"))
            continue
        best, margin, _ = _best_reference(u, rec)
        # a VALID plate needs to be within one slip of a trusted vehicle to be called noise;
        # further away it is a different (unknown) vehicle
        if best is not None and ent.format_valid and (best.score < 0.80 or best.ocr_dist > 1):
            best = None
        if best is None and ent.format_valid:
            # two uncorroborated VALID plates that differ only by look-alike characters
            # (e.g. RTO 'ch3bqy1229' vs camera 'CH38QY1229'): flag the weaker one for review
            twins = [t for t in u.by_skeleton.get(ocr_skeleton(rec.plate), [])
                     if t.plate != rec.plate and t.format_valid]
            if twins:
                t = max(twins, key=lambda x: (len(x.sources), x.n_records))
                if (len(t.sources), t.n_records) >= (len(ent.sources), ent.n_records):
                    out.append(Anomaly("NOISY_PLATE", rec.source, rec.ref, "plate", rec.native,
                                       fmt_for_source(t.plate, rec.source), 0.9, 0.0, "REVIEW",
                                       f"look-alike twin {t.plate} seen in {sorted(t.sources)} "
                                       f"({t.n_records} records); neither is corroborated",
                                       t.plate))
                continue
        if best is None or best.score < 0.60:
            if not ent.format_valid:
                out.append(Anomaly("NOISY_PLATE", rec.source, rec.ref, "plate", rec.native, None, 0.0, 0.0,
                                   "REVIEW", "invalid plate format and no close registered vehicle"))
            continue  # valid but single-source plate with no neighbour = genuinely unknown vehicle
        # confident = high score and clear runner-up gap, or a clear gap that context confirms
        auto = ((best.score >= AUTO_MIN_SCORE and margin >= AUTO_MIN_MARGIN)
                or (best.score >= 0.70 and margin >= 0.10 and best.context_bonus > 0))
        why = [f"ocr_dist={best.ocr_dist}", f"string={best.string_score:.3f}"]
        if best.format_repair:
            why.append("format repair agrees")
        if best.context_reasons:
            why.append("context: " + ", ".join(best.context_reasons))
        out.append(Anomaly("NOISY_PLATE", rec.source, rec.ref, "plate", rec.native,
                           fmt_for_source(best.plate_canonical, rec.source), best.score, margin,
                           "AUTO" if auto else "REVIEW", "; ".join(why)))

    # (b) context conflicts: a vehicle's sightings disagree on colour / type
    by_ref = {(r.source, r.ref): r for r in u.records}
    for attr, votes in (("vehicle_color", u.color_votes), ("vehicle_type", u.type_votes)):
        for plate, counter in votes.items():
            if len(counter) < 2 or not u.entities[plate].trusted:
                continue
            (top, n_top), (_, n_2) = counter.most_common(2)
            for rec in u.records:
                if rec.plate != plate or rec.source != "roadcapture":
                    continue
                val = rec.color if attr == "vehicle_color" else rec.vtype
                if val and val != top:
                    clear = n_top > n_2
                    out.append(Anomaly("CONTEXT_CONFLICT", "roadcapture", rec.ref, attr, val, top,
                                       round(n_top / sum(counter.values()), 3), float(n_top - n_2),
                                       "AUTO" if clear else "REVIEW",
                                       f"other sightings of {plate} say {dict(counter)}", plate))
    del by_ref

    # (c) ghost warehouse rows: vehicle_360 plate that no source record supports any more
    try:
        for (plate,) in _rows("vehicle_dwh", "SELECT plate_canonical FROM vehicle_360"):
            if plate not in u.entities:
                out.append(Anomaly("GHOST_WAREHOUSE_ROW", "vehicle_dwh", plate, "row", plate, None, 1.0, 1.0,
                                   "AUTO", "no capture / policy / registration / theft row has this plate"))
    except Exception:
        pass

    # (d) exact duplicates inside insurance / RTO after cleansing
    seen: dict[tuple, str] = {}
    for rec in u.records:
        if rec.source not in ("insurance", "rto_registration"):
            continue
        sig = (rec.source, rec.plate, json.dumps(rec.extra, default=str), rec.owner, rec.model)
        if sig in seen:
            out.append(Anomaly("DUPLICATE_RECORD", rec.source, rec.ref, "row", rec.native, None, 1.0, 1.0,
                               "AUTO", f"identical to {seen[sig]}"))
        else:
            seen[sig] = rec.ref
    return out


def anomaly_summary(anoms: list[Anomaly] | None = None) -> dict[str, int]:
    anoms = detect_anomalies() if anoms is None else anoms
    c = Counter(f"{a.kind}/{a.decision}" for a in anoms)
    return dict(sorted(c.items()))


# ---- write helpers ---------------------------------------------------------
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
    cur.execute(
        "SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'dwh_cleanse_log'"
    )
    cols = {r[0].lower(): r[1] for r in cur.fetchall()}
    for name, ddl in (("source_name", "VARCHAR(40) NULL"), ("record_ref", "VARCHAR(80) NULL"),
                      ("field_name", "VARCHAR(30) NULL"), ("old_value", "VARCHAR(60) NULL"),
                      ("new_value", "VARCHAR(60) NULL")):
        if name not in cols:
            cur.execute(f"ALTER TABLE dwh_cleanse_log ADD COLUMN {name} {ddl}")
    if (cols.get("action") or 0) < 120:
        cur.execute("ALTER TABLE dwh_cleanse_log MODIFY action VARCHAR(120) NOT NULL")


def _apply_change(source: str, ref: str, field_name: str, old: str, new: str | None) -> bool:
    """Write one fix into the source table where the anomaly was recorded."""
    if source == "roadcapture":
        col = {"plate": "plate_no", "vehicle_color": "vehicle_color", "vehicle_type": "vehicle_type"}[field_name]
        conn = connect("roadcapture")
        cur = conn.cursor()
        cur.execute(f"UPDATE capture_log SET {col}=%s WHERE capture_id=%s", (new, int(ref)))
    elif source == "theft_records":
        conn = connect("theft_records")
        cur = conn.cursor()
        cur.execute("UPDATE theft_shredding SET plate_number=%s WHERE case_id=%s", (new, int(ref)))
    elif source == "vehicle_dwh":  # ghost warehouse row
        conn = connect("vehicle_dwh")
        cur = conn.cursor()
        for t in ("vehicle_360", "dwh_flagged_cache", "ministry_priority"):
            cur.execute(f"DELETE FROM {t} WHERE plate_canonical=%s", (ref,))
    elif source in ("insurance", "rto_registration"):
        table, idcol, pcol = (("stg_policies", "policy_id", "vehicle_number") if source == "insurance"
                              else ("stg_registrations", "reg_id", "reg_no"))
        rid, native = ref.split("|", 1)
        rid_val = None if rid in ("None", "") else int(rid)
        conn = connect("vehicle_dwh")
        cur = conn.cursor()
        if field_name == "row":  # exact duplicate -> remove one copy
            cur.execute(f"DELETE FROM {table} WHERE {idcol} <=> %s AND {pcol}=%s LIMIT 1", (rid_val, native))
        else:
            cur.execute(f"UPDATE {table} SET {pcol}=%s WHERE {idcol} <=> %s AND {pcol}=%s LIMIT 1",
                        (new, rid_val, native))
    else:
        return False
    changed = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return changed


def _log(wcur, *, noisy, chosen, score, ctx, action, note, source=None, ref=None, field_name=None,
         old=None, new=None):
    wcur.execute(
        """
        INSERT INTO dwh_cleanse_log
          (cleansed_at, noisy_plate, chosen_plate, match_score, context_json, action, note,
           source_name, record_ref, field_name, old_value, new_value)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (datetime.now(), str(noisy)[:40], str(chosen)[:20], float(score), json.dumps(ctx, default=str)[:500],
         str(action)[:120], str(note)[:400], source, (ref or "")[:80] or None, field_name,
         None if old is None else str(old)[:60], None if new is None else str(new)[:60]),
    )


def _sync_warehouse(wcur, noisy_plates: set[str], clean_plates: set[str]) -> list[str]:
    """Remove ghost vehicle_360 rows created by noise; refresh last sighting of the clean plate."""
    actions = []
    live: set[str] = set()
    try:
        live = {normalize_plate(p) for (p,) in _rows("roadcapture", "SELECT DISTINCT plate_no FROM capture_log")}
    except Exception:
        pass
    for noisy in noisy_plates - clean_plates:
        if noisy in live:
            continue
        wcur.execute("SELECT plate_canonical FROM vehicle_360 WHERE plate_canonical=%s", (noisy,))
        if wcur.fetchone() is None:
            continue
        partner = next(iter(clean_plates), None)
        wcur.execute("SELECT 1 FROM vehicle_360 WHERE plate_canonical=%s", (partner,))
        if partner and wcur.fetchone() is None:
            wcur.execute("UPDATE vehicle_360 SET plate_canonical=%s WHERE plate_canonical=%s", (partner, noisy))
            actions.append(f"vehicle_360 {noisy}→{partner}")
        else:
            wcur.execute("DELETE FROM vehicle_360 WHERE plate_canonical=%s", (noisy,))
            actions.append(f"vehicle_360 ghost {noisy} removed")
        for t in ("dwh_flagged_cache", "ministry_priority", "dwh_plate_xref"):
            try:
                wcur.execute(f"DELETE FROM {t} WHERE plate_canonical=%s", (noisy,))
            except Exception:
                pass
    for clean in clean_plates:
        try:
            r = _rows("roadcapture",
                      "SELECT capture_time, location FROM capture_log WHERE plate_no=%s "
                      "ORDER BY capture_time DESC LIMIT 1", (clean,))
            if r:
                wcur.execute("UPDATE vehicle_360 SET last_seen_time=%s, last_seen_location=%s "
                             "WHERE plate_canonical=%s AND (last_seen_time IS NULL OR last_seen_time < %s)",
                             (r[0][0], r[0][1], clean, r[0][0]))
        except Exception:
            pass
    return actions


def run_etl_refresh() -> str:
    """Re-run the normal ETL so statuses / xref / serving tables reflect the cleansed sources."""
    try:
        import os
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        res = subprocess.run([sys.executable, str(HERE / "run_etl.py")], cwd=str(HERE), env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        tail = (res.stdout or res.stderr).strip().splitlines()[-1:] or ["(no output)"]
        return ("ETL ok: " if res.returncode == 0 else "ETL failed: ") + tail[0]
    except Exception as e:
        return f"ETL not run: {e}"


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
    run_etl: bool = True,
) -> dict[str, Any]:
    """
    Fix ONE noisy plate everywhere it was recorded (capture, insurance, RTO, theft):
      * rewrite the plate to the chosen vehicle, in each source's own format
      * repair the capture's colour / type using the image + the vehicle's other sightings
      * drop the ghost warehouse row, log every change, refresh the warehouse (ETL)
    """
    noisy_n, chosen_n = normalize_plate(noisy_plate), normalize_plate(chosen_plate)
    if not noisy_n or not chosen_n:
        raise ValueError("noisy_plate and chosen_plate required")
    u = get_universe(max_age=0)
    target = u.entities.get(chosen_n, Entity(chosen_n))
    ctx = {"image_color": query_color, "image_type": query_type, "model": query_model,
           "vehicle_color": target.color, "vehicle_type": target.vtype, "vehicle_model": target.model,
           "score": match_score}
    actions: list[str] = []
    changes = 0

    wh = connect("vehicle_dwh")
    wcur = wh.cursor()
    ensure_cleanse_log(wcur)

    for rec in u.records:
        if rec.plate != noisy_n or (rec.source == "roadcapture" and not fix_capture):
            continue
        new_native = fmt_for_source(chosen_n, rec.source)
        if noisy_n != chosen_n or rec.native != new_native:
            if _apply_change(rec.source, rec.ref, "plate", rec.native, new_native):
                changes += 1
                actions.append(f"{rec.source}#{rec.ref.split('|')[0]} plate {rec.native}→{new_native}")
                _log(wcur, noisy=rec.native, chosen=chosen_n, score=match_score, ctx=ctx, action="plate fixed",
                     note=note or "phase2 cleanse", source=rec.source, ref=rec.ref, field_name="plate",
                     old=rec.native, new=new_native)
        # contextual repair of the sighting itself
        if rec.source == "roadcapture":
            for attr, recorded, image_val, vehicle_val in (
                ("vehicle_color", rec.color, query_color, target.color),
                ("vehicle_type", rec.vtype, query_type, target.vtype),
            ):
                truth = vehicle_val or image_val
                if image_val and vehicle_val and _same(image_val, vehicle_val) is False:
                    truth = vehicle_val  # majority of the vehicle's sightings beats one coarse estimate
                if truth and recorded and _same(recorded, truth) is False:
                    if _apply_change("roadcapture", rec.ref, attr, recorded, truth):
                        changes += 1
                        actions.append(f"capture#{rec.ref} {attr} {recorded}→{truth}")
                        _log(wcur, noisy=rec.native, chosen=chosen_n, score=match_score, ctx=ctx,
                             action=f"{attr} fixed", note="context repair", source="roadcapture",
                             ref=rec.ref, field_name=attr, old=recorded, new=truth)

    actions += _sync_warehouse(wcur, {noisy_n}, {chosen_n})
    if not changes:
        _log(wcur, noisy=noisy_plate, chosen=chosen_n, score=match_score, ctx=ctx,
             action="nothing to change", note=note or "phase2 cleanse")
    wh.commit()
    wcur.close()
    wh.close()
    invalidate_cache()
    etl = run_etl_refresh() if run_etl and changes else "ETL skipped (no source change)"
    actions.append(etl)
    capture_fixed = sum(1 for a in actions if a.startswith("roadcapture#"))
    return {"noisy_normalized": noisy_n, "chosen_plate": chosen_n, "capture_fixed": capture_fixed,
            "changes": changes, "actions": actions}


def cleanse_all(*, include_review: bool = False, run_etl: bool = True, max_passes: int = 4) -> dict[str, Any]:
    """
    Apply every AUTO fix (REVIEW ones only if asked), then look again: fixing a plate can make
    a vehicle corroborated, which exposes e.g. a colour conflict that was invisible before.
    Repeats until a pass changes nothing. Returns before / after counts.
    """
    first = detect_anomalies()
    applied_total, passes = 0, 0
    current = first
    while passes < max_passes:
        passes += 1
        todo = [a for a in current
                if (a.suggested is not None or a.kind in ("DUPLICATE_RECORD", "GHOST_WAREHOUSE_ROW"))
                and (a.decision == "AUTO" or include_review)]
        if not todo:
            break
        wh = connect("vehicle_dwh")
        wcur = wh.cursor()
        ensure_cleanse_log(wcur)
        applied = 0
        for a in todo:
            if _apply_change(a.source, a.record_ref, a.field, a.current, a.suggested):
                applied += 1
                vehicle = a.plate or normalize_plate(a.suggested or a.current)
                _log(wcur, noisy=a.plate or a.current, chosen=vehicle, score=a.score,
                     ctx={"reason": a.reason, "margin": a.margin, "pass": passes},
                     action=f"{a.kind} {a.decision}", note="cleanse_all", source=a.source,
                     ref=a.record_ref, field_name=a.field, old=a.current, new=a.suggested)
        for a in todo:  # ghost warehouse rows: each noisy plate -> its own clean plate
            if a.kind == "NOISY_PLATE" and a.suggested:
                _sync_warehouse(wcur, {normalize_plate(a.current)}, {normalize_plate(a.suggested)})
        wh.commit()
        wcur.close()
        wh.close()
        invalidate_cache()
        applied_total += applied
        if not applied:
            break
        current = detect_anomalies()
    etl = run_etl_refresh() if run_etl and applied_total else "ETL skipped"
    after = detect_anomalies()
    return {"before": anomaly_summary(first), "applied": applied_total, "passes": passes,
            "after": anomaly_summary(after), "remaining": [asdict(a) for a in after], "etl": etl}
