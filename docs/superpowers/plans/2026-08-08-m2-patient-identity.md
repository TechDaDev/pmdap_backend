# M2 Patient Identity Implementation Plan

## Task 1: Contract and RED tests

- [ ] Define registration, legacy completion, retrieve, update, Digital ID,
  enum, DOB, isolation, immutability, and OpenAPI tests.
- [ ] Run focused tests and capture expected missing-feature failures.

## Task 2: Model and services

- [ ] Add PatientProfile using UUIDModel and nullable one-to-one ownership.
- [ ] Add controlled enums, DOB validation, dynamic age/is_minor, immutable IDs.
- [ ] Add secure Digital ID generation and collision-safe creation service.
- [ ] Create and inspect migration without fabricated legacy data.

## Task 3: Registration and API

- [ ] Extend PATIENT registration with nested patient input and one transaction.
- [ ] Add legacy completion plus own-profile GET/PATCH only.
- [ ] Preserve shared envelopes, mass-assignment rejection, and OpenAPI schemas.

## Task 4: Acceptance

- [ ] Run focused M2 and full M0-M2 tests with branch coverage.
- [ ] Run Ruff, Django checks, migration drift, OpenAPI, and Compose validation.
- [ ] Review security/scope, commit phase-scoped changes, verify clean tree, and
  stop before M3.
