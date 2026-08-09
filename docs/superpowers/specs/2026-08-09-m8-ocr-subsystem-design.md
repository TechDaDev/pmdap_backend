# M8 OCR Subsystem Design

## Contract

`OCREngine.extract_image(PIL.Image.Image) -> OCRResult` returns immutable,
validated text lines and confidence/provenance metadata. `PaddleOCREngine`
adapts PaddleOCR 3.7 output. Failures use stable typed codes. Tests inject a fake
engine; core suite needs no GPU, model download, or Paddle import.

## Canonical text

- PDF pages retain exact M7 `native_text`.
- Current `requires_ocr` pages alone are rendered and passed to engine.
- OCR success stores exact `ocr_text`; no translation, cleanup, or digit/date
  normalization.
- `text` is effective page text selected as native when usable, otherwise OCR.
- Aggregate text is page-ordered with `\n\f\n` separators.
- JPEG/PNG create one page in same domain with `effective_source=OCR`.

## Trust boundaries and invariants

Root attacker goal: make untrusted medical upload exhaust worker, bypass file
gates, leak content, corrupt canonical text, or resurrect deleted content.
Branches and controls:

- compressed/image/render expansion: byte, pixel, dimension, DPI, page, text,
  task-time bounds; page-at-a-time rendering;
- parser/model abuse: M6 structural checks, current SHA/size verification,
  safe Pillow decode, typed adapter validation, CPU worker isolation;
- stale/duplicate work: PostgreSQL locks, source UUID snapshot, current-page
  revalidation, uniqueness, final active recheck;
- privacy leak: no text API, existing authority resolver, allowlisted events and
  logs, synthetic fixtures;
- model supply/runtime: pinned packages, build-time pinned model preload,
  runtime source probing disabled, controlled engine-unavailable failure.

Original `StoredFile` evidence is immutable. Completed native/OCR text cannot be
replaced by stale failure. `IDENTITY_VERIFICATION_AGENT` has zero medical OCR
access. Guardian access remains the M4 live verified/minor relationship rule.

## State and dispatch

- JPEG/PNG upload: stored response remains `UPLOADED`; after-commit OCR enqueue.
- PDF upload: M7 runs first.
- M7 pages requiring OCR: after-commit OCR enqueue.
- OCR claim: `OCR_PROCESSING`.
- successful usable OCR: `TEXT_EXTRACTED`.
- controlled terminal failure without canonical text: `FAILED`.
- transient failure: bounded retry while preserving current canonical outcome.

No public endpoint is added. Existing document schemas gain only new processing
enum values where schema generation reflects model choices.

## Acceptance

Unit/service tests cover adapter parsing, Unicode/confidence, preprocessing,
limits, decompression bombs, image/PDF/mixed flows, native preservation,
failures, retries, privacy, authorization, and idempotency. PostgreSQL tests
cover duplicate image/page workers, success versus stale failure, deletion, and
M7/M8 race. Runtime acceptance uses synthetic English, Arabic, mixed, image-only
PDF, and mixed-PDF fixtures plus Redis/Celery/PostgreSQL.

## Non-goals

M9 date work and all later archive/search/facility/doctor/AI work are excluded.
PyMuPDF licensing remains explicitly unresolved.
