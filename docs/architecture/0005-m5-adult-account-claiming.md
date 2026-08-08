# ADR 0005: Adult Patient Account Claiming

## Status

Accepted for M5 implementation on 2026-08-08.

## Context

An unowned PatientProfile must become directly owned after its patient reaches
adulthood without creating another identity or moving history. Public claim
submission uses Digital ID and new identity evidence, creating enumeration,
upload-abuse, ownership-race, account-activation, and identity-truth risks.

## Decision drivers

- Patient UUID, Digital ID, identity documents, and guardian history must survive.
- Digital ID identifies a profile but grants no authority.
- Public responses must not disclose whether a profile or email exists.
- Ownership linking and guardian normalization must be one transaction.
- Activation secrets must be random, hashed at rest, expiring, and single use.
- M3 private storage and M1 password/login rules must be reused.

## Options considered

### Public submission privacy

1. Return specific eligibility errors. Clear for clients, but creates patient and
   account enumeration.
2. Return one accepted receipt for every structurally valid request, persisting
   only eligible claims.
3. Require an authenticated temporary claim session.

Choose option 2. Every structurally valid request returns HTTP 202 with a random
receipt UUID and `PENDING`; only an eligible request creates a database claim.
Input-format and upload errors remain 400, and throttling remains observable 429.
Receipt UUID alone has no read or evidence authority.

### Submitted identity evidence

1. Create or replace an M3 IdentityDocument during public submission.
2. Preserve claim evidence separately and require an already-verified current
   National Card before approval.
3. Automatically approve when submitted fields match.

Choose option 2. ClaimIdentityEvidence references M3 IdentityFile for private
bytes but is not patient identity truth. Approval requires VERIFIED profile state
and a verified current Unified National Card. Name, DOB, and document-number
comparisons are review aids only. Mismatch never auto-rejects. M3 replacement can
occur through its controlled workflow after activation; M5 does not promote claim
evidence automatically.

### Activation delivery

1. Generate and email a password.
2. Store a raw activation token until delivery.
3. Return the raw one-time token once to the approving exact-role agent for
   manual customer-service delivery, storing only SHA-256.

Choose option 3 because no email/SMS provider exists in M5. Token is generated
with `secrets.token_urlsafe(32)`, expires after 30 minutes, and becomes invalid
after one successful activation. Approval replay returns stable conflict instead
of exposing or regenerating the secret.

## Data model

- PatientAccountClaim stores target profile, requested account fields, submitted
  identity fields, deterministic comparison indicators, controlled status,
  reviewer facts, public reason, internal notes, and approved account.
- ClaimIdentityEvidence stores National Card and optional Passport evidence using
  protected IdentityFile references.
- AccountActivation stores claim/account, token hash, expiry, and use time.
- PatientAccountClaimEvent is append-only and excludes raw tokens, email, phone,
  Digital ID, document numbers, filenames, and storage paths.

Partial unique constraints permit one active claim per patient and one active
claim per case-insensitive requested email. Active states are PENDING,
UNDER_REVIEW, and MORE_INFORMATION_REQUIRED.

## Approval and activation

Approval locks claim, profile, account conflicts, and guardian relationships. It
rechecks adulthood, unowned profile, verified identity, current verified National
Card, email availability, and reviewable state. It creates one unusable-password
PATIENT account with `PENDING_ACTIVATION`, links only `PatientProfile.user`, ends
active guardian relationships with `PATIENT_REACHED_ADULTHOOD`, records immutable
events, and creates activation.

Activation locks token and user, verifies hash/expiry/unused state plus claim
approval, applies Django password validation, sets password, changes account to
ACTIVE, and records use. Existing M1 authentication rejects non-ACTIVE status.

## Consequences

- Public callers cannot distinguish patient/account eligibility.
- Ineligible uploads are validated but never persisted.
- Agent response carries a high-value token once; HTTPS and controlled manual
  delivery remain operational requirements until provider integration.
- MORE_INFORMATION_REQUIRED has no anonymous update API in M5.
- Submitted mismatching/newer cards remain evidence and require later M3 work.

## Revisit triggers

- Messaging provider integration replaces agent-mediated token delivery.
- Secure claim sessions enable public status and evidence updates.
- Product requires pre-approval promotion of newer National Cards.

