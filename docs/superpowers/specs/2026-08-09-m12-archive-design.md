# M12 Chronological Medical Archive Design

## Contract

M12 builds a patient-facing medical archive as **metadata-driven views** over
existing `MedicalDocument` records. Archive browsing never physically
reorganizes, copies, renames, re-hashes, or re-extracts files, and never
mutates date, classification, or facility authority. It is a query projection,
not a second source of truth.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/archive/` | Adult owner verified chronological archive |
| GET | `/api/v1/archive/summary/` | Adult owner grouping summary |
| GET | `/api/v1/minors/{minor_uuid}/archive/` | Verified guardian archive for minor |
| GET | `/api/v1/minors/{minor_uuid}/archive/summary/` | Guardian summary for minor |

Minor routes reuse the live M4 `authorized_minor_relationship` authorization
(verified + active + `is_minor`). Exact-age-18 is denied immediately. Adult
routes reuse `patients.api.owned_profile` (PATIENT role only), so
`IDENTITY_VERIFICATION_AGENT` and other roles receive 403. No `patient_id` or
`digital_id` query parameters exist; unknown query parameters are rejected.

## Archive semantics

- **Eligibility:** `archive_status = ACTIVE` plus authorization for the patient
  context. Soft-deleted documents are excluded from lists, summaries, counts,
  and every grouping.
- **Verified archive (default):** `date_verified = true AND document_date IS
  NOT NULL`. Ordering is deterministic `-document_date, -created_at, -uuid`.
- **Unconfirmed bucket:** `date_verified = false OR document_date IS NULL`.
  Ordering is deterministic `-created_at, -uuid`. The list response always
  reports `unconfirmed_date_count` and `?date_status=UNCONFIRMED` lists them.
- **Processing-state independence:** a document with verified date and valid
  metadata is archiveable even if OCR/extraction failed; `TEXT_EXTRACTED` is
  not required.
- **Authorities:** `document_date` (M10), `document_type` (current M11 value),
  `healthcare_facility` (current M11 link). No recomputation from
  `DateCandidate`, classification events, raw text, or facility aliases.

## Filters

Query parameters validated by `ArchiveFilterSerializer` (stable
`validation_error` envelope; no silent ignoring):

| Param | Rule |
|---|---|
| `date_status` | `VERIFIED` (default) or `UNCONFIRMED` |
| `year` | 4-digit integer; applied as a `document_date` range for index use |
| `month` | 1–12; requires `year` |
| `document_type` | current controlled M6/M11 enum; invalid rejected |
| `healthcare_facility` | UUID; applied only within authorized archive |

Incompatible combinations rejected: `UNCONFIRMED` + `year`, `UNCONFIRMED` +
`month`, `month` without `year`.

## Response shapes

### List

```json
{
  "data": {
    "count": 4,
    "next": null,
    "previous": null,
    "results": [
      {
        "uuid": "...",
        "title": "...",
        "document_type": "LABORATORY",
        "document_date": "2026-04-02",
        "date_verified": true,
        "date_source": "USER_CONFIRMED",
        "healthcare_facility": {"uuid": "...", "name": "..."},
        "facility_name": "Raw facility label",
        "location_text": "Baghdad / Karkh",
        "department": "Hematology",
        "physician_name": "Dr Example",
        "processing_status": "DATE_CONFIRMED",
        "created_at": "..."
      }
    ],
    "unconfirmed_date_count": 2
  }
}
```

The list omits storage key, SHA-256, file UUID, raw extracted/OCR text,
date-candidate context, patient internal IDs, and uploader internals. Document
detail remains at `GET /api/v1/documents/{uuid}/`; no duplicate detail route.

### Summary

```json
{
  "data": {
    "years": [
      {"year": 2026, "count": 4, "months": [{"month": 3, "count": 3}]}
    ],
    "document_types": [{"document_type": "LABORATORY", "count": 4}],
    "facilities": [{"uuid": "...", "name": "...", "count": 2}],
    "unconfirmed_date_count": 2
  }
}
```

`years`/`months` count verified dates only. `document_types` and `facilities`
count all active documents (unconfirmed remain visible by type/facility while
never appearing in a date bucket). Counts are strictly per authorized patient;
no global/cross-patient statistics.

## Query layer

`archive.services.ArchiveQueryService(patient)` owns authorization-bounded
queryset construction, filtering, deterministic ordering, and grouping. Views
stay thin. The queryset uses `select_related` on the facility chain
(country/region/city) to avoid N+1.

## Pagination

`PageNumberPagination`, page size 20, matching project conventions. Deterministic
ordering guarantees stable page boundaries, including documents sharing
`document_date` and `created_at` (UUID tie-break).

## Indexes

New `documents` migration adds three indexes to `MedicalDocument`:

- `(patient, archive_status, document_date)` — chronological + year/month range
- `(patient, archive_status, document_type)` — type filter/summary
- `(patient, archive_status, healthcare_facility)` — facility filter/summary

No materialized `ArchiveEntry` table. No full-text/search/analytics in M12.

## Errors

Invalid filters use the standard `validation_error` envelope with per-field
details. Unauthorized minor/patient resources return normal 404. Non-patient
roles receive 403. No new error codes are created.

## Privacy and events

Logs contain only user/document UUIDs, filter names, counts, and duration; no
patient names, titles, facility-patient associations, dates, or medical text.
Archive GETs create no immutable audit events; metadata mutations already carry
events from prior phases.
