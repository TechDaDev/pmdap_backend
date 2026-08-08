# M7 PDF Text Extraction Design

## Goal

Asynchronously extract page-preserving text from accepted medical PDFs, persist
provenance and usability evidence, route non-usable PDFs to `OCR_REQUIRED`, and
preserve every M0-M6 security and storage invariant.

## Module contract

`PDFTextExtractor.extract(content)` returns an immutable structured result with
aggregate `text`, `page_count`, ordered `pages`, `character_count`, `usable`,
`reason`, and safe metadata. Each page contains `page_number`, exact `text`,
meaningful-character count, and `requires_ocr`. Extractor failures are typed as
retryable or non-retryable and expose stable codes, never parser internals.

`TextUsabilityEvaluator` is a pure deterministic policy. It reads the named
settings `PDF_TEXT_MIN_CHARS`, `PDF_TEXT_MIN_PAGE_CHARS`, and
`PDF_TEXT_MIN_TEXT_PAGE_RATIO`. Unicode alphanumeric characters are meaningful;
whitespace and punctuation are not. The evaluator does no linguistic
normalization and does not infer dates.

`DocumentText` and `DocumentTextPage` are derived records owned by `processing`.
They cannot mutate `StoredFile`. A one-to-one and a unique `(document_text,
page_number)` constraint enforce one canonical result and one row per page.

## State machine

- PDF upload: `QUEUED` after storage/integrity success.
- Worker claim: `QUEUED|FAILED -> PROCESSING` plus `PDF_EXTRACTION_STARTED`.
- Usable result: `TEXT_EXTRACTED` plus `PDF_TEXT_EXTRACTED`.
- Valid non-usable result: `OCR_REQUIRED` plus `PDF_OCR_REQUIRED`.
- Controlled terminal error: `FAILED` plus `PDF_EXTRACTION_FAILED`.
- Transient storage error: record retryable failure, return to `QUEUED`, and
  request bounded Celery retry.
- JPEG/PNG, deleted, corrupted, and quarantined input: safe no-op; never parse.

The task input is a document UUID string only. Claim and persistence occur in
short transactions around an unlocked parse. Final persistence locks and
rechecks active/integrity state. Existing canonical text is reused by default.

## Thresholds and bounds

Defaults:

- `PDF_TEXT_MIN_CHARS=80`
- `PDF_TEXT_MIN_PAGE_CHARS=40`
- `PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5`
- `PDF_MAX_PAGES=250`
- `PDF_EXTRACTION_MAX_RETRIES=3`
- `PDF_EXTRACTION_RETRY_BASE_SECONDS=5`

The existing 25 MiB medical-upload ceiling bounds bytes. The extractor rejects
content beyond recorded size/integrity expectations and page count above the
M7 ceiling. Celery soft/time limits bound runaway execution.

## API contract

No new route is added. Adult and guardian document detail success schemas add a
read-only boolean `text_available`. It is true only when canonical derived text
exists for an active document. List responses never include extracted text or
the new availability field. No serializer returns page text, failure codes,
extractor metadata, storage evidence, or processing internals.

## Security and privacy

Threats are unauthorized metadata inference, parser abuse, decompression/page
explosion, duplicate-worker races, integrity bypass, encrypted-PDF bypass,
sensitive log leakage, and deletion races. Controls are existing authority
lookups, type/integrity gates, byte/page/time limits, no password bypass,
database uniqueness and locks, final active recheck, safe errors, and allowlisted
structured logs.

Test documents are synthetic. Tests assert exact English/Arabic/mixed Unicode
and Arabic digit preservation, no raw text in logs/events/API, and unchanged
original digest/bytes.

## Verification

TDD covers extractor behavior, usability thresholds, state/event transitions,
upload dispatch, mixed/scanned PDFs, encryption, missing/tampered/malformed
input, retry classification, idempotency, deletion races, safe API metadata,
OpenAPI accuracy, privacy logging, and PostgreSQL concurrent delivery. Final
acceptance runs targeted M7 tests, the full suite with branch coverage >=90%,
Ruff check/format, Django checks, migration drift, schema validation, Docker
Compose, Redis/Celery smoke, and PostgreSQL concurrency.

## Non-goals

OCR, images as OCR inputs, date extraction/ranking, archive indexing/search,
facilities, doctors, AI, and extracted-text APIs are deferred.
