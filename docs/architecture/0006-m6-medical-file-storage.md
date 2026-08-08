# ADR 0006: M6 Private Medical Document Storage

- Status: Accepted
- Date: 2026-08-09
- Scope: M6 only

## Context

M6 introduces patient-owned medical document uploads without entering the M7
archive-processing scope. Medical records have different retention, integrity,
authorization, and future-processing needs from M3 identity evidence. The API
must support an adult's own documents and a verified guardian's current minor
relationship while preventing patient enumeration, public file exposure, and
duplicate races.

## Decision

### Separate file evidence from document metadata

`StoredFile` owns immutable upload evidence: a private storage key, sanitized
original filename, media type, byte count, SHA-256 digest, optional PDF page
count, integrity status, and malware-scan status. `MedicalDocument` owns the
patient relationship and editable clinical metadata. It refers one-to-one to a
`StoredFile`. `MedicalDocumentEvent` records append-only lifecycle evidence.

Medical files do not reuse M3 `IdentityFile`. Identity evidence has its own
verification lifecycle and access boundary; coupling it to longitudinal medical
records would create unsafe permissions and retention behavior.

### Patient-scoped active duplicate invariant

`MedicalDocument` carries an internal content digest copied from `StoredFile`.
A partial database unique constraint covers `(patient, content_sha256)` only
while the document is active. A service pre-check supplies a stable conflict,
while the database constraint is authoritative under concurrent uploads.
Digests and an existing document identifier are never returned in a duplicate
error, avoiding a cross-patient content oracle. A deleted record does not block
a later upload.

### Private storage and streaming

Medical bytes use a dedicated private filesystem rooted at
`MEDICAL_FILE_ROOT`. Storage has no public URL. A successful authorized download
is a Django streaming file response with an attachment disposition and a safe
filename. The application never exposes internal storage paths or keys.

PDF, JPEG, and PNG are the only M6 formats. Reads are bounded. Images are decoded
and verified. PDFs are structurally parsed to validate the container and count
pages; M6 never extracts text, runs OCR, detects dates from content, or executes
embedded content. Original accepted bytes are retained unchanged.

### Soft deletion with retained evidence

Deleting a document marks it deleted and records actor/time. Normal list,
detail, mutation, and file access cease immediately. The file and metadata are
retained for audit and later retention-policy work. No M6 endpoint restores or
hard-deletes a document.

### Explicit scanner boundary

A file-security scanner interface is present. The M6 implementation returns
`NOT_CONFIGURED`; it does not claim malware scanning or quarantine. The status
is stored truthfully so a later scanner can be integrated without changing the
document contract. Integrity verification is separate: it re-hashes the stored
original and records `VALID` or `CORRUPTED` plus an event.

### Authorization boundary

Adult routes derive the patient from the authenticated account; they accept no
patient UUID or Digital ID. Minor routes are nested under `minor_uuid` and must
resolve a live M4 verified guardian relationship for every operation. A minor
aging to 18 or a revoked/expired relationship ends access immediately. General
staff or verification roles receive no medical-document authority.

### Atomic persistence and compensation

The upload service validates before persistence, uses randomized storage keys,
and creates file and document rows transactionally. Because filesystem writes
are not transactional, any database failure compensates by deleting the newly
written blob. A database duplicate race maps to the same stable conflict and
also removes the losing blob.

## Consequences

- Medical content remains private and independently governable from identity
  evidence.
- Duplicate correctness relies on PostgreSQL's partial unique constraint, not
  an application-only check.
- Soft-deleted bytes consume storage until a later retention milestone.
- `NOT_CONFIGURED` exposes the real malware-defense posture; operations must not
  interpret it as clean.
- M7 can add extraction and processing without changing M6 upload semantics.

## Explicitly deferred

PDF text extraction, OCR, content-derived dates, archive indexing/search,
facilities, doctors, AI features, malware-engine integration, restoration, and
retention-driven hard deletion are outside M6.
