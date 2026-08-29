# Phase-1 API Contract (Frozen at M17)

The `/api/v1/` surface is the stable Phase-1 integration contract for mobile and
frontend clients. This document freezes routes, auth, error, pagination,
serialization, and file contracts. After M17 the surface is backward-compatible;
future breaking changes move to `/api/v2/` or follow an explicit deprecation
period (see `docs/release/phase-1-release-candidate.md`).

Source of truth for machine-readable details is the live OpenAPI schema:

- Schema (JSON): `GET /api/v1/schema/`
- Schema (YAML): `GET /api/v1/schema/?format=yaml`
- Swagger UI: `GET /api/v1/docs/`

## Contract rules

- All product APIs live under `/api/v1/`. `/admin/` is Django admin and is not a
  public API.
- Success responses use one envelope: `{"data": <payload>}`.
- Error responses use one envelope: `{"error": {"code", "message", "details"}}`.
- The only non-enveloped endpoint is `GET /api/v1/health/` →
  `{"status": "ok"}` (deliberate liveness probe, not a product resource).
- List endpoints paginate with `{"count", "next", "previous", "results"}`
  nested inside `data`; default page size 20, `?page=` supported.
- Public resource identifiers are UUID strings. Auto-increment DB ids are never
  exposed. `digital_id` is a separate, stable, human-readable identifier.
- Dates serialize as ISO 8601 `YYYY-MM-DD`; datetimes as timezone-aware ISO 8601.
- Raw OCR/date-source strings are candidate provenance only, never shown as
  authoritative text.
- Mutation serializers reject unknown fields (`400`) instead of ignoring them.
- Private files never have public URLs, storage paths, or keys.

## Endpoint inventory (definitive, M17)

`Auth` — requires: none unless noted.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/v1/auth/register/` | public (throttled) | 201 |
| POST | `/api/v1/auth/login/` | public (throttled) | 200 access+refresh |
| POST | `/api/v1/auth/refresh/` | refresh token in body | 200 rotated pair |
| POST | `/api/v1/auth/logout/` | JWT | blacklists refresh |
| GET | `/api/v1/auth/me/` | JWT | 200 public user |
| POST | `/api/v1/auth/activate-claimed-account/` | public (throttled) | claim activation |

`Registration email verification (M31B)` — public, anonymous, throttled; capability-bound.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/register/email/start/` | account details → creates session + sends OTP; returns `session_token` once + masked email |
| POST | `/api/v1/auth/register/email/resend/` | resends OTP (core cooldown/limits) |
| POST | `/api/v1/auth/register/email/verify/` | verifies OTP; marks session `EMAIL_VERIFIED` |
| GET | `/api/v1/auth/register/email/status/` | resume; `X-Registration-Session-Token` header |
| POST | `/api/v1/auth/register/identity/extract/` | **requires** `X-Registration-Session-Token` of an `EMAIL_VERIFIED` session (403 otherwise) |
| POST | `/api/v1/auth/register/` | scan-first now also requires `registration_session` (verified capability) |

Purpose and OTP target are always chosen server-side (`EMAIL_VERIFICATION`, the
session's own email). The client can never assert verification (`verified`,
`email_verified` fields are rejected); the flag is derived from the session row.

`Patients` — requires: JWT + PATIENT role.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/patients/me/` | own profile |
| POST | `/api/v1/patients/me/` | 409 if exists |
| PATCH | `/api/v1/patients/me/` | locked identity fields when VERIFIED |

`Identity documents` — requires: JWT + owning patient (or authorized guardian).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/identity-documents/` | paginated summary |
| POST | `/api/v1/identity-documents/` | multipart, 201 |
| GET | `/api/v1/identity-documents/{document_uuid}/` | detail |
| POST | `/api/v1/identity-documents/{document_uuid}/replace/` | multipart |
| GET | `/api/v1/identity-documents/{document_uuid}/images/{side}/` | `side` ∈ {front, back}; patient/guardian/agent |

