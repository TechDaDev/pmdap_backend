# ADR 0011: Facility and classification metadata

- Status: Accepted for M11
- Date: 2026-08-09

## Context

Medical documents already preserve uploader-entered facility, location, department,
physician, type, and report-date metadata. M12 archive views and M13 search need a
stable facility identity without changing source evidence or weakening M10 date
authority.

## Decision

Use the existing `facilities` module as a normalized reference-data boundary:
`Country -> AdministrativeRegion -> City -> HealthcareFacility`. Countries use ISO
alpha-2 codes. Facilities have UUID identity, controlled type, explicit curated
aliases, deterministic Unicode/whitespace/case normalization, and hierarchy checks.
The migration seeds the `IQ` country and 19 Iraqi governorate reference names.
Provenance is the Iraqi Central Statistical Organization's official
[governorate statistical abstract](https://cosit.gov.iq/StatisticalAbstract-2022/StatisticalAbstract.html),
supplemented by the Iraqi Council of Representatives' 2025
[law establishing Halabja Governorate](https://archive5.parliament.iq/law/?entry=1585).
Foreign locations use the same generic hierarchy and can be curated through Django
admin/internal services. No facility-directory completeness claim is made.

`MedicalDocument.healthcare_facility` is an optional protected foreign key. Existing
raw `facility_name` and `location_text` remain unchanged. No migration guesses a
facility from raw text. No fuzzy, transliteration, OCR-text, keyword, or AI matching
is performed.

Facilities are deactivated, not deleted. Historical documents keep the same facility
UUID and serialize the current safe reference record. Active facilities are the
default assignment/directory set; inactive assignment is rejected.

Document classification continues to use the M6 enum. `classification_source`
records `USER_SELECTED`, `GUARDIAN_SELECTED`, or `SYSTEM_DEFAULT`. Existing explicit
uploads are backfilled from their uploader relationship. Classification changes are
explicit authorized metadata updates and never re-run file, OCR, or date processing.

Generic metadata PATCH cannot write document-date authority fields. M10 confirmation
remains the only post-upload report-date authority path.

PostgreSQL remains the authoritative development and concurrency database. Model and
service checks supplement database foreign keys, uniqueness, and the city-requires-
region constraint because cross-table country/region/city agreement cannot be fully
expressed as a portable SQL check constraint.

## Consequences

- Raw evidence and normalized identity coexist.
- Renames do not duplicate or relink documents.
- No patient/document reverse associations appear in the facility API.
- Facility creation remains admin/internal; patient APIs are read-only references.
- Archive, general search, physician identity, and automatic classification remain
  out of scope.
