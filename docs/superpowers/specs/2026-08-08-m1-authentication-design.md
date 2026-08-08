# M1 Authentication Design

## Status

Approved for implementation by explicit M1 acceptance dated 2026-08-08.

## Objective

Deliver a secure email/password authentication boundary for adult account
registration. M1 creates only `accounts.User`; patient identity, Digital ID,
identity documents, minors, guardians, medical archives, uploads, and OCR remain
outside this phase.

## API contract

All routes use `/api/v1/auth/` and JSON envelopes.

- `POST register/` creates an active PATIENT account and returns its public user
  representation. Accepted inputs: `email`, optional `phone`, and `password`.
- `POST login/` returns access and refresh JWTs after generic credential and
  account-state validation.
- `POST refresh/` rotates a refresh token, blacklists the old token, and returns
  a new access/refresh pair.
- `POST logout/` blacklists the supplied refresh token.
- `GET me/` returns the authenticated user's public representation.

Success envelope: `{"data": ...}`.

Error envelope:

```json
{
  "error": {
    "code": "machine_readable_code",
    "message": "Human-readable summary.",
    "details": {}
  }
}
```

Validation details are field-scoped. Authentication failures do not distinguish
unknown email, bad password, inactive state, or non-active account status.

## User finalization

- Email is stripped and case-folded before persistence.
- Database constraints preserve case-insensitive email uniqueness.
- Ordinary registration fixes role to PATIENT, status to ACTIVE, and all
  privilege/verification flags to safe defaults.
- Django password validators run before creation; passwords are hashed and never
  serialized.
- The public user schema exposes only `uuid`, `email`, `phone`, `role`,
  `email_verified`, `phone_verified`, and `created_at`.

M1 does not collect name, birth date, sex, nationality, or identity evidence;
those belong to PatientProfile and identity-document phases.

## Token and account-state design

SimpleJWT supplies signed access/refresh tokens. Refresh rotation and the token
blacklist app provide replay resistance. Logout accepts only a refresh token and
blacklists it. Access authentication and refresh both require `is_active=True`
and `status=ACTIVE`, so a status change takes effect on the next request without
waiting for token expiry.

Access tokens cannot be used as refresh tokens; refresh tokens cannot authorize
protected endpoints. Invalid, expired, wrong-type, rotated, and blacklisted
tokens use the shared error envelope.

## Abuse controls

Registration and login use separate anonymous throttle scopes. Rates are
configuration-backed and test-overridable. Throttled responses use the shared
error envelope and HTTP 429.

## Module boundaries

- `accounts/models.py` and `accounts/managers.py`: account invariants.
- `accounts/services.py`: registration and credential/state workflows.
- `accounts/serializers.py`: transport validation and public representations.
- `accounts/authentication.py`: active-account JWT enforcement.
- `accounts/throttles.py`: endpoint throttle scopes.
- `accounts/api.py` and `accounts/urls.py`: HTTP behavior and OpenAPI metadata.
- `common/exceptions.py`: cross-API error rendering.

## Verification

M1 acceptance requires targeted authentication/security/schema tests, the full
M0+M1 suite, branch coverage at or above the repository threshold, Ruff, Django
system checks, and a no-drift migration check. M1 stops after a clean,
phase-scoped commit; M2 requires explicit approval.
