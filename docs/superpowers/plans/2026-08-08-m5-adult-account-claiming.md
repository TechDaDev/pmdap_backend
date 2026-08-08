# M5 Adult Patient Account Claiming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Link one activation-required PATIENT account to an existing adult
PatientProfile without changing lifelong identity or history.

**Architecture:** The claims domain owns the public privacy contract, evidence,
review, ownership transaction, activation, and events. It reuses M1 account
enforcement, M2 age/profile invariants, M3 private IdentityFile mechanics, and
M4 guardian history. Public eligibility failures become indistinguishable decoy
receipts.

**Tech Stack:** Python 3.12, Django 5.2, DRF, PostgreSQL 17, Redis throttling,
SimpleJWT, drf-spectacular, pytest.

## Global constraints

- Preserve every M0-M4 invariant and test.
- Never create, copy, replace, or renumber the existing PatientProfile.
- Never reveal whether a Digital ID, email, identity, or ownership record exists.
- Do not implement reports, archives, OCR, facilities, doctors, or AI.
- Use strict test-driven development: observe RED before each production change.
- Prove the four required races against PostgreSQL, not SQLite.

## Task 1: Persist the claim contract

**Files:** `claims/models.py`, `claims/migrations/0001_initial.py`,
`accounts/models.py`, `accounts/migrations/0003_*.py`,
`tests/test_account_claim_models.py`.

- [ ] Add failing tests for choices, constraints, immutable events, token hashes,
  active-claim uniqueness, and `PENDING_ACTIVATION` login rejection.
- [ ] Run the targeted tests and record RED.
- [ ] Add `PatientAccountClaim`, `ClaimIdentityEvidence`, `AccountActivation`, and
  `PatientAccountClaimEvent` with controlled status/comparison/event choices.
- [ ] Add partial uniqueness for active patient claims and case-insensitive active
  requested-email claims.
- [ ] Extend account status storage for `PENDING_ACTIVATION`.
- [ ] Generate migrations, rerun the targeted tests, and record GREEN.

## Task 2: Build privacy-preserving submission and evidence

**Files:** `claims/serializers.py`, `claims/services/submission.py`,
`claims/exceptions.py`, `tests/test_account_claim_submission.py`.

- [ ] Add failing tests for required fields, strict unknown-field rejection,
  National Card requirement, optional Passport, image validation, cleanup, and
  every privacy-equivalent ineligible condition.
- [ ] Run targeted tests and record RED.
- [ ] Implement `submit_account_claim(validated_data) -> ClaimReceipt`.
- [ ] Return the same `202` receipt shape for eligible and ineligible requests;
  persist only eligible requests and use a random decoy UUID otherwise.
- [ ] Store evidence privately and compute review-only
  `MATCH`/`MISMATCH`/`UNAVAILABLE` comparisons against current M3 truth.
- [ ] Clean persisted files on every database/storage failure.
- [ ] Rerun targeted tests and record GREEN.

## Task 3: Expose and throttle public submission

**Files:** `claims/api.py`, `claims/urls.py`, `claims/throttles.py`,
`config/urls.py`, `config/settings/base.py`,
`tests/test_account_claim_api.py`.

- [ ] Add failing API tests for multipart success, indistinguishable responses,
  throttling, no authentication requirement, method restrictions, and OpenAPI.
- [ ] Run targeted tests and record RED.
- [ ] Implement `POST /api/v1/account-claims/` with `AllowAny`, bounded multipart
  uploads, the common envelope, and scope `account_claim_submit`.
- [ ] Rerun targeted tests and record GREEN.

## Task 4: Implement exact-role review workflow

**Files:** `claims/services/review.py`, `claims/serializers.py`, `claims/api.py`,
`claims/verification_urls.py`, `tests/test_account_claim_review.py`.

- [ ] Add failing tests for exact-role queue/detail/evidence access, pagination,
  status filtering, transitions, reason validation, events, and 405 responses.
- [ ] Run targeted tests and record RED.
- [ ] Implement queue, detail, private evidence streaming, reject, and
  request-more-information endpoints.
- [ ] Prevent public status/detail/update access and cross-role access.
- [ ] Rerun targeted tests and record GREEN.

## Task 5: Implement the atomic ownership transaction

**Files:** `claims/services/review.py`, `claims/api.py`,
`tests/test_account_claim_approval.py`.

- [ ] Add failing tests for every approval precondition, stable replay conflict,
  rollback injection, identity continuity, and guardian history normalization.
- [ ] Run targeted tests and record RED.
- [ ] Implement `approve_account_claim(...) -> ApprovalResult` using one
  `transaction.atomic()` block and `select_for_update()` on claim and profile.
- [ ] Revalidate adult age, ownership, email uniqueness, verified profile, and
  current verified National Card inside the lock.
- [ ] Create exactly one PATIENT account with unusable password and
  `PENDING_ACTIVATION`, link the same profile, close active guardian relations,
  create review/guardian/activation events, and return the raw token once.
- [ ] Prove injected failure rolls back account, link, guardian changes, claim,
  activation, and events.
- [ ] Rerun targeted tests and record GREEN.

## Task 6: Implement one-time activation

**Files:** `claims/services/activation.py`, `claims/serializers.py`,
`claims/api.py`, `accounts/urls.py`, `tests/test_claim_activation.py`.

- [ ] Add failing tests for valid, invalid, expired, reused, concurrent, and weak
  password activation plus pre/post-login behavior.
- [ ] Run targeted tests and record RED.
- [ ] Implement `activate_claimed_account(token, new_password) -> User` with
  SHA-256 lookup, row locking, expiry/single-use enforcement, standard password
  validation, password set, and status transition to `ACTIVE`.
- [ ] Expose `POST /api/v1/auth/activate-claimed-account/` with generic token
  failures and scope `account_claim_activation`.
- [ ] Rerun targeted tests and record GREEN.

## Task 7: Lock the contract and journey

**Files:** `tests/test_m5_journey.py`, `tests/test_openapi.py`, `README.md`,
`docs/superpowers/specs/2026-08-08-m5-adult-account-claiming-design.md`.

- [ ] Add the complete age-18 claim-to-login journey test.
- [ ] Assert exact OpenAPI request/response schemas, authentication, and status
  codes for every M5 endpoint.
- [ ] Document the M5 endpoints, lifecycle, privacy posture, and operational token
  delivery boundary without exposing implementation secrets.
- [ ] Run journey and OpenAPI tests to GREEN.

## Task 8: Prove PostgreSQL concurrency

**Files:** `tests/test_account_claim_concurrency.py`.

- [ ] Add transactional barriers for simultaneous same-claim approval, duplicate
  active submissions, ownership race, and activation double-consumption.
- [ ] Run all four on PostgreSQL and prove exactly one valid terminal outcome.
- [ ] Confirm no duplicate accounts, ownership links, active claims, or token use.

## Task 9: Acceptance, review, and commits

- [ ] Run targeted M5 tests and the full pytest suite.
- [ ] Run branch-aware coverage and confirm configured threshold.
- [ ] Run Ruff, Django system checks, migration drift, and OpenAPI validation.
- [ ] Run Caveman review; resolve all HIGH/MEDIUM findings or report blockers.
- [ ] Inspect migration reversibility and final worktree scope.
- [ ] Make clean phase-scoped Caveman commits and report exact evidence.
- [ ] Stop before M6 and wait for explicit approval.
