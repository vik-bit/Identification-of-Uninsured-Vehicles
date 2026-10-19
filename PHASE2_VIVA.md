# Phase 2 viva — noisy entity matching & cleansing (Oct 21–23)

## 0. Before the viva (5 minutes)

```powershell
# MySQL containers up (roadcapture :3307, theft :3310, warehouse :3306), then:
python phase2_check.py --full      # re-seed noise, run everything, PASS/FAIL per rubric item
python populate_noisy_data.py      # fresh noise for the live demo (check --full cleansed it)
python run_etl.py
python generate_sample_plate_images.py
streamlit run app.py               # -> tab "Entity match"
```
`phase2_check.py` (no flag) is read-only. `--fix` cleanses. `--full` rehearses the whole viva,
including the evaluator's live INSERT / UPDATE / DELETE.

## 1. Demo script (app → Entity match)

**① Match**
1. Sample scan `scan_DL9CAF8B88.png` → **Read plate from image**.
   Result: `DL9CAF8B88`, Black, SUV, via *template-matching OCR (pixels)*.
2. **Find matches** → `DL9CAF8888` at rank 1, score 1.00.
   Why: OCR distance 0.25 (B↔8), format repair agrees (a B can't sit in a digit slot), colour ✓, type ✓.
   Two look-alikes follow with colour ✗ and type ✗.
3. Sample scan `scan_DL9CAF888.png` (one digit lost) → **Find matches**.
   Three plates are each 1 edit away, so the string score is a tie. The app says so, and
   Black / SUV (+ model Tata Nexon) picks `DL9CAF8888`. *This is why context is needed.*
4. **Bad scan of a clean plate:** open *Simulate a bad scan* and put mud on character 9.
   The OCR now misreads it with low confidence (shown in red under that character), so it
   becomes `?`. The scan still matches `DL9CAF8888`, and the colour-coded alignment shows why.
   This is noise created by the *reading*, not typed into the data.
4b. **Real photo:** Upload a photo → open *Crop / straighten* → **Auto-find plate**
   (rotate if tilted) → Read. Without cropping, Tesseract often reads nothing from a full photo.
5. Vice-versa: switch direction, plate `DL9CAF8888` → every noisy record (capture rows) and
   scanned image that is close to this vehicle.

**② Cleanse**
5b. Choose `DL9CAF8888` → **Cleanse noisy record** → the list of changes (capture row
   rewritten, ghost warehouse row removed, ETL refreshed).
6. **Scan for anomalies** → about 70 anomalies across capture / insurance / RTO / warehouse:
   noisy plates, wrong format, wrong colour.
   **Cleanse all** → "Anomalies left: 0". Show the cleanse log (old → new value, per source).
7. Show the SQL in the expander (source rows now clean, no ghost row, audit trail).

**③ Accuracy & why** → **Run accuracy benchmark** → table + chart (section 2 below).

**④ Prompt & code changes** → prompts, generated code, highlighted diff (section 4).

**If the evaluator edits data live:**
- INSERT a capture with a wrong plate → ETL → ① Match finds the real vehicle; ② Scan shows NOISY_PLATE → Cleanse.
- UPDATE a sighting's colour → ② Scan shows CONTEXT_CONFLICT (other sightings disagree) → Cleanse restores the majority colour.
- DELETE a row → ② Scan shows a GHOST_WAREHOUSE_ROW if a warehouse row is left with no source → Cleanse removes it.

## 2. Rubric 1 — algorithm and justification

`entity_matching.py`, score = `w_ocr·OCR + w_lev·Levenshtein + w_jw·Jaro-Winkler + w_fmt·format + context`

Weights are **not hand-picked**: `train_plate_matching.py` fits `LogisticRegression` on a compact
labelled set (noisy plate vs candidate, match/non-match), takes the positive coefficients, and
L1-normalises them into `match_weights.json`. OCR glyphs use a **road-type-style dictionary**
(`plate_ocr_lookup.json`): seed look-alikes plus variants discovered from training pairs (≥2).

| signal | why it is needed |
|---|---|
| normalise | separators / case differ per source (`DL-9-CAF-8888`, `dl9caf8888`) |
| OCR-weighted edit distance | scanners confuse 0/O, 1/I, 8/B, 5/S, 2/Z, 6/G: cost 0.25 instead of 1, swaps cost 1 |
| Levenshtein | dropped / extra characters (smudged plate) |
| Jaro-Winkler | state + RTO prefix is usually read correctly |
| Indian plate grammar | `SS 9[9] X[XX] 9999`: a letter in a digit slot is repaired |
| context (colour, type, model) | breaks ties between plates the text cannot separate |

Speed: an OCR-skeleton + symmetric-delete index (blocking) reaches the true plate in 100% of
cases while scoring only a few dozen candidates.

**Evidence** (`evaluate_matching.py`): 600 real vehicles' plates are corrupted 7 ways. Each one
competes with about 24 realistic look-alikes (same series, nearby numbers, which is how RTOs
allot plates). App weights come from the logistic train step above; the eval script can still
grid-search for comparison.

| method | top-1 |
|---|---|
| exact (phase 1) | ~14 % |
| Levenshtein | ~33 % |
| Jaro-Winkler | ~41 % |
| OCR-weighted | ~67 % |
| combined, no context | ~67 % |
| **combined + context (ours)** | **~98–99 %** |

By noise type: OCR weighting fixes look-alike swaps (Levenshtein ~20% → 100%). Context fixes
dropped digits (~10% → ~98%). On the seeded DB noise, 100% is recovered.
*Why not one algorithm:* each one fails on a different kind of noise (see the per-noise table in ③).

## 3. Rubric 2 — cleansing

`detect_anomalies()` checks every record in every source:

- **NOISY_PLATE**: the plate is not corroborated by another source but is close to a trusted
  vehicle. Trusted = valid format + seen in ≥ 2 sources + not just an OCR twin.
- **FORMAT_VARIANT**: right vehicle, wrong format for that source.
- **CONTEXT_CONFLICT**: a sighting's colour or type disagrees with the vehicle's other sightings.
- **GHOST_WAREHOUSE_ROW** and **DUPLICATE_RECORD**.

Each anomaly is AUTO (confident: high score and a clear gap to the runner-up, or confirmed by
context) or REVIEW.

`cleanse_noisy_record()` / `cleanse_all()`:

1. Rewrite the plate in every source, in that source's own format.
2. Correct colour / type from the image + majority of sightings.
3. Remove ghost warehouse rows.
4. Log old → new in `dwh_cleanse_log`.
5. Repeat until nothing changes.
6. Run the ETL.

`phase2_check.py --full` proves that 0 anomalies are left, that every cleansed value equals
the true value (the answer key), and that the KPI counts return to the true numbers.

## 4. Rubric 3 — prompt, generated code, our changes

Folder `phase2_vibe/` (also in the app, ④):
- `PROMPTS.md`: prompt 1 (generate) and prompt 2 (review against the rubric and fix)
- `generated/`: the four files exactly as the AI produced them
- `CHANGES.md`: what was wrong and what was changed. Key points: the ranking bug, OCR cost 0,
  hand-picked weights, matching against ghost rows, the image read from hidden metadata, only
  capture_log cleansed, a seeder that deleted real policies, and a demo that leaked the answer key.
- `python phase2_vibe/show_changes.py` → `changes.html`, a side-by-side highlighted diff

## 5. Files

| file | role |
|---|---|
| `entity_matching.py` | matching, vice-versa, anomaly detection, cleansing |
| `plate_ocr.py` | image → plate (pixels), colour, type; sample scan generator |
| `populate_noisy_data.py` | realistic, reversible noise + answer key (`--undo` to remove) |
| `evaluate_matching.py` | algorithm comparison (rubric 1 evidence) |
| `phase2_check.py` | one-command rubric check |
| `phase2_vibe/` | rubric 3 evidence |