`Identity verification` — requires: JWT + IDENTITY_VERIFICATION_AGENT.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/verification/identity-documents/` | `?status=`, paginated |
| GET | `/api/v1/verification/identity-documents/{document_uuid}/` | |
| POST | `/api/v1/verification/identity-documents/{document_uuid}/approve/` | |
| POST | `/api/v1/verification/identity-documents/{document_uuid}/reject/` | reason required |

`Minors` (guardian) — requires: JWT + verified, active guardian relationship.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/minors/` | paginated |
| POST | `/api/v1/minors/` | multipart; `Idempotency-Key` header required |
| GET | `/api/v1/minors/{minor_uuid}/` | |
| GET | `/api/v1/minors/{minor_uuid}/archive/` | paginated |
| GET | `/api/v1/minors/{minor_uuid}/archive/summary/` | |
| GET | `/api/v1/minors/{minor_uuid}/search/` | paginated |
| GET | `/api/v1/minors/{minor_uuid}/documents/` | paginated |
| POST | `/api/v1/minors/{minor_uuid}/documents/` | multipart |
| GET | `/api/v1/minors/{minor_uuid}/documents/{document_uuid}/` | |
| PATCH | `/api/v1/minors/{minor_uuid}/documents/{document_uuid}/` | metadata only |
| DELETE | `/api/v1/minors/{minor_uuid}/documents/{document_uuid}/` | 204 soft delete |
| GET | `/api/v1/minors/{minor_uuid}/documents/{document_uuid}/file/` | octet-stream |
| GET | `/api/v1/minors/{minor_uuid}/documents/{document_uuid}/date-candidates/` | paginated |
| POST | `/api/v1/minors/{minor_uuid}/documents/{document_uuid}/confirm-date/` | |

`Guardian relationship verification` — requires: JWT + agent.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/verification/guardian-relationships/` | `?status=`, paginated |
| GET | `/api/v1/verification/guardian-relationships/{relationship_uuid}/` | |
| POST | `/api/v1/verification/guardian-relationships/{relationship_uuid}/approve/` | |
| POST | `/api/v1/verification/guardian-relationships/{relationship_uuid}/reject/` | |
| GET | `/api/v1/verification/guardian-relationships/{relationship_uuid}/evidence/{evidence_uuid}/file/` | image |

`Account claims` — public submission + agent review.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/v1/account-claims/` | public (throttled) | multipart, 202 |
| GET | `/api/v1/verification/account-claims/` | agent | `?status=`, paginated |
| GET | `/api/v1/verification/account-claims/{claim_uuid}/` | agent | |
| POST | `/api/v1/verification/account-claims/{claim_uuid}/approve/` | agent | |
| POST | `/api/v1/verification/account-claims/{claim_uuid}/reject/` | agent | reason required |
| POST | `/api/v1/verification/account-claims/{claim_uuid}/request-more-information/` | agent | reason required |
| GET | `/api/v1/verification/account-claims/{claim_uuid}/evidence/{evidence_uuid}/images/{side}/` | agent | `side` ∈ {front, back} |

`Medical documents` — requires: JWT + owning patient.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/documents/` | paginated, active only |
| POST | `/api/v1/documents/` | multipart, throttled, 201 |
| GET | `/api/v1/documents/{document_uuid}/` | detail (+ `text_available`) |
| PATCH | `/api/v1/documents/{document_uuid}/` | metadata only, never date authority |
| DELETE | `/api/v1/documents/{document_uuid}/` | 204 soft delete |
| GET | `/api/v1/documents/{document_uuid}/file/` | octet-stream |
| GET | `/api/v1/documents/{document_uuid}/date-candidates/` | paginated |
| POST | `/api/v1/documents/{document_uuid}/confirm-date/` | `candidate_id` XOR `date` |

`Facilities` — requires: JWT.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/facilities/` | `?active=&country=&region=&city=&type=`, paginated |
| GET | `/api/v1/facilities/{facility_uuid}/` | |

`Archive` — requires: JWT + owning patient (guardian for minor routes).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/archive/` | verified chronology default, `?date_status=`, paginated |
| GET | `/api/v1/archive/summary/` | |

`Search` — requires: JWT + owning patient. Lexical `simple` PostgreSQL search.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/search/` | `?q=` (≤ 200 chars) + filters, throttled, paginated |

