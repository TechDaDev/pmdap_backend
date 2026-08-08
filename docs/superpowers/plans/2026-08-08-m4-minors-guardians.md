# M4 Minors and Guardian Relationships Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Build independent minor identities and manually verified, temporary
guardian authority with private evidence and live age-18 enforcement.

**Architecture:** Guardians domain owns relationships, evidence, events,
idempotency, and minor APIs. It reuses M2 PatientProfile creation and M3 identity
storage/workflows. A narrow guardian authorization policy extends only M3
document detail, image, and replacement operations.

**Tech Stack:** Python 3.12, Django 5.2, DRF, PostgreSQL, SimpleJWT,
drf-spectacular, pytest, Docker Compose.

## Global constraints

- Preserve all M0-M3 behavior and tests.
- Minor PatientProfile has `user = NULL`; no minor login account.
- No M5 adult claiming or M6+ medical/OCR/archive work.
- Use shared success/error envelopes and private identity storage.
- Overall branch-aware coverage remains at least 90%.
- PostgreSQL concurrency tests must execute, not merely exist.

---

### Task 1: Guardian data contract

**Files:**
- Create: `guardians/models.py`
- Create: `guardians/migrations/0001_initial.py`
- Test: `tests/test_minors_guardians.py`

**Interfaces:**
- Produces: `GuardianRelationship`, `GuardianEvidence`,
  `GuardianRelationshipEvent`, `MinorCreationRequest`.
- Constraints: one active guardian/minor/relationship tuple and one creation key
  per guardian.

- [x] Write model tests for controlled choices, protected history, immutable
  event records, relationships without minor users, and both uniqueness rules.
- [x] Run `pytest tests/test_minors_guardians.py -q --no-cov -x`; expect missing
  guardian models.
- [x] Add minimal models and generate migration with
  `python manage.py makemigrations guardians`.
- [x] Run model tests until green; inspect migration constraints with
  `python manage.py sqlmigrate guardians 0001 --settings=config.settings.test`.

### Task 2: Guardian eligibility and live authority

**Files:**
- Create: `guardians/exceptions.py`
- Create: `guardians/policies.py`
- Test: `tests/test_minors_guardians.py`

**Interfaces:**
- Produces: `eligible_guardian_profile(user, *, for_update=False) -> PatientProfile`.
- Produces: `guardian_relationship(user, minor, *, require_verified=True)`.
- Produces: `guardian_can_manage_minor(user, minor) -> bool`.

- [x] Write RED tests for role, account state, owned adult verified profile,
  verified-current National Card, pending/rejected states, and exact birthday.
- [x] Implement policy queries with current-state checks and optional row locks.
- [x] Verify policy tests, including no access from UUID, Digital ID, or family
  number alone.

### Task 3: Transactional idempotent minor creation

**Files:**
- Create: `guardians/services.py`
- Modify: `identities/services.py`
- Test: `tests/test_minors_guardians.py`

**Interfaces:**
- Produces: `create_minor(*, guardian, idempotency_key, profile_data,
  identity_data, relationship_type, evidence_data=None) -> MinorCreationResult`.
- Reuses: `patients.services.create_patient_profile` and
  `identities.services.submit_identity_document`.
- Produces storage-reference cleanup helpers for outer transaction rollback.

- [x] Write RED tests for valid National Card/Birth Document creation, minor age
  boundaries, one Digital ID, atomic rollback, storage cleanup, primary-document
  allowlist, family result, and idempotency replay/conflict.
- [x] Implement canonical fingerprint using normalized fields and validated file
  SHA-256 values; require 1-128 character `Idempotency-Key`.
- [x] Implement one outer transaction and explicit blob cleanup on failure.
- [x] Verify zero orphan profiles/documents/relationships/files after injected
  failure and one result after replay.

### Task 4: Minor identity verification and M3 authorization reuse

**Files:**
- Modify: `identities/services.py`
- Modify: `identities/api.py`
- Create: `guardians/identity_access.py`
- Test: `tests/test_minors_guardians.py`

**Interfaces:**
- Extends profile-state calculation: verified current Birth Document verifies a
  minor but never an adult.
- Produces: `guardian_document_access(user, document) -> bool`.

- [x] Write RED tests for Birth Document verification, adult passport/card rules
  unchanged, guardian document detail/image/replacement, pending/rejected/
  unrelated/adult-boundary denial, actor event, and replacement continuity.
- [x] Extend central M3 state calculation using PatientProfile `is_minor`.
- [x] Extend only M3 detail/image/replace object authorization; preserve direct
  owner collection behavior.
- [x] Run all M3 plus M4 identity tests until green.

### Task 5: Minor and relationship verification APIs

**Files:**
- Create: `guardians/serializers.py`
- Create: `guardians/api.py`
- Create: `guardians/urls.py`
- Modify: `config/urls.py`
- Test: `tests/test_minors_guardians.py`
- Test: `tests/test_guardian_openapi.py`

**Interfaces:**
- Adds `/api/v1/minors/` collection/detail routes.
- Adds `/api/v1/verification/guardian-relationships/` queue/detail/decision and
  private evidence-stream routes.
- Reuses shared envelopes and M3 multipart/file schemas.

- [x] Write RED API tests covering stable errors, mass assignment, pagination,
  IDOR, pending restrictions, queue projections, evidence access, decisions,
  unsupported methods, and exact response fields.
- [x] Implement serializers with unknown-field rejection and distinct summary,
  guardian detail, agent queue, and agent evidence views.
- [x] Implement service-controlled decisions and explicit OpenAPI operations.
- [x] Run M4 API/OpenAPI tests and `manage.py spectacular --validate` until clean.

### Task 6: Multiple guardians and relationship lifecycle

**Files:**
- Modify: `guardians/services.py`
- Test: `tests/test_minors_guardians.py`

**Interfaces:**
- Produces: `create_guardian_relationship(...)` internal service for independent
  relationships without exposing a broad staff CRUD API.
- Approval/rejection operate on locked records and preserve history.

- [x] Write RED tests for independent father/mother review, duplicate blocking,
  rejection isolation, private-account non-disclosure, guardian suspension, and
  approval after adulthood.
- [x] Implement minimal internal relationship service and deterministic decision
  conflicts.
- [x] Verify relationship and authorization branches are effectively complete.

### Task 7: PostgreSQL concurrency lane

**Files:**
- Create: `config/settings/postgres_test.py`
- Create: `tests/test_postgresql_concurrency.py`
- Modify: `pytest.ini`

**Interfaces:**
- Marker: `postgresql`; tests skip unless `connection.vendor == "postgresql"`.
- Uses separate thread-local Django DB connections.

- [x] Write three concurrency tests for M3 replacement approval, guardian
  approval, and idempotent duplicate creation.
- [x] Run SQLite collection and verify PostgreSQL tests skip explicitly.
- [x] Start an isolated PostgreSQL 17 container with test-only credentials and run
  marked tests from the host using `config.settings.postgres_test`.
- [x] Record exact executed pass/fail/skip counts; never infer concurrency success.

### Task 8: Acceptance and commits

**Files:**
- Modify: `README.md`
- Modify: this plan checklist after evidence exists.

- [x] Run targeted M4 tests, full branch-aware pytest, Ruff check/format, Django
  checks, migration drift, OpenAPI validation, Compose validation, and actual
  PostgreSQL concurrency tests.
- [x] Review authorization, age boundary, transaction rollback, storage cleanup,
  PII projections, migration constraints, and M5+ scope exclusion.
- [x] Create clean conventional M4 commits, verify clean worktree and Git state,
  report required evidence, and stop before M5.
