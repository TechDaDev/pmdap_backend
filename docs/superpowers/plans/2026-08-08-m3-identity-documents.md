# M3 Identity Documents Implementation Plan

## Task 1: Contract and RED tests

- [ ] Add model, file-validation, isolation, authorization, verification,
  replacement, transaction, event, and OpenAPI tests.
- [ ] Run focused tests and capture expected missing-feature failures.

## Task 2: Models, storage, and services

- [ ] Add identity file, document, and immutable event models.
- [ ] Add private storage plus content/size/hash validation.
- [ ] Add transactional workflows, locking, state rules, and DB constraints.
- [ ] Generate and inspect migration.

## Task 3: APIs

- [ ] Add patient-owned create/list/detail/replace/image endpoints.
- [ ] Add role-scoped verification queue/detail/approve/reject endpoints.
- [ ] Preserve envelopes, pagination, safe projections, errors, and OpenAPI.

## Task 4: Acceptance

- [ ] Run targeted M3 and full M0-M3 tests with branch coverage at least 90%.
- [ ] Run Ruff, Django checks, migration drift, OpenAPI, and Compose validation.
- [ ] Review security, privacy, transitions, races, scope, and migration safety;
  commit clean phase changes; stop before M4.
