# ADR-0022: M31B — Registration Email Verification

## Status

Accepted for M31B.

## Context

M31A delivered the PostgreSQL-authoritative OTP core (target state, one-time
challenges, hashed authorization artifacts, issuance rate limits) but exposed
no HTTP endpoints. The scan-first registration flow let a public client upload
the Iraqi National Card, run OCR, and finalize an account with
`email_verified=False` — no proof of email ownership before identity OCR.

M31B must require verified email ownership *before* a new registration may
start identity OCR/upload, reusing the M31A OTP core, without weakening OTP
target binding, without a client-controllable OTP-purpose endpoint, and without
locking out existing production users.

## Decision

### Authoritative state

- Add a nullable `User.email_verified_at` timestamp. The existing
  `email_verified` boolean remains; `email_verified_at` is the authoritative
  stamp set only server-side when a registration completes with a verified
  session.
- Add a `RegistrationSession` (pre-registration, anonymous, capability-bound)
  in the `registration` app. It stores a SHA-256 capability digest of the
  high-entropy `session_token` (returned to the client exactly once), the
  normalized email (required to deliver the OTP and to bind the final
  registration), and non-secret account details (phone, governorate). Its
  lifecycle is `PENDING_EMAIL_VERIFICATION -> EMAIL_VERIFIED -> FINALIZED`
  (or `EXPIRED`). It is durable in PostgreSQL so verification survives app
  refresh/restart.
- The client can never assert `verified`: every serializer rejects unknown
  fields (400), and the server derives verification exclusively from the
  session row.

### OTP reuse

- All email-verification OTPs are issued with `OtpPurpose.EMAIL_VERIFICATION`
  hardcoded server-side. The target is always the session's own email — never
  client-supplied. Resend cooldown, TTL, attempt locks, and issuance rate
  limits all come from the M31A core untouched. No generic
  client-controlled-purpose endpoint is created.

### API shape (`/api/v1/auth/register/email/`)

- `POST start/` — account details (`email`, optional `phone`/`governorate`).
  Creates the session and issues the first OTP in one call. Returns
  `session_token` (once), masked email, status, `resend_at`, `expires_at`.
- `POST resend/` — `session_token`; re-issues the OTP (core enforces cooldown
  and rate limits and invalidates the previous challenge).
- `POST verify/` — `session_token` + `code`; calls `verify_otp` with the
  session email as target, then atomically marks the session
  `EMAIL_VERIFIED`. Strictly non-idempotent so code replay is denied by the
  consumed challenge.
- `GET status/` — `session_token` in the `X-Registration-Session-Token`
  header (never in URL/logs). Returns masked email, status, `email_verified`,
  `resend_at`, `expires_at` — the resume endpoint.

### Gating

- The public identity-extraction view requires a verified session capability
  in the `X-Registration-Session-Token` header; otherwise 403
  `registration_email_not_verified`. Extraction jobs are bound to the session.
- Final registration (`RegisterSerializer.registration_session`) requires a
  verified, non-expired session whose email matches the registration email and
  whose job is bound to it. On success the created user is stamped
  `email_verified=True` / `email_verified_at=now` and the session is marked
  `FINALIZED`.

### Existing-user grandfathering policy

- The email-verification gate applies only to the anonymous pre-registration
  identity-OCR path (a new-registration session). It is **not** applied
  retroactively: existing `ACTIVE` accounts (many with `email_verified=False`)
  keep logging in and using the platform unchanged. No rows are modified and
  no destructive migration is required.
- A future milestone that needs verified email for sensitive account
  operations (e.g. M32A password reset) will design its own explicit backfill,
  opt-in flow, and lockout rules separately.

## Trade-offs

- Storing the normalized email on the pre-registration session is required for
  OTP delivery, final binding, and resume. It is the user's own registration
  data (never a third party's medical document) and is never returned except
  masked. All OTP tables continue to store only keyed hashes.
- `start` is not idempotent by design (each start creates a fresh session), but
  session churn is bounded by the OTP target/API issuance limits, so an
  attacker cannot create unbounded sessions for a victim email.
- `start` does not check email existence, preserving the no-enumeration
  posture; the duplicate-email rejection happens at final registration exactly
  as before.

## Consequences

- New registrations can no longer reach identity OCR without a verified email.
- Existing users are unaffected (grandfathered).
- OTP purpose remains server-chosen; M31A security properties are preserved.

## Delivery readiness (verified against production)

The M31B E2E surfaced a delivery-provider gap, not a code gap:

- The Resend account (`techda.info@gmail.com`) is in Resend **test/sandbox
  mode**: the default sender `onboarding@resend.dev` only delivers to the
  account owner's own address. Sending to any other recipient returns a 403
  `validation_error` until a domain is verified at resend.com/domains.
- Probing candidate senders (`noreply@techda.dev`, `pmdap@techda.dev`,
  `pmdap@techda.info`, `pmdap.dev`) all return "domain is not verified".
- The project's legacy SMTP host (`premium86.web-hosting.com`, port 587/465)
  delivers from outside but the Railway web container's egress **times out** on
  both ports, so it is not a viable production OTP path.
- The delivery resolver (`registration.email_services.get_otp_delivery_service`)
  uses Resend when `RESEND_API_KEY` is set, else Django's SMTP backend; the
  web container can reach `api.resend.com`, so Resend remains the intended
  provider.

**Required before real-user delivery works:** verify a sending domain in the
Resend account (Domains → Add domain → add SPF/DKIM/DMARC → verify), then set
`RESEND_FROM_EMAIL` on Railway to an address on that verified domain (e.g.
`noreply@<domain>`). Until then `start/` returns 503
`registration_email_delivery_failed` for non-owner recipients — a deliberate
generic error (no provider detail leak). Delivery failures log only the
exception type/message via `_log_delivery_cause` (never the OTP or target).
