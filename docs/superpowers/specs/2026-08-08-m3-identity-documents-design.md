# M3 Identity Documents and Verification Design

## Scope

M3 adds private government identity evidence, patient-owned document history,
verification-agent review, deterministic profile state, replacement, and
immutable domain events. It excludes minors, guardians, claims, medical
documents, archive, OCR, facilities, doctors, and AI.

## Patient API contract

- `POST /api/v1/identity-documents/`: multipart own-document submission.
- `GET /api/v1/identity-documents/`: paginated own history with safe summaries.
- `GET /api/v1/identity-documents/{uuid}/`: own document detail.
- `POST /api/v1/identity-documents/{uuid}/replace/`: submit replacement.
- `GET /api/v1/identity-documents/{uuid}/images/{side}/`: authorized original
  byte stream for `front` or `back`.

No patient UUID input, PATCH, PUT, or DELETE operation exists. National Card
requires three separate numbers, Iraqi issuing country (`IQ`), and both images.
Passport requires document number, issuing country, dates, and front/data image.
Birth and other government IDs are recordable without child workflow.

## Verification-agent API contract

- `GET /api/v1/verification/identity-documents/`: paginated queue with optional
  `status` filter.
- `GET /api/v1/verification/identity-documents/{uuid}/`: review detail.
- `POST /api/v1/verification/identity-documents/{uuid}/approve/`.
- `POST /api/v1/verification/identity-documents/{uuid}/reject/`: reason required.

Exact verification-agent role required. Patient and ADMIN roles are denied.
Repeating approval by the same agent is an idempotent read of the verified
state. Approval by a different agent, approval after rejection, and repeated
rejection return a transition conflict.

## Data and response rules

- List and queue projections omit document, national, and family numbers.
- Authorized details include evidence fields needed by owner or reviewer, but
  never storage references, paths, hashes, or internal file IDs.
- Shared success/error envelopes continue. OpenAPI describes actual multipart,
  query, request, success, and error schemas.

## Upload security

- `IDENTITY_FILE_MAX_BYTES` defaults to 10 MiB.
- JPEG and PNG only; declared MIME must match decoded image format.
- Empty, malformed, executable-disguised, unsupported, and oversized content
  fails before persistence.
- Original bytes remain unchanged; SHA-256 is computed from uploaded bytes.
- Random storage names contain no patient or identity values.

## Transaction and event rules

Submission, file metadata, document row, profile state, and event share one DB
transaction. Blobs written before rollback are removed. Decisions lock affected
documents/profile. Conditional uniqueness prevents two verified current records
of one type. Append-only events are `UPLOADED`, `REPLACEMENT_SUBMITTED`,
`VERIFIED`, `REJECTED`, and `REPLACED`; event metadata contains no identity or
file secrets. No writable event API exists.