`Health` — public.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/health/` | `{"status": "ok"}` |

## Throttling inventory

Rates are environment-configurable via `DEFAULT_THROTTLE_RATES` in
`config/settings/base.py` (read from `DJANGO_THROTTLE_*`-style env where wired).

| Scope | Endpoint(s) | Default rate |
|---|---|---|
| `auth_register` | POST `/api/v1/auth/register/` | 5/hour |
| `auth_login` | POST `/api/v1/auth/login/` | 10/minute |
| `account_claim_submit` | POST `/api/v1/account-claims/` | 5/hour |
| `account_claim_activation` | POST `/api/v1/auth/activate-claimed-account/` | 10/hour |
| `medical_document_upload` | POST `/api/v1/documents/` (and minor upload) | 20/hour |
| `medical_search` | GET `/api/v1/search/`, minor search | 600/minute |

No global default throttle; sensitive public endpoints are throttled explicitly.
Rate-limit responses use the standard error envelope with code `throttled`.

## Authentication contract

- Register: `email`, `password`, optional `phone`, nested `patient` profile.
  Creates an ACTIVE PATIENT user with a PatientProfile. 201 with public user.
- Login: `email` + `password`. 200 with `access` (5 min) + `refresh` (1 day).
  Generic `invalid_credentials` for bad credentials; `account_unavailable` for
  inactive/pending accounts. No endpoint reveals which field was wrong.
- Refresh: `refresh` token in body. Rotates (ROTATE_REFRESH_TOKENS) and
  blacklists (BLACKLIST_AFTER_ROTATION) the presented token. Presenting an
  expired access token in the `Authorization` header alongside a valid refresh
  returns 401; clients should refresh without a stale access header.
- Logout: `refresh` in body; blacklists it. 200.
- `/auth/me/`: current active user.
- Claim activation: `POST /auth/activate-claimed-account/` with activation code
  (short-lived, single-use, stored as SHA-256 hash).
- No endpoint ever returns passwords, hashes, or token internals.

## Standard error envelope

```json
{
  "error": {
    "code": "stable_code",
    "message": "safe message",
    "details": {}
  }
}
```

`details` carries field-level validation errors (keyed by field). Stable codes
in use include:

- `validation_error` — field validation failures (400)
- `not_authenticated` — missing/invalid token (401)
- `authentication_failed` / `invalid_credentials` / `account_unavailable` (401)
- `method_not_allowed` (405)
- `throttled` (429)
- domain codes: `duplicate_document`, `medical_file_unavailable`,
  `date_candidate_stale`, `date_candidate_not_found`,
  `invalid_date_confirmation_state`, `healthcare_facility_inactive`,
  `guardian_relationship_not_found`, `identity_document_not_found`,
  `account_claim_not_found`, and other stable `snake_case` domain codes.

Python exception names and tracebacks are never serialized.

## 401 / 403 / 404 policy

- `401` — unauthenticated (missing/expired/invalid token, inactive account).
- `403` — authenticated but wrong role where concealment is not needed (e.g.
  patient hitting an agent-only queue; non-verified guardian).
- `404` — IDOR-sensitive resource unavailable/not authorized. A patient
  requesting another patient's document UUID receives 404, never existence
  leakage. This is deliberate: do not "simplify" secure 404s into 403s.

## Pagination contract

All list endpoints return `{"count", "next", "previous", "results"}` inside
`data`, default page size 20, `?page=N`. The archive list additionally returns
`unconfirmed_date_count`. Ordering is deterministic on every paginated endpoint
(no DB-natural order); archive and search document their ordering in
`docs/architecture/0013-m12-archive-query-and-indexes.md` and
`docs/architecture/0014-m13-search.md`.

## File download contract

- Identity/evidence images: `Content-Type: image/jpeg|png`,
  `Content-Disposition: attachment` with a fixed safe filename
  (`identity-{side}`, `guardian-evidence`, `claim-evidence-{side}`).
- Medical files: `Content-Type` from stored MIME, `attachment` with the
  sanitized original filename. Filenames are stored as basename with
  `\ / \r \n \0` stripped; no filesystem paths are ever returned.
- `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`.
- `VALID` → stream. `CORRUPTED`/`QUARANTINED`/`MISSING` → `medical_file_unavailable`.
- Deleted (soft) documents → 404. Unauthorized → 404 (IDOR policy).

## OpenAPI

- 48 unique paths, 58 operations (57 product + health), 136 components.
- Every operation documents request body, query params, responses, and the
  standard error responses. Image `side` path params are constrained to
  `front|back`. Schema validates with `drf-spectacular.validation.validate_schema`.
- No unstable generated names; enums are overridden to stable names.
- Runtime-vs-OpenAPI drift is covered by `tests/test_openapi_runtime_drift.py`.

## Backward compatibility

M17 introduces no response-field removal or rename. Changes are additive and
documentation-level only. Existing M0–M16 clients remain compatible.
