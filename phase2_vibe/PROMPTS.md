# Phase 2 — prompts used (vibe coding)

Two prompts, in order. The code produced by prompt 1 is frozen in `generated/`.
What we changed after reviewing it is listed in `CHANGES.md`, and shown line by line in
`changes.html` (run `python phase2_vibe/show_changes.py`, or open the **Prompt & code
changes** tab in the app).

---

## Prompt 1 — generate the feature (Cursor)

```text
For our IIA uninsured-vehicles warehouse (4 MySQL sources + vehicle_dwh + Streamlit),
add Phase-2 entity matching under noisy OCR plates:

1) Given a scanned/noisy registration string, find all approximately matching vehicles
   in the warehouse (and vice-versa: clean plate → nearby noisy/alternate spellings).
   Use a combination that works for Indian plates: normalize separators, OCR confusion
   (0/O, 1/I/L, 8/B, …), Levenshtein, Jaro-Winkler, then re-rank with contextual
   color/model/type from the camera image / RTO. Justify weights.

2) Cleanse recorded data: when the user confirms the best match, fix the noisy
   capture_log plate, merge vehicle_360 if needed, and write dwh_cleanse_log so
   anomalies/noise are removed. Evaluator will INSERT/UPDATE/DELETE rows live.

3) Streamlit tab "Entity match" + seed script for a demo noisy plate + short viva notes.
Keep warehouse-only dashboard style; no ministry dossier.
```

Output: `entity_matching.py`, `plate_ocr.py`, `populate_noisy_data.py`, the Entity match tab in
`app.py`, `PHASE2_VIVA.md` (copies of the four code files in `generated/`).

---

## Prompt 2 — review against the rubric and fix

```text
Your project phase 2 evaluation ... you need to demonstrate the data integration (and entity
matching) under noisy data ... if the vehicle registration is not matched exactly, how to find
out all vehicles which are very close to the scanned (image) registration number, and then how
to cleanse this noisy record with the help of other contextual data (like model, color etc).
Rubric:
1. Given a scanned image of a car registration, find out all vehicles approximately matching
   with this noisy car plate and vice-versa. Apply the matching algorithm which gives the best
   accuracy. Justify why you have taken a particular algorithm or combination. (5)
2. Cleanse the recorded data w.r.t. the image data and other contextual information so that
   there are no anomalies (including the noise) in the recorded data. (5)
3. Show the prompt (for vibe coding), generated code and highlight the changes you have made
   in the generated code. (5)
Recent changes were made — run the db check whether I met all criteria for full marks and make
any necessary changes (also make it easy to use and understand).
```

Output of the review: the bugs and gaps in `CHANGES.md`, the new `evaluate_matching.py`
(accuracy evidence) and `phase2_check.py` (one-command rubric check).
