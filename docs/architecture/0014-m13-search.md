# ADR 0014: Patient-Scoped PostgreSQL Search

- Status: Accepted for M13
- Date: 2026-08-09
- Scope: M13 only

## Context

Patients and verified guardians need to locate reports by metadata and
extracted text without leaving the authorized, per-patient document set. Search
must stay read-only, deterministic, and consistent with M12 archive semantics,
while avoiding heavy infrastructure (embeddings, vector DBs, external engines,
LLMs) and avoiding synchronization-heavy stored search data.

## Decisions

### 1. Patient-scoped search
Search is built as a query projection over active `MedicalDocument` rows,
always starting from the authorized patient context. Authorization is applied
before any filter or keyword; results are never assembled globally.

### 2. Search surface
`q` covers metadata (`title`, `description`, `facility_name`, `location_text`,
`department`, `physician_name`) and the canonical M7/M8 `DocumentText.text`.
Only current medical document text is searched; retired OCR, duplicate
native/OCR representations, identity documents, guardian evidence, and claim
evidence are never searched.

### 3. Multilingual lexical strategy
PostgreSQL text-search configuration `simple` is used for every
`SearchVector`/`SearchQuery`. It tokenizes on whitespace/punctuation without
English stemming, so Arabic, English, and Kurdish tokens match exactly without
false stemming. This is token-based lexical search, not semantic multilingual
search; limitations are documented and no linguistic stemming claim is made.

### 4. Dynamic search vector (Option A)
`SearchVector` is computed at query time; no persisted vector column, trigger,
or reindex job. This eliminates vector-synchronization races and makes M10 date
corrections and M11 classification/facility changes reflect immediately. The
expected per-patient scale keeps dynamic computation cheap; the M13 performance
sanity verifies this. If future scale requires it, `architect-review` precedes
a persisted/GIN design.

### 5. Ranking
`SearchRank` over weighted vectors (title A, description/raw-facility B,
location/department/physician C, canonical text D) orders keyword results;
deterministic tie-breaks follow. Rank is retrieval relevance only, is never
presented as medical relevance, and is not exposed in responses.

### 6. Query-term privacy
Raw `q` values are never logged because they may contain medical information.
Operational logs record patient UUID, a `query_present` boolean, filter names,
result count, and duration only.

### 7. Limits and DoS protection
`SEARCH_QUERY_MAX_CHARS` (200) caps query length; `SearchQuery` uses default
`search_type='plain'` (no tsquery/regex injection); pagination is fixed at 20;
queries stay index-backed on patient-scoped sets; a scoped `medical_search`
throttle follows existing endpoint-throttle patterns.

### 8. Indexes and extensions
One new index `(patient, archive_status, created_at)` supports upload-date
filters and unconfirmed ordering. M12 indexes are reused; no duplicate
indexes. `pg_trgm` is intentionally not added: patient-scoped sets make it
unjustified for M13. No GIN full-text index (no stored vector).

### 9. Search/archive semantic consistency
`date_status` defaults to `VERIFIED` with the same definition and ordering as
M12, so equivalent structured filters (e.g. `year=2026&document_type=LABORATORY`)
agree on membership. Search adds `q`, date ranges, upload-date filters, and
department/physician filters on top of that shared foundation.

### 10. PostgreSQL authoritative search behavior
SQLite remains the fast unit lane for non-keyword filters; keyword/full-text
search behavior is PostgreSQL-only and authoritative. FTS tests are marked
`postgresql`.

## Consequences

- No embeddings/vector/LLM/external engine; no async search tasks.
- No stored search data to synchronize; metadata corrections reflect instantly.
- `simple` config gives deterministic multilingual lexical matching with
  documented limitations.
- Keyword behavior requires PostgreSQL (SQLite skips keyword tests).
- PyMuPDF production licensing remains unresolved: AGPL/commercial dual license.
