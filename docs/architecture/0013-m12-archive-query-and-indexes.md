# ADR 0013: Archive Query and Index Strategy

- Status: Accepted for M12
- Date: 2026-08-09
- Scope: M12 only

## Context

Archive views filter, order, and aggregate `MedicalDocument` metadata per
patient. Without care, per-request aggregation and missing indexes cause N+1
queries, unbounded scans, and unstable ordering. Grouping must never expose
cross-patient statistics, and invalid filter combinations must fail predictably
instead of silently misbehaving.

## Decision

A dedicated `archive.services.ArchiveQueryService(patient)` owns authorization-
bounded queryset construction, filtering, deterministic ordering, and grouping;
views stay thin and reuse existing adult/minor authorization services. The
queryset uses `select_related("healthcare_facility__country",
"healthcare_facility__region", "healthcare_facility__city")` so the facility
chain costs no N+1. Grouping uses a small, fixed number of aggregate queries:

- years/months from one `ExtractYear`/`ExtractMonth` aggregation over verified
  dates;
- document types and facilities as separate `GROUP BY` aggregations over active
  documents;
- unconfirmed count as a single bounded count.

`year`/`month` filters are applied as `document_date` range predicates
(`>= start, < end`) rather than `EXTRACT(...)=`, so they can use a B-tree index.

## Indexes

One `documents` migration adds three justified indexes to `MedicalDocument`:

| Index | Columns | Serves |
|---|---|---|
| `archive_patient_status_date_idx` | `patient, archive_status, document_date` | verified chronology, year/month range |
| `archive_patient_status_type_idx` | `patient, archive_status, document_type` | type filter + type grouping |
| `archive_patient_status_facility_idx` | `patient, archive_status, healthcare_facility` | facility filter + facility grouping |

`archive_status` is included because every archive query filters on it. No
redundant or speculative indexes are added. Query plans were inspected via
`EXPLAIN` during performance sanity (see M12 report). No materialized table is
introduced; if later evidence requires one, `architect-review` precedes it.

## Summary privacy

Counts and groupings are computed strictly from the authorized patient's active
documents. No global top facilities, document-type prevalence, or patient
counts are exposed.

## Consequences

- Bounded, index-friendly reads for list and summary.
- Stable, deterministic ordering across pagination.
- No N+1 for the facility/city/region/country chain.
- Aggregate count queries are small and fixed in number.
