# M7 PDF Text Extraction Implementation Plan

**Goal:** Persist safe, page-aware PDF text asynchronously and stop before OCR.

**Architecture:** Keep M6 validation/storage in `documents`; add extraction,
usability, derived persistence, and Celery orchestration in `processing`. Reuse
existing authorization and expose detail-only availability metadata.

**Tech Stack:** Django, PostgreSQL, Celery/Redis, pypdf, PyMuPDF, DRF,
drf-spectacular, pytest, Ruff.

## Task 1: Extractor and usability contract

1. Write failing unit tests for digital, multi-page, Unicode, scanned, mixed,
   junk, encrypted, malformed, and page-bound results.
2. Add PyMuPDF and implement typed results, errors, and deterministic evaluator.
3. Run focused tests and refactor only while green.

## Task 2: Derived persistence and lifecycle

1. Write failing model/state/event tests.
2. Add `DocumentText`, `DocumentTextPage`, constraints, provenance, failure code,
   `OCR_REQUIRED`, extraction event types, and migration.
3. Run model tests and migration drift check.

## Task 3: Asynchronous upload dispatch

1. Write failing tests proving PDF-only `transaction.on_commit` dispatch and
   no synchronous extraction.
2. Add queued state/event and a UUID-only Celery dispatch boundary.
3. Prove JPEG/PNG uploads remain unchanged and M6 rollback behavior stays green.

## Task 4: Idempotent processing task

1. Write failing tests for claim/parse/persist, canonical reuse, transient retry,
   terminal failures, missing/integrity/encryption cases, deletion recheck, and
   database persistence failure.
2. Implement short claim/persist transactions, bounded retries, safe logging,
   and controlled state/events.
3. Prove retry does not duplicate rows and success cannot be overwritten.

## Task 5: Safe API metadata

1. Write failing adult/guardian authorization and OpenAPI tests for
   detail-only `text_available`.
2. Add a detail serializer without any full-text exposure.
3. Re-run M4/M6 authorization suites and schema validation.

## Task 6: PostgreSQL concurrency and acceptance

1. Add concurrent same-document task tests proving one canonical result, unique
   pages, and no late failure overwrite.
2. Run targeted M7, full branch-aware pytest, Ruff check/format, Django checks,
   migration drift, OpenAPI validation, Docker Compose, Redis/Celery smoke, and
   PostgreSQL concurrency.
3. Run caveman-review plus architecture/security/scope review, make clean
   phase-scoped commits, report exact evidence, and stop before M8.
