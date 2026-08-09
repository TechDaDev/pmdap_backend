# ADR 0015 — M14 Audit Log and Integrity Hardening

## Status

Accepted

## Context

Medical Care Connect stores sensitive identity and medical records. Every
security-significant transition (account claim, identity verification,
guardian authority, document lifecycle, date confirmation, processing
failure, file-integrity failure) currently mutates state with no forensic
trail. Files were validated at upload but never re-verified against
tampering or loss after storage. The system needed an append-only audit log
with strict privacy sanitization and a file-integrity verification path.

## Decision

### Audit log

- Introduce `audit.AuditLog` (UUID pk) recording ~33 security-significant
  actions with actor, actor type, patient scope, resource, redacted
  before/after values, and per-request correlation id.
- Immutability is **application-level**: `save()` rejects updates and
  `delete()` raises on the model; the admin is read-only. A DB trigger is
  deliberately **not** used (it would interfere with test teardown and add
  operational risk). The log is documented as "append-only through
  application APIs".
- Recursive redaction in `audit.services.sanitize_audit_values` replaces
  credentials, hashes, tokens, document numbers, national/family numbers,
  storage paths, and raw extracted text with `[REDACTED]`.
- Per-request `request_id` via middleware, returned as `X-Request-Id`.
- Indexes on `(patient, created_at)`, `(actor, created_at)`,
  `(action, created_at)`, `(resource_type, resource_uuid)`.
- No public audit endpoint.

### Integrity

- `StoredFile.IntegrityStatus` adds `MISSING`.
- `verify_stored_file_integrity` revalidates size + SHA-256 atomically
  (with row lock), marks `CORRUPTED`/`MISSING`, emits an event + audit, and
  never mutates file bytes.
- Downloads of non-`VALID` blobs stay blocked by the controlled M6
  `MedicalFileUnavailable` (409).
- Batch verification via `verify_medical_file_integrity` management command
  with count-only output.

## Consequences

- Every security-significant action now has a sanitized, chronological,
  immutable-at-the-API trail suitable for forensic review.
- Redaction prevents credential/document-number leakage into audit payloads.
- Integrity failures surface as `INTEGRITY_FAILURE` audits and block
  download of tampered/missing files.
- Application-level immutability is honest but bypassable at the raw SQL
  layer; this is documented as a known limitation.
- No new public API surface for the audit log in this milestone.
