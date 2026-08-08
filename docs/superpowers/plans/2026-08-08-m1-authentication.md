# M1 Authentication Implementation Plan

**Goal:** Implement the approved M1 authentication contract without crossing
into patient identity or document workflows.

## Task 1: Contract and dependencies

- [ ] Pin SimpleJWT and enable its blacklist application.
- [ ] Configure JWT lifetimes, rotation, revocation, authentication, throttles,
  and the shared exception handler.
- [ ] Record the endpoint, schema, state, and error contracts.

## Task 2: Executable acceptance tests

- [ ] Add registration validation, normalization, privilege-injection, password,
  serialization, and throttling tests.
- [ ] Add login enumeration resistance, state enforcement, and throttling tests.
- [ ] Add access, refresh, rotation, blacklist, logout, token-type, expiry, and
  `/auth/me/` tests.
- [ ] Add method and exact OpenAPI request/response contract tests.
- [ ] Run the focused suite and retain the expected failing baseline.

## Task 3: Account and API implementation

- [ ] Finalize email normalization and case-insensitive uniqueness.
- [ ] Implement services, serializers, active-account JWT authentication,
  throttles, views, and routes within `accounts`.
- [ ] Implement the common error envelope.
- [ ] Generate and inspect migrations.

## Task 4: Acceptance and review

- [ ] Run targeted M1 tests.
- [ ] Run full pytest with branch coverage.
- [ ] Run Ruff, Django checks, and migration drift checks.
- [ ] Review scope/security/API schema, commit cleanly, verify clean worktree,
  report exact evidence, and stop before M2.
