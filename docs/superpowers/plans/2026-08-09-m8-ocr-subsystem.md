# M8 OCR Subsystem Implementation Plan

**Goal:** Add bounded native-first OCR and stop before M9.

## Task 1: Engine and derivative contract

1. Write failing adapter, Unicode, confidence, malformed-output, preprocessing,
   pixel/dimension/bomb/text-limit, and PDF-render tests.
2. Add typed OCR results/errors, Paddle adapter, conservative image preparation,
   and page-at-a-time renderer.
3. Run focused unit tests.

## Task 2: Derived-text schema

1. Write failing model/canonicalization/provenance tests.
2. Add native/OCR/effective page fields, OCR provenance, extraction-method
   choices, state/events, and data-preserving migration.
3. Run model tests and migration drift.

## Task 3: Image and PDF orchestration

1. Write failing JPEG/PNG, image-only PDF, mixed PDF, dispatch, idempotency,
   deletion, stale snapshot, and failure/retry tests.
2. Add after-commit dispatch, authoritative claim/read/revalidate/persist flow,
   page-only rendering, aggregate rebuild, safe events/logs, and Celery task.
3. Preserve M7 direct extraction and all earlier authorization/storage behavior.

## Task 4: Runtime packaging

1. Add pinned OCR requirements and separate CPU OCR worker image stage.
2. Add build-time model preload/cache and explicit configuration/resource limits.
3. Validate Compose and run real PaddleOCR synthetic smoke.

## Task 5: Acceptance

1. Run targeted M8 and full M0-M8 branch-aware suite.
2. Run PostgreSQL races, real Redis/Celery/PostgreSQL image/PDF flows, Ruff,
   Django checks, migration drift, OpenAPI, dependency, and Compose gates.
3. Review architecture/security/scope, commit docs/runtime/tests separately, and
   stop before M9.
