# ADR 0007: M7 PDF Text Extraction

- Status: Accepted
- Date: 2026-08-09
- Scope: M7 only

## Context

M6 stores immutable, structurally validated medical PDFs. M7 must derive useful
text asynchronously without changing the original evidence, widening medical
record authorization, or entering OCR and archive semantics. Digital, scanned,
mixed, encrypted, missing, tampered, and malformed inputs require deterministic
outcomes under duplicate Celery deliveries and concurrent workers.

## Decision

### Parser separation

`pypdf` remains the M6 upload-boundary parser for structural validation, page
counting, and encrypted-file rejection. M7 uses PyMuPDF only after persistence
for page-aware text extraction. This separates fast admission checks from the
heavier derived-content pipeline and keeps both libraries replaceable behind
their service boundaries.

### Processing ownership and persistence

The `processing` module owns the extractor, usability policy, Celery task, and
derived-text models. `DocumentText` is one-to-one with `MedicalDocument` and
stores the exact page-ordered aggregate plus extraction provenance. Ordered
`DocumentTextPage` rows store each page's exact extracted text, meaningful
character count, and `requires_ocr` flag. Original `StoredFile` bytes, digest,
and evidence fields remain immutable.

Page boundaries are authoritative in `DocumentTextPage`; the aggregate joins
pages with a stable form-feed separator. Extracted Unicode is not normalized,
translated, date-parsed, or otherwise interpreted. Extraction method is
`PDF_TEXT`; extractor name/version and pipeline version are persisted.

### Deterministic usability

Meaningful characters are Unicode alphanumeric characters. A page is text
capable when its count reaches `PDF_TEXT_MIN_PAGE_CHARS`. A document is usable
when total meaningful characters reach `PDF_TEXT_MIN_CHARS` and the proportion
of text-capable pages reaches `PDF_TEXT_MIN_TEXT_PAGE_RATIO`. Defaults are 80,
40, and 0.5. `PDF_MAX_PAGES` defaults to 250.

A usable mixed PDF becomes `TEXT_EXTRACTED` while insufficient pages retain
`requires_ocr=true`. A valid PDF whose aggregate text is not usable becomes
`OCR_REQUIRED`, not `FAILED`; any safely extracted page text is retained.

### Asynchronous lifecycle and idempotency

Successful PDF upload sets `QUEUED`, records `PDF_EXTRACTION_QUEUED`, and uses
`transaction.on_commit` to enqueue a Celery task containing only the stable
document UUID. JPEG and PNG uploads remain `UPLOADED` and are not enqueued.

The task locks briefly to claim eligible work as `PROCESSING`, releases the row
lock before reading/parsing, then locks again to persist. Existing canonical
derived text is reused unless an explicit internal reprocess is requested.
Workers encountering `PROCESSING` or a canonical result do not duplicate work.
Final persistence rechecks that the document is active, preventing a concurrent
deletion from restoring derived content.

Canonical states for this phase are `UPLOADED`, `QUEUED`, `PROCESSING`,
`TEXT_EXTRACTED`, `OCR_REQUIRED`, and `FAILED`. Earlier future-state enum values
remain reserved for later milestones but are not produced by M7.

### Failure and retry policy

Transient storage I/O is retryable with bounded Celery retries and exponential
backoff. Missing blobs, integrity mismatch, encrypted input, parser rejection,
page-limit rejection, malformed extractor output, unsupported media, and final
database persistence failures are controlled non-retryable outcomes. Stable
internal failure codes are stored; client responses expose none of them in M7.

A quarantined, corrupted, deleted, or non-PDF document is never extracted.
PyMuPDF encryption is never bypassed. A successful canonical result cannot be
overwritten by a late failed worker.

### API and privacy

M7 adds no extracted-text endpoint. Authorized document detail responses gain
only `text_available`; lists and downloads remain unchanged. Existing owner and
live verified-guardian resolution remains authoritative, so unrelated users,
expired/revoked/pending guardians, adults aged 18, agents, and anonymous callers
gain no access.

Logs and events contain document UUID, task state, page count, duration, status,
and stable failure code only. They never contain extracted text, patient names,
Digital IDs, raw files, filenames, or storage paths.

## Consequences

- M8 can process only pages marked `requires_ocr` without discarding digital
  text.
- Exact derived text is sensitive database content even though no M7 API
  returns it; database encryption and retention remain operational concerns.
- A 25 MiB M6 upload ceiling and M7 page ceiling bound extraction input. Celery
  task time limits provide the execution timeout policy.
- PyMuPDF 1.28.0 is dual AGPL/commercial licensed; deployment owners must ensure
  the selected license is compatible with distribution and service operation.
- Broker enqueue failure after commit can leave a document queued; operational
  redelivery/outbox hardening is deferred.

## Explicitly deferred

OCR, image OCR, multilingual date detection, date candidate ranking, archive
indexing/search, facilities, doctors, AI features, and full-text APIs are outside
M7.
