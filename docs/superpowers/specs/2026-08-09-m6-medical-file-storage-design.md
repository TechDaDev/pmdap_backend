# M6 Medical File Storage Design

## Goal

Deliver private PDF/JPEG/PNG medical-document upload, retrieval, metadata
maintenance, download, duplicate protection, and soft deletion for an adult's
own profile and for minors governed by a live verified M4 relationship.

## Domain model

`StoredFile` is immutable upload evidence. It records storage key, safe original
name, MIME type, exact size, SHA-256, optional PDF page count, integrity state,
and malware-scan state. `MedicalDocument` links exactly one stored file to one
patient and records type, optional title/description/date, date provenance,
facility/location/department/physician metadata, processing status, and active
or deleted state. `MedicalDocumentEvent` is append-only and records upload,
metadata update, deletion, duplicate rejection, and integrity checking.

Supported document types are controlled values: laboratory, radiology,
prescription, consultation, medical report, hospital admission, discharge
summary, surgery procedure, pathology, vaccination, vital signs, and other.
Processing begins and remains `UPLOADED` in M6.

An upload-supplied date is `USER_ENTERED` and verified. A later changed date is
`USER_CORRECTED` and verified. Clearing the date clears its source and
verification timestamp. `PDF_TEXT`, `OCR`, and `USER_CONFIRMED` are reserved for
later phases and cannot be selected through M6 input.

## API contract

Adult endpoints:

- `GET, POST /api/v1/documents/`
- `GET, PATCH, DELETE /api/v1/documents/{uuid}/`
- `GET /api/v1/documents/{uuid}/file/`

Guardian endpoints:

- `GET, POST /api/v1/minors/{minor_uuid}/documents/`
- `GET, PATCH, DELETE /api/v1/minors/{minor_uuid}/documents/{uuid}/`
- `GET /api/v1/minors/{minor_uuid}/documents/{uuid}/file/`

Create is multipart and accepts `file`, `document_type`, and optional editable
metadata. Responses expose document metadata and safe file evidence but never a
digest, storage key/path, uploader internals, deletion internals, or patient
identity beyond the route's authority. PATCH accepts metadata only. Unknown or
protected fields fail with the established error envelope. Unsupported methods
return 405. Active records use deterministic newest-first ordering.

Adult lookup derives an owned patient profile from the authenticated active
account. Guardian lookup reuses the M4 live-relationship service on every
request. Cross-patient UUIDs resolve as 404, including file downloads.

## Validation and storage flow

1. Apply the authenticated upload throttle and bounded multipart handling.
2. Sanitize the supplied filename by discarding path components and control
   characters, normalizing separators, and enforcing a bounded display length.
3. Read at most `MEDICAL_FILE_MAX_BYTES + 1`; reject empty or oversized input.
4. Validate actual content. Pillow verifies JPEG/PNG. A strict PDF parser verifies
   a readable, unencrypted container and counts pages. Extension and declared
   content type must agree with the detected format.
5. Compute SHA-256 over the unchanged original bytes.
6. Reject an already-active patient-scoped digest with a stable 409 response.
7. Store under a random key in the private medical root, create `StoredFile`,
   create `MedicalDocument`, and append `UPLOADED` in one database transaction.
8. If the transaction fails, remove the new blob. If the database constraint
   wins a race, remove the losing blob and return the same conflict.

Downloads stream the stored original with `nosniff`, a safe attachment filename,
and no public URL. Soft deletion retains bytes and evidence but excludes the
record from all normal access. The partial active-digest constraint permits a
later re-upload after deletion.

## Security model

Assets are medical bytes, metadata, authorship, integrity evidence, and patient
association. Primary attacker paths are IDOR, guardian-role confusion, spoofed
formats, decompression/resource exhaustion, path or header injection, duplicate
oracles, malicious active content, and orphaned blobs.

Controls are authority-derived routes, live guardian checks, private no-URL
storage, allowlisted structural validation, bounded reads and dimensions,
random keys, sanitized filenames, attachment streaming, patient-scoped database
uniqueness, generic conflicts, upload throttling, and failure compensation.

Residual risk is explicit: M6 has no malware engine. The scanner abstraction
records `NOT_CONFIGURED`, never `CLEAN`. Accepted content is stored but never
rendered or executed by the backend. Operational malware scanning and quarantine
remain future work.

## Integrity behavior

An internal integrity service streams the stored original through SHA-256 and
compares it with immutable upload evidence. It records `VALID` or `CORRUPTED`
and appends `FILE_INTEGRITY_CHECKED`. There is no public M6 integrity mutation
endpoint. Ordinary serializers cannot edit file evidence.

## Verification

Tests cover models and constraints, allowed formats, spoofing and malformed
files, limits, names, exact-byte preservation, adult and guardian CRUD/download,
relationship expiry/revocation/age-out, cross-patient isolation, duplicate races,
rollback cleanup, scanner truthfulness, integrity tampering, soft deletion,
method handling, throttling, error/schema consistency, and M0-M5 regression.
PostgreSQL tests prove the concurrency constraint and compensation behavior.

## Non-goals

No text extraction, OCR, content-derived dates, archive search/indexing,
facilities, doctors, AI, restore/hard-delete workflow, or production malware
scanner is implemented.
