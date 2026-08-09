# M16 — OCR & Date-Extraction Benchmark

Evaluates the production **PaddleOCR 3.7.0 + M9 deterministic date pipeline**
for archival report-date extraction. All committed fixtures are synthetic.

## Harness

```
python benchmarks/ocr/benchmark.py [--set frozen|expanded|all] [--limit N] [--dpi 300]
python benchmarks/ocr/real_benchmark.py --manifest /tmp/manifest.json   # private dataset
```

- `benchmarks/ocr/fixtures.py` — synthetic report fixtures (English, Arabic,
  mixed; clean + degraded; multiple formats; M16.1 expanded structural set).
- `benchmarks/ocr/benchmark.py` — runs the production `PaddleOCREngine` +
  `detect_page_dates`/`choose_suggested_index`, compares against ground truth.
- `benchmarks/ocr/real_benchmark.py` — evaluates a PAIRED PDF/JPG private
  dataset (manifest with source paths stays outside Git; results are
  anonymized aggregates only).
- `benchmarks/ocr/out/` — generated `results.json`, `results.csv`, `summary.md`.

Writes no patient data, no raw medical text to logs, and does not touch the
persistent database.

## M16.1 M9 remediation

`processing/dates.py` (pipeline `m9-date-v2`) now:

- associates a date with an **explicit label on the immediately preceding
  line** (label-before-value), not just the same line — fixes real PDFs where
  the text layer splits "Date" and the value;
- adds `APPLICATION_DATE` type so "Date of Application" never wins over an
  explicit Report Date;
- adds bare `Issued` as an `ISSUE_DATE` label;
- adds **strict compact-date recovery** (exactly 8 digits under a strong
  non-generic label, valid calendar, never near identifier semantics);
- reduces the generic-label penalty so a bare "Date : ..." report is still
  suggestable.

Safety: previous-line-only association (no trailing-label reassignment),
DOB false-selection stays 0%, and a no-suggestion is preferred over a wrong
confident suggestion.

## Engines

- **A (baseline):** PaddleOCR 3.7.0, PaddlePaddle 3.3.0 (CPU, mkldnn off),
  `PP-OCRv5_mobile_det` + `arabic_PP-OCRv5_mobile_rec`, render DPI 300.
- **B (comparator):** Tesseract — not installable here (no sudo for apt; no
  pullable container image in this environment). Not benchmarked.

## Findings

1. **English/mixed date extraction is strong** after remediation
   (frozen synthetic suggestion accuracy ≈ 0.90 / 1.0; expanded ≈ 0.90).
2. **Real paired dataset:** JPG (scan) path 100% correct; PDF native-text
   path 2/3 — the page-1 date exists only in the embedded scan image and is
   missed by the native-text layer (honest product limitation for hybrid
   PDFs). No wrong suggestions; no DOB false-selection.
3. **Synthetic Arabic still fails** where the recognizer drops inline western
   date digits / Arabic-Indic-Persian separators. Not reproduced on the real
   (English) pages, so production OCR was not overfitted to the synthetic
   renderer. Flagged for a future Arabic-OCR pass on real Arabic scans.
4. Zero wrong *confident* suggestions on all benchmark sets after remediation.

These are benchmark findings; the only production change is the M9
deterministic logic + `APPLICATION_DATE` type (+ `m9-date-v2` pipeline).

