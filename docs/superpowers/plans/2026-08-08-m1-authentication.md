# M1 Authentication Implementation Plan

**Goal:** Implement the approved M1 authentication contract without crossing
into patient identity or document workflows.

## Task 1: Contract and dependencies

- [x] Pin SimpleJWT and enable its blacklist application.
- [x] Configure JWT lifetimes, rotation, revocation, authentication, throttles,
  and the shared exception handler.
- [x] Record the endpoint, schema, state, and error contracts.

## Task 2: Executable acceptance tests

- [x] Add registration validation, normalization, privilege-injection, password,
  serialization, and throttling tests.
- [x] Add login enumeration resistance, state enforcement, and throttling tests.
- [x] Add access, refresh, rotation, blacklist, logout, token-type, expiry, and
  `/auth/me/` tests.
- [x] Add method and exact OpenAPI request/response contract tests.
- [x] Run the focused suite and retain the expected failing baseline.

## Task 3: Account and API implementation

- [x] Finalize email normalization and case-insensitive uniqueness.
- [x] Implement services, serializers, active-account JWT authentication,
  throttles, views, and routes within `accounts`.
- [x] Implement the common error envelope.
- [x] Generate and inspect migrations.

## Task 4: Acceptance and review

- [x] Run targeted M1 tests.
- [x] Run full pytest with branch coverage.
- [x] Run Ruff, Django checks, and migration drift checks.
- [x] Review scope/security/API schema, commit cleanly, verify clean worktree,
  report exact evidence, and stop before M2.
