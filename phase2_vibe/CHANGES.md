# Changes made to the AI-generated code (rubric 3)

`generated/` = code as produced by prompt 1. Below: what was wrong when we tested it against
the database, and what we changed. Line-level highlights: `changes.html`.

## A. Matching (rubric 1)

**A1. Ranking bug: exact matches were ranked LAST.** `entity_matching.py`
The generated sort key put `is_exact` first and context before score, so an insurance row
`DL-9-CAF-8888` (same vehicle, other format) came at the bottom. Measured: only 20/40 insurance
spellings found at rank 1.
```python
# generated
return (is_exact, -h.context_bonus, -h.score, h.ocr_dist, h.lev)
# now
hits.sort(key=lambda h: (-h.score, h.ocr_dist, h.lev, h.plate_canonical))
```

**A2. OCR look-alike cost 0 → 0.25, plus transpositions.** With cost 0 the true plate and an
OCR twin were indistinguishable. Now an exact read still beats a look-alike read, and swapped
neighbours (`8127`→`8172`) cost 1 (Damerau) instead of 2.

**A3. New signal: Indian plate grammar (format repair).** `SS 9[9] X[XX] 9999`: a letter in a
digit slot is repaired to the digit it resembles (`DL9CAF8B88` → `DL9CAF8888`). `I`/`O` are
never used in a series.

**A4. Weights measured, not guessed.** Generated weights `0.45/0.25/0.30` were hand-picked.
`evaluate_matching.py` grid-searches them on a DEV half and reports a held-out TEST half.
Each test plate competes with about 24 realistic look-alikes (same series, nearby numbers).
Result: OCR 0.3, Levenshtein 0.3, Jaro-Winkler 0.3, format 0.1. This is the most balanced of
the near-optimal weightings.

| method (400 held-out noisy plates) | top-1 |
|---|---|
| exact (phase 1) | ~14 % |
| Levenshtein | ~33 % |
| Jaro-Winkler | ~41 % |
| OCR-weighted | ~67 % |
| combined (string only) | ~67 % |
| **combined + context (used)** | **~98–99 %** |

**A5. Candidate universe.** The generated code searched only `vehicle_360`, which also holds
the ghost rows made by the noise. A noisy plate therefore "matched itself". Now:
- Every source is loaded.
- A plate is trusted only if it has a valid format, appears in ≥ 2 sources, and is not just an
  OCR twin of a registered plate.
- An OCR-skeleton + symmetric-delete index (blocking) keeps queries fast.

**A6. Vice-versa was not really vice-versa.** It reran the same warehouse search. Now
`find_records_near_plate()` scans the raw records of all 4 sources and the scanned images, and
gives a verdict per row.

**A7. Context.** The generated code only had bonuses. Now there is also a penalty when colour,
type or model disagree. A vehicle's colour is the majority vote of its sightings.

**A8. Image read from PIXELS.** The generated `plate_ocr.py` returned the plate stored inside
the PNG metadata, not what is in the picture. Now the plate is read by template-matching OCR
(PIL) or by tesseract. The metadata is used only as a cross-check. Colour now comes from the
car body, not the whole image. With the whole image, the white plate turned Black cars into
"Grey" and Grey cars into "Silver".

## B. Cleansing (rubric 2)

**B1. Only `capture_log` was cleansed.** Noisy insurance and RTO rows stayed noisy. Now each
fix goes into every source, written in that source's own format (hyphen / lowercase / spaced).

**B2. Context repair.** A sighting whose colour or type disagrees with the vehicle's other
sightings (or the image) is corrected. Example: the evaluator changes a colour.

**B3. Anomaly scan and "0 left" proof.** New `detect_anomalies()` covers NOISY_PLATE,
FORMAT_VARIANT, CONTEXT_CONFLICT, GHOST_WAREHOUSE_ROW and DUPLICATE_RECORD, each marked AUTO
or REVIEW with a reason. New `cleanse_all()` repeats until nothing changes, because fixing a
plate can reveal a colour conflict. It then runs the ETL. Every change is logged with the old
and new value in `dwh_cleanse_log`.

**B4. Ghost warehouse rows** created by noisy sightings are now removed, so the KPI counts go
back to the true numbers.

**B5. The seeder was corrupting the truth.** `populate_noisy_data.py`:
- It deleted the real policy when its "noisy" spelling equalled the real one
  (`DELETE ... WHERE vehicle_number='GJ-6-ZS-5998'`), then inserted a fake `NoiseDemoInsurer`
  policy. Vehicles flipped status; 19 real policies were lost.
- It inserted fake owners (`Noise Owner 0`) with random models, so the context became wrong.
- Several rows shared the same id.

Now the seeder re-keys the vehicle's own real row and keeps the original value in the manifest
so re-runs undo it. It also repairs the old damage and adds dropped-char, swapped, format and
wrong-colour noise. A decoy set (`DL9CAF8883`, `DL9CAF3888`) shows context breaking a 3-way tie.

**B6. The demo leaked the answer.** The generated tab pre-filled the "expected clean plate"
and the model from the seed manifest (the answer key). Removed. The manifest is now used only
for scoring (`evaluate_matching.py`, `phase2_check.py`).

## C. Usability
- The Entity match tab has 4 numbered sub-tabs (Match / Cleanse / Accuracy / Prompt & changes).
  Scans can be picked from a list instead of a file dialog.
- `phase2_check.py`: one command prints PASS/FAIL for each rubric item, with a rehearsal mode
  that simulates the evaluator's INSERT / UPDATE / DELETE.

## D. Fixes after running the check on the real (smaller) database
- **Wrong-colour noise.** Most vehicles are seen once, so no colour noise could be seeded. The
  seeder now adds one supporting correct sighting first, so a majority exists.
- **Typos that are still valid plates** (`ch3bqy1229` for `CH38QY1229`). These were skipped
  silently. The seeder now prefers mis-reads that break the plate grammar. If two
  uncorroborated look-alike twins still occur, they are flagged as REVIEW with the suggested
  plate.
- **A valid plate counts as noise only within one slip** of a trusted vehicle. A random theft
  plate two edits away is a different vehicle.
- **The evaluator simulation crashed** on small data. It now picks any captured + registered
  vehicle and adds helper sightings when needed, then removes them.

## E. Frontend and OCR (strict-review pass)
- **Simulate a bad scan.** Blur, dust and mud on one character are applied to the picture
  *before* OCR, so the noise comes from reading a clean plate, not from noisy text drawn into
  the image. A median filter in the OCR handles dust.
- **Per-character OCR confidence.** Characters read below 0.60 become `?`. The matcher treats
  `?` as "unknown" (cost 0.25, the plate grammar fills it in) instead of trusting a wrong letter.
  This is a new noise type in the benchmark: string-only methods ~20%, ours ~96%.
- **Explanations in the UI:**
  - colour-coded character alignment (same / OCR look-alike / unreadable / different / missing)
  - per-signal score-contribution chart
  - progress strip for the viva steps
  - **before → after** view of the exact source rows the cleanse changed

## F. Crop / straighten uploaded photos
- In the Match tab: **Auto-find plate** finds the bright rectangle with characters in it.
  **Rotate** straightens a tilted plate. Two range sliders crop the image, or you can drag a box
  if `streamlit-cropper` is installed. The cropped plate is what gets read.
- Tesseract input is prepared the standard way (plate only, about 120 px tall, Otsu binarisation,
  white border).
- Car colour is now taken from the bumper around the plate, not the photo edges.
