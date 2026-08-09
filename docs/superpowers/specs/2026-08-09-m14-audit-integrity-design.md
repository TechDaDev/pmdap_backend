# M14 Audit, Integrity, and Security Hardening Design

## Contract

M14 adds an immutable, privacy-sanitized audit log over every security-
significant medical-care action, plus file-integrity verification for stored
medical files, and a hardening sweep (IDOR, mass assignment, search
injection, logging, constraints, concurrency, performance). It is
append-only and read-only through the public API — there is no public audit
endpoint (M14 #22). No analytics, no doctor workflows, no external audit
vendor.

## Audit log

### Model (`audit.AuditLog`)

UUID primary key, append-only via application guards.

| Field | Type | Notes |
|---|---|---|
| `actor` | FK `User` (SET_NULL) | Who acted; null for pure system events |
| `actor_type` | `USER` / `SYSTEM` | SYSTEM for background transitions |
| `patient` | FK `PatientProfile` (SET_NULL) | Scope of the affected record |
| `resource_type` | str | `USER`, `DOCUMENT`, `CLAIM`, ... |
| `resource_uuid` | UUID | Referenced resource |
| `action` | enum | ~33 security-significant actions |
| `previous_values` / `new_values` | JSON | Redacted diff |
| `metadata` | JSON | Redacted context |
| `request_id` | str | Per-request correlation id |

### Action inventory (security-significant only)

Account: `ACCOUNT_CREATED`, `ACCOUNT_STATUS_CHANGED`, `ACCOUNT_ACTIVATED`,
`ACCOUNT_CLAIM_SUBMITTED`, `ACCOUNT_CLAIM_APPROVED`, `ACCOUNT_CLAIM_REJECTED`,
`PATIENT_ACCOUNT_LINKED`, `ACCOUNT_ACTIVATION_CREATED`.
Identity: `IDENTITY_DOCUMENT_UPLOADED`, `IDENTITY_DOCUMENT_VERIFIED`,
`IDENTITY_DOCUMENT_REJECTED`, `IDENTITY_DOCUMENT_REPLACED`,
`PATIENT_IDENTITY_STATUS_CHANGED`.
Minors/guardians: `MINOR_CREATED`, `GUARDIAN_RELATIONSHIP_SUBMITTED`,
`GUARDIAN_RELATIONSHIP_VERIFIED`, `GUARDIAN_RELATIONSHIP_REJECTED`,
`GUARDIAN_RELATIONSHIP_ENDED`, `GUARDIAN_ACCESS_EXPIRED`.
Documents/date: `DOCUMENT_UPLOADED`, `DOCUMENT_METADATA_UPDATED`,
`DOCUMENT_DELETED`, `DOCUMENT_TYPE_CHANGED`, `DOCUMENT_FACILITY_CHANGED`,
`DATE_CONFIRMED`, `DATE_CORRECTED`.
Processing: `PDF_EXTRACTION_FAILED`, `OCR_FAILED`.
Integrity: `FILE_INTEGRITY_CHECKED`, `INTEGRITY_FAILURE`.
Claims/activation: `CLAIM_SUBMITTED`, `CLAIM_MORE_INFORMATION_REQUIRED`,
`CLAIM_REJECTED`, `CLAIM_APPROVED`.

### Immutability

Application-level only (no DB trigger — DB triggers would break test
teardown and add operational risk). `AuditLog.save()` rejects updates;
`AuditLog.delete()` raises; `AuditLogAdmin` is read-only. The log is
"append-only through application APIs". Documented honestly.

### Redaction (`audit.services.sanitize_audit_values`)

Recursive redaction of `SENSITIVE_KEYS` (credentials, hashes, tokens,
document numbers, national/family numbers, storage paths, raw OCR/text,
context) to `[REDACTED]`. Wiring passes structured diffs, never raw content.

### Request correlation

`common.middleware.AuditRequestIdMiddleware` sets a per-request id in
thread-local storage and returns it as `X-Request-Id`. `record_audit()`
stamps every entry with the current id.

### Indexes

`(patient, created_at)`, `(actor, created_at)`, `(action, created_at)`,
`(resource_type, resource_uuid)` — verified with EXPLAIN on 17k rows.

## File integrity

`StoredFile.IntegrityStatus` gains `MISSING`. `verify_stored_file_integrity`
(atomic, `select_for_update`) recomputes size + SHA-256, marks
CORRUPTED/MISSING, writes a `FILE_INTEGRITY_CHECKED` event and an
`INTEGRITY_FAILURE` audit on mismatch, and never mutates bytes. Downloads
for non-VALID blobs return the controlled M6 `MedicalFileUnavailable` (409).
`verify_medical_file_integrity` management command supports batch scans with
safe, count-only output.

## Hardening sweep

- IDOR: actor matrix across documents, identity, minors, archive, search.
- Mass assignment: protected-field injection rejected on upload/update,
  identity submission, minor creation.
- Search injection: PG `SearchQuery` (plain) treats operators as literal;
  patient scope enforced; oversized q rejected.
- Logging: static scan — no sensitive payloads in `logger.*` calls.
- Constraints: unique email (CI), digital_id, active claim, verified current
  identity type, guardian active relationship, immutable audit guards.
- Concurrency: idempotent confirm/approve operations write exactly one
  semantic audit; audit writes are transactional with rollback.
- Performance: 17k-row index sanity with EXPLAIN; bulk ingest.

## Explicitly out of scope

No public audit API, no audit UI, no analytics, no AI/LLM interpretation,
no doctor workflows, no external audit service, no DB trigger immutability.
