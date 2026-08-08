# M5 Adult Patient Account Claiming Design

## Scope

M5 lets an adult claim one existing unowned PatientProfile. Same profile UUID,
Digital ID, identity history, and guardian history continue. No medical reports,
generic medical files, OCR, archive, facilities, doctors, AI, delegated adult
access, or second PatientProfile enters this phase.

## Public contract

`POST /api/v1/account-claims/` is anonymous, multipart, and throttled by IP.
Required fields: Digital ID, email, phone, full name, DOB, Unified National Card
number, and front/back images. Optional Passport evidence is a separate prefixed
field set and never substitutes for National Card.

After structural validation, every eligibility outcome returns HTTP 202:

```json
{"data":{"claim_id":"random-uuid","status":"PENDING"}}
```

Known and unknown Digital IDs, minor/owned/unowned profiles, active duplicates,
and unavailable emails share this contract. Ineligible requests create neither
claim nor stored files. Malformed fields, invalid/oversized images, unsupported
documents, and throttling retain normal 400/429 envelopes.

No public claim GET, status polling, evidence endpoint, cancellation, or update
exists. Claim UUID is a receipt reference, not a possession credential.

## Claim and evidence state

PatientAccountClaim statuses: PENDING, UNDER_REVIEW,
MORE_INFORMATION_REQUIRED, APPROVED, REJECTED, CANCELLED. Public input cannot set
status, comparisons, reviewer fields, approved account, notes, timestamps, file
references, or patient UUID.

Submitted name, DOB, and National Card number remain separate from PatientProfile
and IdentityDocument. Comparison indicators use MATCH, MISMATCH, or UNAVAILABLE.
Name comparison collapses whitespace and casefolds. DOB compares exact dates.
Document number compares normalized uppercase text against current verified
National Card. Indicators never approve or reject automatically.

ClaimIdentityEvidence reuses IdentityFile private storage, byte limits, MIME and
content validation, SHA-256, randomized storage names, and rollback cleanup.
National Card is mandatory; Passport may be supplemental only.

## Agent contract

Exact `IDENTITY_VERIFICATION_AGENT` role protects:

- `GET /api/v1/verification/account-claims/`
- `GET /api/v1/verification/account-claims/{uuid}/`
- `POST /api/v1/verification/account-claims/{uuid}/approve/`
- `POST /api/v1/verification/account-claims/{uuid}/reject/`
- `POST /api/v1/verification/account-claims/{uuid}/request-more-information/`
- `GET /api/v1/verification/account-claims/{uuid}/evidence/{evidence_uuid}/images/{side}/`

Queue supports status filtering and pagination. Agent detail exposes only claim
form, comparisons, existing patient identity fields, identity document summaries,
relationship history, and evidence metadata. No medical authorization or
guardian private account data appears.

Approval is allowed only from PENDING or UNDER_REVIEW. Rejection and more-info
are allowed from PENDING or UNDER_REVIEW. MORE_INFORMATION_REQUIRED has no public
resubmission path. Final decisions conflict with later decisions; approval replay
also returns stable 409 because raw activation token is returned only once.

## Ownership transaction

Approval locks claim and PatientProfile, then verifies:

- patient is at least 18 using PatientProfile.age_on;
- profile remains unowned;
- profile identity status is VERIFIED;
- current verified Unified National Card exists;
- requested email remains case-insensitively available;
- no competing account or ownership decision won;
- claim remains reviewable and has valid private National Card evidence.

Transaction creates exactly one PATIENT User with unusable password,
`PENDING_ACTIVATION`, no staff flags, and unverified contact flags. It links
existing `PatientProfile.user`; changes no other profile field. Active guardian
relationships become inactive and record ended_at plus
PATIENT_REACHED_ADULTHOOD. Existing inactive/rejected relationships and all
evidence remain unchanged. Claim becomes APPROVED and activation is created.

Injected failures roll back account, ownership, claim, activation, relationship
changes, and events together.

## Activation

`POST /api/v1/auth/activate-claimed-account/` is anonymous and separately
throttled. Payload contains token and new_password. Token is 32 random URL-safe
bytes; only SHA-256 is stored. Lifetime is configurable, default 30 minutes.

Activation validates password through existing Django validators, then locks and
consumes token once, sets password, and changes account status to ACTIVE. Expired,
used, malformed, wrong, cross-account, or inconsistent-state tokens are rejected
with stable codes. Before activation, login/JWT fails. After activation, login,
refresh, `/auth/me/`, and `/patients/me/` use same historical PatientProfile.

## Events and privacy

Immutable claim events record submission, review, more-info, approval, rejection,
patient linking, activation creation, and activation. Guardian ending uses
existing immutable relationship events. Event metadata contains no PII or token.

Throttle scopes: `account_claim_submit` and `account_claim_activation`. Production
uses configured Redis cache. Claim images have no public URL. Agent evidence
streaming performs exact-role and claim-object authorization.

## Verification

Tests cover public privacy equivalence, upload cleanup, mass assignment, state
transitions, transaction rollback, identity continuity, guardian normalization,
activation security, M1 integration, age-18 journey, OpenAPI, and four actual
PostgreSQL races. Full M0-M4 suite stays green; branch coverage remains >=90%.
