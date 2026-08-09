# ADR 0009: Deterministic Multilingual Date Candidates

- Status: Accepted
- Date: 2026-08-09
- Scope: M9 only

## Context

M7/M8 produce canonical page text from native PDF extraction or OCR. Medical
reports commonly contain several dates in English, Arabic, mixed digits, and
ambiguous numeric order. M9 must explain and rank occurrences without changing
patient-verified metadata or pretending a machine suggestion is final truth.
Candidate context is sensitive medical data. Duplicate Celery delivery and
concurrent processing require PostgreSQL enforcement.

## Options considered

1. Third-party natural-language date parser: broad formats, but locale heuristics
   are difficult to explain and may silently reinterpret ambiguity.
2. LLM classification: flexible language handling, but nondeterministic,
   privacy-expanding, costly, and explicitly outside scope.
3. Explicit patterns, dictionaries, and named weights: narrower coverage, but
   deterministic, testable, versioned, and auditable.

## Decision

Choose explicit deterministic parsing and scoring behind one `m9-date-v1`
pipeline. Normalize derivative text with NFKC, Arabic/Persian digit translation,
supported separator mapping, and whitespace handling while retaining source
indices. Parse only controlled numeric and English month-name forms. Preserve
DMY/MDY ambiguity with an alternative date instead of hiding it.

Store every useful occurrence as `DateCandidate` with page/text source, bounded
context, type, score, ambiguity, parser rule, offset, and version. Replace one
document's candidate set atomically on reprocessing. PostgreSQL uniqueness
prevents duplicate occurrences and more than one suggested candidate.

Classification uses nearest controlled English/Arabic labels. Score range is
0.0–1.0 and represents archival relevance, not OCR accuracy. DOB and print dates
are strongly deprioritized. Unlabeled, ambiguous, and far-future dates are
penalized. Old dates are not penalized merely for age. Different top dates tied
within tolerance yield no suggestion.

`is_suggested` is advisory only. M9 never writes `document_date`, `date_source`,
`date_verified`, or verification timestamps. Existing verified/manual dates
remain authoritative.

Run asynchronously after canonical text reaches `TEXT_EXTRACTED`. Dispatch uses
`transaction.on_commit`; task input is document UUID. PostgreSQL row locking,
source-text snapshot revalidation, atomic replacement, and state-checked failure
handling protect duplicate, stale, and deletion races. No-date is
`DATE_NOT_FOUND`, not failure.

Expose bounded candidates through additive adult/minor read-only subresources
using existing medical authorization. Do not expose full text, raw storage,
patient internals, OCR internals, or parser implementation details. Logs/events
exclude contexts and detected dates.

Docker PostgreSQL 17 with retained named volume is authoritative for development
and integration. SQLite remains optional for fast non-locking tests. Both
persistent forward migration and fresh migration-from-zero are acceptance gates.

## Consequences

- Results are explainable, reproducible, and versionable.
- Genuine ambiguity stays visible; weak evidence may produce no suggestion.
- Dictionaries and patterns require deliberate versioned expansion.
- Candidate context creates sensitive-data retention and authorization duties.
- Exactly-once broker delivery is not claimed; database idempotency is required.
- Patient confirmation and final archival date remain M10 work.

## Explicitly unresolved/deferred

Arabic textual month names, broader locale formats, confirmation/selection,
archive indexing, search, classification, facilities, doctors, LLMs, and OCR
fallback policy remain deferred. PyMuPDF AGPL/commercial production licensing
remains unresolved.
