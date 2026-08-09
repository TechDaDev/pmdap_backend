# M9 Multilingual Date Detection Design

## Contract

M9 consumes only canonical `DocumentTextPage.text` produced by M7/M8. A
deterministic pipeline normalizes derivative working text, detects supported date
expressions, preserves raw occurrence provenance, classifies nearby controlled
labels, scores archival relevance, and persists `DateCandidate` rows. Raw native
and OCR text never changes. No LLM, translation, medical interpretation, or OCR
retry participates.

## Normalization and parsing

- Normalize Unicode with NFKC while retaining an index map to source text.
- Translate Arabic-Indic and Persian digits one character at a time.
- Normalize supported Unicode date separators and collapse whitespace without
  losing raw occurrence boundaries.
- Parse DMY numeric, ISO/YMD numeric, and English named-month forms explicitly.
- Validate dates with `datetime.date`; impossible dates produce no candidate.
- Numeric values where day and month are both plausible preserve a DMY primary,
  MDY alternative, parsing rule, and `ambiguous=true`.

## Candidate and ranking invariants

`DateCandidate` stores document/page, detected and alternative dates, raw and
normalized values, semantic type, bounded context, text source, occurrence
offset, score, ambiguity, parsing rule, pipeline version, and advisory
`is_suggested`. A conditional PostgreSQL constraint permits at most one
suggested row per document. A second uniqueness constraint prevents duplicate
occurrences within a pipeline version.

Classification uses explicit English/Arabic dictionaries and nearest same-line
label proximity. Named weights rank report, result, issue, and examination dates
above collection/sample/admission/discharge dates. Print dates rank low; DOB
ranks near zero; unlabeled and ambiguous dates stay weak. Dates beyond configured
future tolerance receive a strong penalty. Old dates remain valid. OCR confidence
does not override semantics.

Suggestion requires configured minimum score. Different top dates tied within
configured tolerance produce no suggestion. Repeated equivalent top occurrences
use stable page/offset order. Suggestion never means patient verification and
never changes `MedicalDocument.document_date`, especially verified manual dates.

## State, dispatch, and concurrency

Native PDF or OCR persistence reaching `TEXT_EXTRACTED` records a queued event
and schedules `processing.detect_document_dates` only after transaction commit.
Task input is document UUID. Claim uses a PostgreSQL row lock and
`DATE_PROCESSING`; success ends in `DATE_DETECTED`, while valid text with no date
ends in `DATE_NOT_FOUND`. Actual controlled failure uses `FAILED` with a safe
code. Bounded retry applies only to transient database/task failures.

Parsing runs outside the claim transaction. Final persistence locks and
revalidates active document, processing claim, and source `DocumentText` UUID.
Candidate rows are replaced atomically for current pipeline version. Duplicate
workers reuse current authoritative outcome; stale failure cannot replace
success; deletion cannot resurrect a document.

## API and privacy

Add read-only paginated candidate routes for adult-owner and existing minor
guardian resource shapes. Reuse current medical-document authorization and 404
IDOR behavior. Response allowlists date, type, numeric score, page, bounded
context, source, ambiguity, alternative date, and suggestion flag. It excludes
raw full text, storage data, patient data, parser internals, and OCR internals.

Context is capped and stripped of unsafe control characters. Logs/events contain
only UUID, count, suggestion boolean, duration, pipeline version, status, and
stable failure code. No context, detected date, DOB, patient identity, or medical
text enters logs/events.

## Persistent development database

Docker PostgreSQL 17 with named `pmdap_backend_postgres_data` is authoritative
for local development and integration. Redis uses its own retained named volume.
SQLite remains a fast isolated test lane only. Acceptance verifies persistent
M8-to-M9 migration, fresh migration from zero, and PostgreSQL race constraints.

## Acceptance

Tests cover digits/Unicode/whitespace, all required formats and invalid dates,
English/Arabic labels, mixed language, ambiguity, ranking/ties/future dates,
manual-date precedence, no-date behavior, idempotency, deletion/stale races,
authorization/privacy/OpenAPI, native PDF integration, OCR integration, real
PostgreSQL concurrency, and real Celery flows.

## Non-goals

M10 confirmation/selection, final archive indexing, facility or document
classification, search, doctor access, AI/LLM analysis, and alternate OCR engines
remain excluded. PyMuPDF licensing remains unresolved.
