# M16 — OCR & Date-Extraction Benchmark

Evaluates the production **PaddleOCR 3.7.0 + M9 deterministic date pipeline**
for archival report-date extraction. All fixtures are synthetic.

## Harness

```
python benchmarks/ocr/benchmark.py [--limit N] [--dpi 300] [--output-dir out]
```

- `benchmarks/ocr/fixtures.py` — synthetic report fixtures (English, Arabic,
  mixed; clean + degraded; multiple formats).
- `benchmarks/ocr/benchmark.py` — runs the production `PaddleOCREngine` +
  `detect_page_dates`/`choose_suggested_index`, compares against ground truth.
- `benchmarks/ocr/out/` — generated `results.json`, `results.csv`, `summary.md`.

Writes no patient data, no raw medical text to logs, and does not touch the
persistent database.

## Engines

- **A (baseline):** PaddleOCR 3.7.0, PaddlePaddle 3.3.0 (CPU, mkldnn off),
  `PP-OCRv5_mobile_det` + `arabic_PP-OCRv5_mobile_rec`, render DPI 300.
- **B (comparator):** Tesseract — not installable in this environment without
  `sudo` (tesseract-ocr / tesseract-ocr-ara require apt). Not benchmarked.

## Ground truth

Authored in `fixtures.py` (expected report date + label) — never derived from
the OCR engine. See `test_m16_regression.py` for the deterministic M9-layer
regression fixtures.

## Key findings (see summary.md for current numbers)

1. **English/mixed date extraction is strong** (suggestion accuracy ≈ 0.90 /
   1.0 on synthetic clean + degraded English fixtures; native-PDF control 1.0).
2. **Arabic date extraction fails on this synthetic set** — the Arabic
   recognizer drops inline western date digits and loses Arabic-Indic/Persian
   separators; M9 then has no date or an UNKNOWN (below-threshold) candidate.
3. **M9 classifies dates by same-line label only** — a date on its own line
   below the label is UNKNOWN and not suggested.
4. No wrong *confident* suggestions observed; DOB never won as report date.

These are benchmark findings, not production product changes.
