# M2 Patient Identity Implementation Plan

## Task 1: Contract and RED tests

- [x] Define registration, legacy completion, retrieve, update, Digital ID,
  enum, DOB, isolation, immutability, and OpenAPI tests.
- [x] Run focused tests and capture expected missing-feature failures.

## Task 2: Model and services

- [x] Add PatientProfile using UUIDModel and nullable one-to-one ownership.
- [x] Add controlled enums, DOB validation, dynamic age/is_minor, immutable IDs.
- [x] Add secure Digital ID generation and collision-safe creation service.
- [x] Create and inspect migration without fabricated legacy data.

## Task 3: Registration and API

- [x] Extend PATIENT registration with nested patient input and one transaction.
- [x] Add legacy completion plus own-profile GET/PATCH only.
- [x] Preserve shared envelopes, mass-assignment rejection, and OpenAPI schemas.

## Task 4: Acceptance

- [x] Run focused M2 and full M0-M2 tests with branch coverage.
- [x] Run Ruff, Django checks, migration drift, OpenAPI, and Compose validation.
- [x] Review security/scope, commit phase-scoped changes, verify clean tree, and
  stop before M3.
