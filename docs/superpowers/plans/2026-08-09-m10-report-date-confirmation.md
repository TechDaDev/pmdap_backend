# M10 Report Date Confirmation Implementation Plan

**Goal:** Convert M9 advice into a user-authoritative, audited report date and stop
before M11.

## Tasks

1. Add retained/current candidate generations and immutable date-decision history.
2. Add one transactional date-decision service with document-first row locking,
   candidate revalidation, replay idempotency, and verified-date precedence.
3. Add adult and minor `confirm-date` POST routes with XOR input, bounded output,
   live authorization, stable errors, and exact OpenAPI contracts.
4. Cover manual/candidate decisions, stale and cross-document candidates,
   corrections, privacy, age-out, failures, PostgreSQL races, and M9 regressions.
5. Run persistent/fresh migrations, real PDF/OCR-to-M10 flows, full branch coverage,
   static/system/schema/Compose/image/dependency gates; stop before M11.
