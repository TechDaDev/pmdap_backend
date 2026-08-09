# M13 Medical Archive Search and Filters Design

## Contract

M13 lets an authorized patient or guardian locate medical reports through
structured metadata filters and PostgreSQL lexical search over metadata and the
canonical M7/M8 extracted text. It is retrieval-centric and patient-scoped. No
embeddings, vector search, LLM, semantic AI search, analytics, doctor access,
or external search engines.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/search/` | Adult owner search over own active documents |
| GET | `/api/v1/minors/{minor_uuid}/search/` | Verified guardian search for minor |

Adult target derives from `patients.api.owned_profile` (PATIENT role only).
Minor target derives from the live M4 `authorized_minor_relationship`
(verified + active + `is_minor`; exact age 18 denied). `patient_id`,
`digital_id`, and `guardian_id` query parameters are rejected as unknown.

## Search architecture

```
authorized patient context
        ↓
active MedicalDocument queryset
        ↓
validated filters
        ↓
optional PostgreSQL text search (dynamic SearchVector)
        ↓
deterministic ordering
        ↓
paginated results
```

Authorization is applied before any search criteria. Search never runs
globally and filters in Python.

## Search surface (`q`)

`q` searches these fields, in ranking weight order:

| Weight | Fields |
|---|---|
| A | `title` |
| B | `description`, `facility_name` (raw) |
| C | `location_text`, `department`, `physician_name` |
| D | canonical extracted text (`DocumentText.text`) |

`q` is a token-based lexical search. No stemming, no fuzzy matching, no
transliteration, no translation. A document without extracted text still
matches via metadata (`q` over metadata fields).

## PostgreSQL full-text strategy

Dynamic `SearchVector` computed per query (Option A), config `simple` on every
component, weights A–D. `SearchQuery(..., config='simple')` default
`search_type='plain'` (no user-controlled tsquery/regex syntax). Ordering with
`q`: `SearchRank` DESC, then `-document_date, -created_at, -uuid`.

Why dynamic:
- No persisted search vector → no synchronization races, no trigger, no
  reindex job; M10/M11 metadata changes reflect immediately.
- `simple` config is multilingual (whitespace tokenization, no English
  stemming) and needs no extension or migration.
- Patient-scoped active sets are small enough that computing the vector at
  query time is cheap (verified by EXPLAIN/perf sanity).

## date_status semantics (identical to M12)

- Default `VERIFIED`: `date_verified = true AND document_date IS NOT NULL`.
- `UNCONFIRMED`: `date_verified = false OR document_date IS NULL`.
- No third definition. Default matches M12 archive default so search and
  archive agree on membership for overlapping structured filters.

## Structured filters

| Param | Rule |
|---|---|
| `q` | max `SEARCH_QUERY_MAX_CHARS` (200); token lexical match |
| `date_from` | verified report date lower bound (inclusive) |
| `date_to` | verified report date upper bound (inclusive) |
| `year` | 1900–2100, verified report date range |
| `month` | 1–12, requires `year` |
| `document_type` | controlled M6/M11 enum, no fuzzy |
| `healthcare_facility` | UUID, within authorized archive only |
| `department` | case-insensitive substring |
| `physician_name` | case-insensitive substring |
| `uploaded_from` | `created_at >=` start of day (UTC) |
| `uploaded_to` | `created_at <` start of next day (UTC) |
| `date_status` | `VERIFIED` (default) or `UNCONFIRMED` |

All filters AND. One value per filter. Unknown parameters rejected.

## Ordering

- No `q`, `VERIFIED`/date filters: `-document_date, -created_at, -uuid`.
- No `q`, `UNCONFIRMED`: `-created_at, -uuid`.
- With `q`: `-rank, -document_date, -created_at, -uuid`.

Rank is retrieval relevance only, never medical relevance, and is not exposed
in responses.

## Response schema

Standard project pagination envelope:

```json
{
  "data": {"count": N, "next": null, "previous": null, "results": [...]}
}
```

Each result reuses the M12 safe `ArchiveDocumentSerializer` summary: `uuid`,
`title`, `document_type`, `document_date`, `date_verified`, `date_source`,
`healthcare_facility`, `facility_name`, `location_text`, `department`,
`physician_name`, `processing_status`, `created_at`. No extracted text,
snippets, OCR confidence, storage keys, hashes, file UUID, patient IDs, search
vector, or rank.

## Limits and DoS protection

- `q` length capped by `SEARCH_QUERY_MAX_CHARS` (200).
- Pagination page size fixed at 20 (bounded).
- `SearchQuery` `search_type='plain'` → no regex/tsquery injection.
- Patient-scoped active query uses M12 indexes plus a new
  `(patient, archive_status, created_at)` index for upload-date filters and
  unconfirmed ordering.
- Scoped `UserRateThrottle` `medical_search` (600/minute) following the
  existing endpoint-throttle pattern.

## Errors

Invalid filters use the standard `validation_error` envelope with per-field
details. Unauthorized minor/patient resources return 404. Non-patient roles
return 403. No new error codes.

## Privacy and events

`q` values are never logged (may contain medical information). Logs contain
only patient UUID, `query_present` bool, filter names, result count, and
duration. No document titles, diagnoses, facility-patient associations, or
medical text. No immutable audit events per search request.

## Indexes

One new `documents` migration adds `(patient, archive_status, created_at)`
(`archive_status_created_idx`). M12 indexes are reused; no duplicate indexes,
no `pg_trgm` extension (not justified for patient-scoped sets), no stored
search vector, no GIN full-text index (dynamic vectors).
