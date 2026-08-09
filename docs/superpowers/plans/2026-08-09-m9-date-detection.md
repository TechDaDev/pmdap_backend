# M9 Multilingual Date Detection Implementation Plan

**Goal:** Persist deterministic advisory date candidates and stop before M10.

## Task 1: Deterministic text/date contract

1. Write failing normalization, raw-index provenance, format, leap-year,
   impossible-date, ambiguity, multilingual label, scoring, threshold, tie, and
   future-tolerance tests.
2. Add immutable normalized/matched candidate contracts, controlled dictionaries,
   explicit parsers, bounded contexts, and named scoring policy.
3. Run focused pure-service tests.

## Task 2: Candidate schema and state machine

1. Write failing model, constraint, versioning, manual-date precedence, state,
   event, no-date, idempotency, deletion, and stale-failure tests.
2. Add `DateCandidate`, database constraints/indexes, `DATE_PROCESSING` and
   `DATE_NOT_FOUND`, immutable events, transactional claim/replace persistence,
   and migration.
3. Apply M9 forward migration to persistent M8 database and verify drift.

## Task 3: Dispatch and API

1. Write failing native/OCR after-commit dispatch and Celery retry tests.
2. Schedule M9 from both successful M7 and M8 persistence paths.
3. Add read-only paginated adult/minor candidate routes with shared live medical
   authorization, bounded serializer, stable errors, and exact OpenAPI schemas.

## Task 4: Security and concurrency

1. Test adult owner, verified live guardian, unrelated/pending/rejected/age-18
   guardian, verification-agent, and unauthenticated access.
2. Test bounded context, response allowlist, control characters, no sensitive
   logging/events, and no verified-date mutation.
3. Run real PostgreSQL duplicate-worker, unique-suggestion, stale-failure, and
   deletion races.

## Task 5: Runtime acceptance

1. Run persistent PostgreSQL integration and forward-migration evidence.
2. Run fresh PostgreSQL migration from zero.
3. Run real Redis/PostgreSQL/Celery native-PDF and OCR-image flows.
4. Run targeted/full branch coverage, Ruff, formatting, Django, migration drift,
   OpenAPI, Compose, Docker build, and dependency gates.
5. Review and commit phase-scoped slices; stop before M10.
