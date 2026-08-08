# M6 Medical File Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add secure private medical-document uploads for adult owners and verified guardians without entering M7 processing.

**Architecture:** Extend the existing `documents` module with separate stored-file evidence, patient document metadata, immutable events, a private storage adapter, upload/integrity services, and adult/minor API adapters. Reuse existing patient ownership, guardian authority, error, permission, and throttling boundaries.

**Tech Stack:** Django, Django REST Framework, PostgreSQL, Pillow, a structural PDF parser, drf-spectacular, pytest, Ruff.

---

### Task 1: Persistence invariants

**Files:** `documents/models.py`, `documents/migrations/`, `tests/test_medical_document_models.py`

1. Write failing model tests for file evidence, controlled values, relationships,
   ordering, active patient-scoped duplicate uniqueness, and immutable events.
2. Implement the smallest models and migration.
3. Run targeted tests and migration drift checks.

### Task 2: Private storage and validation

**Files:** `documents/storage.py`, `documents/validation.py`, `documents/scanning.py`, settings/requirements, `tests/test_medical_file_validation.py`

1. Write failing tests for PDF/JPEG/PNG, malformed/spoofed/encrypted inputs,
   byte/dimension limits, filename sanitization, page count, and exact bytes.
2. Add dedicated no-URL storage and configuration.
3. Add bounded structural validation and the `NOT_CONFIGURED` scanner.
4. Run the focused validation suite.

### Task 3: Atomic upload and integrity services

**Files:** `documents/services.py`, `tests/test_medical_document_services.py`

1. Write failing tests for successful persistence, duplicate conflicts, blob
   compensation, immutable evidence, integrity success, and tamper detection.
2. Implement transaction orchestration, randomized keys, stable domain errors,
   compensation, lifecycle events, and internal re-hashing.
3. Run focused service tests.

### Task 4: Adult API

**Files:** `documents/api.py`, `documents/serializers.py`, `documents/urls.py`, project URLs, `tests/test_medical_documents_api.py`

1. Write failing tests for adult create/list/detail/download/PATCH/DELETE,
   authentication, cross-patient isolation, protected input, and methods.
2. Implement owner-derived lookups, multipart create, safe output, metadata-only
   mutation, streaming response, and soft deletion.
3. Run focused adult API tests.

### Task 5: Guardian API

**Files:** document API/service modules, `tests/test_minor_medical_documents_api.py`

1. Write failing tests for current verified guardian access plus unverified,
   unrelated, revoked, expired, inactive, and age-18 denial.
2. Implement nested minor adapters using the existing M4 authority service.
3. Prove guardian and adult routes share document behavior without widening
   authority.

### Task 6: Security and API contract

**Files:** throttling/settings/schema modules, `tests/test_medical_document_security.py`, `tests/test_medical_document_openapi.py`

1. Test throttling, error envelopes, filename/header safety, no digest/path/key
   leakage, no deleted access, and actual OpenAPI request/response shapes.
2. Add endpoint schema declarations and operation descriptions.
3. Run schema generation and focused security tests.

### Task 7: PostgreSQL concurrency

**Files:** `tests/test_medical_document_concurrency.py`

1. Create parallel same-patient and cross-patient upload tests against PostgreSQL.
2. Prove one same-patient winner, stable loser conflict, and no orphan loser blob.
3. Prove identical bytes remain valid for different patients.

### Task 8: Operational configuration and acceptance

**Files:** Docker/settings/env documentation and all changed M6 files

1. Add the persistent private medical-storage volume and documented limits.
2. Run targeted M6 tests, the full suite with branch coverage, Ruff, Django
   checks, migration drift, OpenAPI validation, and PostgreSQL concurrency tests.
3. Review authorization, upload abuse, response leakage, transaction cleanup,
   and scope exclusions.
4. Commit clean phase-scoped changes and stop before M7.
