# 0017 — M18 Structured Lab Result Extraction

- Status: accepted
- Date: 2026-08-18
- Related: 0007 (M7 PDF text), 0008 (M8 OCR subsystem), 0009 (M9 dates)

## Context

Medical-report body OCR persistence is accepted. OCR line geometry (Paddle
`rec_boxes`) exists in memory during OCR but is discarded before persistence.
Plain flattened OCR text cannot reliably reconstruct lab-table rows: columns
(Test / Result / Unit / Reference Range / Flags / Low / High) lose their
spatial association, and reports such as the CBC print two six-column panels
side by side.

Future milestones (lab history, trends, medical document search, report
analysis) need trustworthy test → value → unit → reference relationships.

## Decision

1. **Persist normalized OCR geometry as `DocumentTextSpan`.**
   - One row per OCR line that carries a bounding box.
   - Coordinates normalized to the page (0.0–1.0), independent of source
     resolution; `page_width` / `page_height` retain the source pixels the OCR
     engine actually saw.
   - `sequence` preserves OCR reading order per page.
   - Canonical human-readable text stays in `DocumentText` / `DocumentTextPage`;
     spans are supplemental spatial evidence only.

2. **Add structured lab models in a new `labs` app.**
   - `LabReportExtraction` — one per document per pipeline version, with
     status (`QUEUED / COMPLETED / NOT_APPLICABLE / FAILED`), safe error code,
     result count, aggregate extraction confidence.
   - `LabResult` — one structured row: raw + normalized test name, raw result +
     `Decimal` numeric when safe, raw unit, raw reference range + `reference_low`
     / `reference_high` only for simple unambiguous numeric ranges, printed flag
     (`flag_raw`) recorded but never interpreted clinically.
   - `source_spans` M2M links every structured value to its OCR evidence.

3. **Parse from persisted spans, never a second OCR pass.** The parser groups
   spans into rows by Y proximity, detects header rows, splits side-by-side
   panels at repeated TEST columns, and maps cells to columns via header
   geometry. Generic cues only — no per-report template hardcoding.

## Why not raw Paddle payload blobs

Raw engine arrays (`dt_polys`, tensors, crop images) are engine-specific,
large, and unversioned. Normalized `x/y` bounds are resolution-independent,
queryable, and sufficient for table row/column reconstruction.

## Privacy

OCR text and lab values are sensitive medical data. The body and structured
values are internal processing records: never returned by patient-facing APIs
in this milestone, never indexed into search, never placed in audit payloads or
logs, and excluded from admin list views (read-only detail inspection only).

## Failure semantics

Lab extraction is non-fatal. A failed or missing extraction never invalidates
the archived document or its OCR body. Reprocessing the same pipeline version
atomically replaces the previous extraction (idempotent); parser failure
records `LabReportExtraction.status = FAILED` with a safe error code and leaves
no partial rows.

## Consequences

- Geometry persistence adds one table and modest write cost (spans are written
  once during OCR persistence).
- Future lab parsers can rely on normalized coordinates without re-running OCR.
- A future patient-facing lab history UI can consume `LabResult` rows; clinical
  interpretation (HIGH/LOW/ABNORMAL) remains out of scope.
