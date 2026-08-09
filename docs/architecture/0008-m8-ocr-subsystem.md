# ADR 0008: M8 Bounded Multilingual OCR

- Status: Accepted
- Date: 2026-08-09
- Scope: M8 only

## Context

M7 preserves native PDF text per page and marks weak pages `requires_ocr`.
M8 must derive text from accepted JPEG/PNG reports, image-only PDFs, and only
weak pages in mixed PDFs. Inputs and output are sensitive medical data. OCR is
CPU-heavy, model-backed, and exposed to malformed images, decompression bombs,
render expansion, duplicate delivery, stale workers, and deletion races.

PyMuPDF 1.28.0 remains dual AGPL/commercial licensed. Compatibility for
production distribution and operation remains unresolved; M8 does not change
or resolve that item.

## Decision

### Replaceable engine contract

`processing.ocr.OCREngine` defines a structured `extract_image` contract.
`PaddleOCREngine` is the only production adapter in M8. Results contain exact
Unicode text, ordered lines, confidence aggregates, engine/version, duration,
and preprocessing provenance. Engine output is validated before persistence.
Medical-document services depend on the contract, not PaddleOCR internals.

PaddleOCR 3.7.0 with PaddlePaddle 3.3.0 CPU is pinned. The exact
`PP-OCRv5_mobile_det` and `arabic_PP-OCRv5_mobile_rec` models are selected; the
recognizer supports Arabic, Persian, English, and digits in one deterministic
pass. Explicit names avoid PaddleOCR falling back to PP-OCRv6 when only one
model is overridden. No cloud API, VLM, GPU, or second OCR engine is used.
oneDNN is disabled because PaddlePaddle 3.3.x CPU inference has an unresolved
PIR attribute-conversion failure on these PP-OCRv5 models; standard CPU kernels
are the verified M8 path.

### Native-first page canonicalization

M7 native page text is preserved in `native_text`. OCR output is preserved in
`ocr_text`. `text` remains canonical effective text. Pages with usable native
text keep `effective_source=PDF_TEXT`; only pages whose current authoritative
row still has `requires_ocr=true` may be rendered and OCRed. Successful OCR sets
that page to `effective_source=OCR`, `ocr_completed=true`, and
`requires_ocr=false`. Aggregate text is rebuilt in page order with M7's stable
form-feed separator.

JPEG/PNG use one logical page in the same `DocumentText`/`DocumentTextPage`
domain. Successful image or PDF OCR ends at `TEXT_EXTRACTED`. No OCR text is
added to patient APIs; `text_available` remains the only public signal.

### Asynchronous lifecycle and concurrency

Image uploads enqueue `processing.ocr_medical_document` only after commit while
retaining the established upload response. M7 persistence enqueues the same
task after commit whenever current pages require OCR, including usable mixed
PDFs. Task input is only the document UUID.

OCR uses `OCR_PROCESSING` as the only new transient state. A short row lock
claims work, processing runs outside the transaction, and final persistence
locks and revalidates active state, source text UUID, integrity, malware state,
and current page requirements. Duplicate workers reuse canonical output.
Replacing M7 text invalidates an older OCR snapshot. Late failures cannot
replace completed text or resurrect a deleted document. Exactly-once broker
delivery is not claimed; transactional outbox remains deferred.

### Bounded derivatives

Images are decoded from verified bytes with Pillow. EXIF orientation correction
and RGB conversion are the conservative `m8-preprocess-v1` derivative pipeline;
original bytes never change. Pixel count, width, height, and decompression-bomb
warnings are enforced before OCR.

PDF pages are rendered one at a time from original bytes at configured 300 DPI.
Projected dimensions are checked before rendering. Rendered pixels stay in
memory only and are released after each page. DPI and all resource limits are
server configuration, never request input. Per-page and document OCR text
limits fail closed rather than truncate.

### Worker and model provisioning

Web image stays free of Paddle runtime. Dedicated `ocr-worker` Docker stage adds
CPU Paddle dependencies and preloads detection plus Arabic PP-OCRv5 recognition
models at image build time into `/opt/paddle-cache`. Runtime disables model
source probing and uses the image cache, permitting offline task execution.
Large model binaries are not committed. Local development may provision the
same pinned models explicitly with the preload script.

### Privacy and failure policy

Logs/events allowlist document UUID, page number/count, engine name, duration,
character count, confidence aggregates, status, and stable failure code. They
never include OCR/native text, filenames, storage paths, patient identifiers,
diagnoses, or values.

Transient storage/resource/process failures receive bounded exponential retry.
Missing/tampered/quarantined/deleted input, image decode/render errors, engine
unavailability, malformed results, resource overflow, and DB persistence errors
are controlled stable failures. Original `StoredFile` and any valid native or
completed OCR text remain preserved.

## Defaults

- `OCR_ENGINE=paddleocr`
- `OCR_TEXT_DETECTION_MODEL_NAME=PP-OCRv5_mobile_det`
- `OCR_TEXT_RECOGNITION_MODEL_NAME=arabic_PP-OCRv5_mobile_rec`
- `OCR_PDF_RENDER_DPI=300`; maximum 400
- `OCR_MAX_IMAGE_PIXELS=20000000`
- `OCR_MAX_WIDTH=6000`; `OCR_MAX_HEIGHT=6000`
- `OCR_MAX_TEXT_CHARS_PER_PAGE=100000`
- `OCR_MAX_TEXT_CHARS_PER_DOCUMENT=2000000`
- retries 3; base backoff 10 seconds; soft/hard limits 1500/1800 seconds

## Consequences

- Mixed PDFs preserve good native text and spend OCR only on weak pages.
- CPU workers remain replaceable and independently scalable.
- OCR confidence is provenance, never medical truth or a medical decision.
- OCR text remains sensitive DB content with operational encryption/retention
  obligations.
- PaddleOCR/PaddlePaddle and model cache materially enlarge only worker image.
- Real Arabic/English accuracy is fixture-level M8 evidence, not clinical
  validation or final benchmarking.

## Explicitly deferred

Date parsing/normalization/ranking, report-date confirmation, archive indexing,
search, classification, facilities, doctors, LLMs, alternative OCR adapters,
GPU orchestration, text APIs, and PyMuPDF licensing resolution are outside M8.
