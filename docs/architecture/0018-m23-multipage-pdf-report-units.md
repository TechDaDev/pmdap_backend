# ADR-0018: Multi-page medical PDFs as one source document with independent page report units

## Status

Accepted (M23 V1)

## Context

A single uploaded medical PDF can contain multiple heterogeneous report pages
(chemistry, hormones, CBC) that may share or differ in date. Today the whole
PDF is treated as one extraction context: OCR loops every page in a single
task, one bad page fails the whole document, lab extraction mixes rows from all
pages, and date confirmation is document-level. Result: a single bad page can
leave the PDF stuck on "Extracting results…" forever, and one confirmation
silently applies one date to every page.

## Decision

Keep ONE uploaded PDF as ONE archived source `MedicalDocument`. Introduce a
domain-level `MedicalDocumentPage` report unit (one per PDF page, page 1 for
images) that independently owns:

- page number
- OCR/processing state
- detected report subtype (layout metadata only)
- date candidates + confirmation state
- structured lab extraction + results
- failure state

`DocumentTextPage`/`DocumentTextSpan` stay OCR/text infrastructure. Page units
are medical-document domain state.

### Rejected alternatives

- **Flatten whole PDF into one extraction** — one bad page loses all results,
  cross-page row contamination, cannot represent per-page dates.
- **Create a separate uploaded MedicalDocument per page** — breaks archive
  integrity, duplicates source file, wrong duplicate semantics, fragment the
  patient's record.

## Details

- **Parent aggregate status** (`recalculate_document_processing_state`):
  any page working -> PROCESSING; all READY -> DATE_CONFIRMED; any awaiting ->
  AWAITING_CONFIRMATION; READY+FAILED mix -> PARTIAL; all FAILED -> FAILED.
- **Parent date rule**: when all confirmed page dates are identical the parent
  date equals that date; when they differ the parent date is cleared (mixed
  state); otherwise untouched until confirmed.
- **Date candidates**: page-scoped (`DateCandidate.page_unit`, existing
  `page_number`). Each page's candidates come from that page's text only.
- **Lab extraction**: page-scoped (`LabReportExtraction.page_unit`). Each page
  parses only its own spans; a result's `source_spans` always belong to one
  page (invariant tested).
- **Confirm queue**: report-unit aware. A 3-page PDF contributes up to 3
  independent confirmations; Home badge, queue page, and archive count derive
  from the same page-unit rule.
- **Failure isolation**: a page OCR/parse failure fails only that page.
- **Single-page compatibility**: images and 1-page PDFs keep the classic
  document-level flow and UI (page abstraction hidden).
- **Duplicate protection** stays document/file-level (one PDF = one upload).
- **Archive**: still one archive card per source document. Year filter uses the
  parent effective date (page dates are aggregated per the parent date rule).
- **Polling**: one parent pages-status poll per document, not N page polls.

## Date rule

Independent page dates. No implicit mass confirmation. Optional "apply to
other pages" is future UX, not V1.

## Consequences

- Multi-page PDFs become failure-isolated and independently confirmable.
- Existing single-page data is preserved; migration backfills one page unit per
  existing `DocumentTextPage` (or page 1 fallback).
- Minor/guardian page endpoints are NOT added in V1 (guardian medical policy
  unchanged); page access inherits the source document's ownership checks.
