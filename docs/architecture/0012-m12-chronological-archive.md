# ADR 0012: Metadata-Driven Chronological Archive

- Status: Accepted for M12
- Date: 2026-08-09
- Scope: M12 only

## Context

Patients and verified guardians need chronological browsing of medical documents.
Archive folder trees or a duplicated materialized table risk divergence from the
authoritative metadata, file duplication, and synchronization bugs. Report dates
must be truthful: documents without a user-verified report date must not silently
land in an upload-time bucket. Archive ordering must be deterministic so
pagination stays stable across a lifelong archive.

## Decision

The archive is a **query projection over `MedicalDocument`**, never duplicated
storage. No `ArchiveEntry` table is created unless a demonstrated need appears
(later `architect-review`). No physical folders are created and no files are
copied, renamed, re-hashed, or re-extracted. Archive calls are strictly
read-only for `document_date`/`date_verified`/`date_source`/`date_verified_at`,
`document_type`/`classification_source`, and facility metadata.

`document_date` where `date_verified = true` is the **chronology authority**
(M10). The current `document_type` and current `healthcare_facility` are the
classification and facility authorities (M11). `DateCandidate`, classification
events, OCR/PDF text, and facility aliases are never used to derive current
archive position.

Eligibility is `archive_status = ACTIVE` plus authorization for the patient
context. A manually uploaded document with a verified date and valid metadata
is archiveable even if OCR/extraction failed; `TEXT_EXTRACTED` is not required.

## Unconfirmed-date bucket

`date_verified = false OR document_date IS NULL` defines an explicit
**unconfirmed** bucket. These documents remain accessible to the patient but are
never placed into a chronological date bucket. Default archive semantics:
verified chronological archive, with `?date_status=UNCONFIRMED` for unresolved
documents. Every list response reports `unconfirmed_date_count`.

## Deterministic ordering and pagination

- Verified: `-document_date, -created_at, -uuid`
- Unconfirmed: `-created_at, -uuid`

This foundation makes page boundaries stable even when documents share
`document_date` and `created_at`, because the UUID tie-break is total. The
project's `PageNumberPagination` (page size 20) is reused; no unbounded archive
responses.

## Immediate metadata reflection

Because the archive is query-driven, an authorized M10 date correction moves a
document between date buckets immediately; an M11 classification or facility
change is reflected immediately in summaries. No reindex, reprocessing, or
rebuild job is required.

## Consequences

- No file/date/classification duplication or mutation risk.
- Verified chronology is truthful; unconfirmed documents stay visible.
- Deterministic ordering gives stable pagination for long-term archives.
- Metadata corrections reflect instantly without background jobs.
- Archive queries carry load; justified indexes are handled in ADR 0013.
- PyMuPDF production licensing remains unresolved: AGPL/commercial dual license.
