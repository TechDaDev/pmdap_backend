# ADR 0003: M3 Identity Document Storage and Verification

## Status

Accepted for M3 implementation on 2026-08-08.

## Context

M3 must preserve original government-ID images, prevent public storage access,
retain every verification decision, and switch verified identity documents
without gaps or duplicate current records. Medical-file infrastructure belongs
to M6 and must not be introduced early.

## Options considered

1. Reuse a generic stored-file model now. This could support M6, but couples
   identity evidence to medical-document requirements before those exist.
2. Store image bytes in PostgreSQL. Transactions are simple, but database size,
   backup cost, and future object-storage migration become worse.
3. Use an identity-owned file-reference model behind a private Django storage
   backend. This keeps original bytes outside PostgreSQL while leaving the
   backend replaceable with private S3-compatible storage.

## Decision

Choose option 3.

- `IdentityFile` stores opaque file reference, verified media type, byte size,
  SHA-256 digest, and safe original filename metadata.
- Identity bytes use dedicated private storage outside public media. APIs never
  serialize a storage key, filesystem path, or direct URL.
- Authorized owner and verification-agent image access streams through an API.
- Upload validation allowlists JPEG and PNG by declared MIME and decoded image
  format, rejects empty/malformed/oversized content, and preserves original
  bytes without re-encoding.
- `IdentityDocument` records remain historical. No delete or arbitrary update
  API exists. Replacements create linked records.
- `IdentityDocumentEvent` is an append-only M3 domain journal. Cross-domain
  audit hardening remains deferred to M14.

## Verification and replacement rules

- New submission is `PENDING` and lifecycle `CURRENT`. Existing same-type
  records require explicit replacement.
- One pending candidate per patient/type is enforced transactionally. PostgreSQL
  enforces at most one verified `CURRENT` document per patient/type.
- Existing verified document stays `CURRENT` while replacement is pending.
- Approval locks patient/type rows, marks old verified document `REPLACED`, then
  verifies candidate. Rejection leaves old document current.
- Profile becomes `VERIFIED` only from current verified Unified National Card.
  Pending card sets `PENDING_VERIFICATION` only without a verified card. A final
  failed card sets `REJECTED`; passport approval never verifies profile.

## Authorization decision

Only exact role `IDENTITY_VERIFICATION_AGENT` may review or decide documents.
Patient access derives from authenticated ownership. UUID knowledge grants no
access. Verification role grants no medical-document access.

## Consequences

- M3 gains a small identity-specific file model that M6 need not reuse.
- Private object storage can replace local storage without API changes.
- Pending candidates and current verified documents coexist safely.
- Database constraint covers critical verified-current uniqueness; service
  locks cover richer transition rules.

## Revisit triggers

- M6 introduces generic medical-file storage.
- Deployment adopts S3-compatible storage or signed downloads.
- Multiple concurrent reviewers require queue claiming.
