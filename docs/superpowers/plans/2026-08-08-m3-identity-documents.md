# M3 Identity Documents Implementation Plan

## Task 1: Contract and RED tests

- [x] Add model, file-validation, isolation, authorization, verification,
  replacement, transaction, event, and OpenAPI tests.
- [x] Run focused tests and capture expected missing-feature failures.

## Task 2: Models, storage, and services

- [x] Add identity file, document, and immutable event models.
- [x] Add private storage plus content/size/hash validation.
- [x] Add transactional workflows, locking, state rules, and DB constraints.
- [x] Generate and inspect migration.

## Task 3: APIs

- [x] Add patient-owned create/list/detail/replace/image endpoints.
- [x] Add role-scoped verification queue/detail/approve/reject endpoints.
- [x] Preserve envelopes, pagination, safe projections, errors, and OpenAPI.

## Task 4: Acceptance

- [x] Run targeted M3 and full M0-M3 tests with branch coverage at least 90%.
- [x] Run Ruff, Django checks, migration drift, OpenAPI, and Compose validation.
- [x] Review security, privacy, transitions, races, scope, and migration safety.
- [x] Commit clean phase changes and stop before M4.
