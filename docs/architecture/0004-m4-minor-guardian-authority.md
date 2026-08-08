# ADR 0004: M4 Minor Identity and Guardian Authority

## Status

Accepted for M4 implementation on 2026-08-08.

## Context

Minors need permanent patient identities without login accounts. Adults may
manage those identities only through explicit, reviewed, temporary authority.
M4 must support Birth Documents, parent-family-number signals, legal evidence,
mobile retries, immediate age-18 access termination, and real PostgreSQL
concurrency verification without introducing M5 account claiming or M6 medical
documents.

## Decision drivers

- Minor PatientProfile ownership must remain independent from guardian accounts.
- Guardian access must fail closed and be evaluated live.
- Relationship evidence and decisions must remain historical.
- Birth Documents must be useful for children without National Cards.
- Retry handling must not merge distinct siblings or duplicate one child.
- M3 identity workflows and private storage must be reused.

## Options considered

### Minor identity verification

1. Require Unified National Card for every minor. Simple adult parity, but
   excludes children who only have Birth Documents.
2. Verify minors through a current verified National Card or Birth Document.
3. Let relationship approval implicitly verify minor identity.

Choose option 2. Identity-document review remains separate from relationship
review. Guardian approval requires a verified current primary minor document.

### Relationship evidence storage

1. Encode legal evidence as IdentityDocument. Reuses a model but conflates
   patient identity with guardian authority.
2. Introduce generic StoredFile now. Reusable later, but prematurely enters M6.
3. Add GuardianEvidence referencing M3 IdentityFile.

Choose option 3. It reuses private byte storage and validation while preserving
domain boundaries.

### Minor-creation idempotency

1. Deduplicate payload fields. This can merge twins and legitimate corrections.
2. Require `Idempotency-Key`, scoped to guardian plus minor creation, and bind it
   to a canonical request fingerprint.
3. Provide no retry contract.

Choose option 2. Same key plus same request replays one result. Same key plus a
different request returns conflict. PostgreSQL uniqueness and row locking make
concurrent retries converge.

### Guardian identity-document access

1. Duplicate M3 endpoints under `/minors/`.
2. Extend M3 detail, image, and replacement authorization through a narrow
   guardian policy function.

Choose option 2. Collection create/list remain direct-owner operations. Existing
document detail, image, and replacement operations accept a verified active
guardian only while the linked patient is currently a minor.

## Data model

- GuardianRelationship links guardian User to minor PatientProfile. Relationship
  type, verification state, active period, family-number signal, reviewer, and
  ending/rejection facts are explicit controlled fields.
- GuardianEvidence references GuardianRelationship and IdentityFile with a
  controlled evidence type.
- GuardianRelationshipEvent is append-only and contains workflow facts without
  family numbers, identity numbers, filenames, or storage references.
- MinorCreationRequest uniquely keys guardian plus idempotency key and stores the
  request fingerprint and resulting minor/relationship references.

## Authorization

Guardian eligibility requires active PATIENT User, owned adult VERIFIED profile,
and current verified Unified National Card. Management requires a VERIFIED,
active relationship and a minor whose age is evaluated against current date.
UUID, Digital ID, family number, surname, and staff flags grant no authority.

On exact 18th birthday, authorization fails immediately without waiting for a
job. Relationship and identity history remain unchanged. M5 will later claim the
existing patient identity.

## Family-number signal

For FATHER and MOTHER, compare non-empty family numbers from guardian current
verified National Card and submitted minor National Card. Store MATCH, MISMATCH,
or UNAVAILABLE. Signal never approves, rejects, or authorizes a relationship.
Legal guardians ignore family-number matching and require official evidence.

## Verification behavior

- Same-agent repeated approval is idempotent.
- Another-agent approval after a decision, approval after rejection, or repeated
  rejection returns transition conflict.
- Rejection preserves minor profile, Digital ID, documents, evidence, and event
  history while denying rejected guardian access.
- Multiple guardians may hold independently reviewed relationships. Conditional
  uniqueness prevents duplicate active same guardian/minor/type relationships.

## Consequences

- Minor creation remains one transactional workflow with explicit storage
  cleanup after outer-transaction failure.
- Birth Documents become valid primary child identity evidence without changing
  adult verification policy.
- One dedicated evidence model and one domain-specific idempotency model enter
  M4; neither becomes a medical-file or generic workflow framework.
- PostgreSQL-only tests become a separate required acceptance lane.

## Revisit triggers

- M5 adds adult claiming and relationship-ending automation.
- M6 introduces generic medical StoredFile architecture.
- Product requires guardian invitation/onboarding API for a second guardian.
