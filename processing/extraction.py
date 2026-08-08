from dataclasses import dataclass
from typing import Any

from django.conf import settings

EXTRACTION_METHOD = "PDF_TEXT"
EXTRACTOR_NAME = "PyMuPDF"
PIPELINE_VERSION = "m7-v1"
PAGE_SEPARATOR = "\n\f\n"


class PDFExtractionError(Exception):
    code = "pdf_extraction_failed"
    retryable = False

    def __init__(self, message="PDF text extraction failed."):
        super().__init__(message)


class PDFEncryptedError(PDFExtractionError):
    code = "pdf_encrypted"


class PDFPageLimitError(PDFExtractionError):
    code = "pdf_page_limit_exceeded"


@dataclass(frozen=True)
class TextUsabilityResult:
    usable: bool
    reason: str
    total_meaningful_characters: int
    page_meaningful_characters: tuple[int, ...]
    page_requires_ocr: tuple[bool, ...]


@dataclass(frozen=True)
class PDFTextPageResult:
    page_number: int
    text: str
    meaningful_character_count: int
    requires_ocr: bool


@dataclass(frozen=True)
class PDFTextResult:
    text: str
    page_count: int
    pages: tuple[PDFTextPageResult, ...]
    character_count: int
    usable: bool
    reason: str
    metadata: dict[str, Any]


class TextUsabilityEvaluator:
    def __init__(
        self,
        *,
        min_chars=None,
        min_page_chars=None,
        min_text_page_ratio=None,
    ):
        self.min_chars = settings.PDF_TEXT_MIN_CHARS if min_chars is None else min_chars
        self.min_page_chars = (
            settings.PDF_TEXT_MIN_PAGE_CHARS
            if min_page_chars is None
            else min_page_chars
        )
        self.min_text_page_ratio = (
            settings.PDF_TEXT_MIN_TEXT_PAGE_RATIO
            if min_text_page_ratio is None
            else min_text_page_ratio
        )

    @staticmethod
    def meaningful_character_count(text):
        return sum(character.isalnum() for character in text)

    def evaluate(self, page_texts):
        counts = tuple(
            self.meaningful_character_count(page_text) for page_text in page_texts
        )
        requires_ocr = tuple(count < self.min_page_chars for count in counts)
        text_page_count = sum(not required for required in requires_ocr)
        page_count = len(counts)
        text_page_ratio = text_page_count / page_count if page_count else 0.0
        total = sum(counts)
        usable = total >= self.min_chars and text_page_ratio >= self.min_text_page_ratio
        reason = "usable_pdf_text" if usable else "insufficient_meaningful_text"
        return TextUsabilityResult(
            usable=usable,
            reason=reason,
            total_meaningful_characters=total,
            page_meaningful_characters=counts,
            page_requires_ocr=requires_ocr,
        )


class PDFTextExtractor:
    def __init__(self, *, evaluator=None):
        self.evaluator = evaluator or TextUsabilityEvaluator()

    def extract(self, content):
        import pymupdf

        try:
            document = pymupdf.open(stream=content, filetype="pdf")
        except (pymupdf.EmptyFileError, pymupdf.FileDataError) as exc:
            raise PDFExtractionError() from exc
        except Exception as exc:
            raise PDFExtractionError() from exc

        try:
            if document.needs_pass:
                raise PDFEncryptedError("Encrypted PDFs cannot be extracted.")
            page_count = document.page_count
            if page_count > settings.PDF_MAX_PAGES:
                raise PDFPageLimitError("PDF exceeds the configured page limit.")

            try:
                page_texts = tuple(page.get_text("text") for page in document)
            except Exception as exc:
                raise PDFExtractionError() from exc
            usability = self.evaluator.evaluate(page_texts)
            pages = tuple(
                PDFTextPageResult(
                    page_number=index + 1,
                    text=text,
                    meaningful_character_count=(
                        usability.page_meaningful_characters[index]
                    ),
                    requires_ocr=usability.page_requires_ocr[index],
                )
                for index, text in enumerate(page_texts)
            )
            aggregate = PAGE_SEPARATOR.join(page_texts)
            return PDFTextResult(
                text=aggregate,
                page_count=page_count,
                pages=pages,
                character_count=len(aggregate),
                usable=usability.usable,
                reason=usability.reason,
                metadata={
                    "extraction_method": EXTRACTION_METHOD,
                    "extractor_name": EXTRACTOR_NAME,
                    "extractor_version": pymupdf.__version__,
                    "pipeline_version": PIPELINE_VERSION,
                    "meaningful_character_count": (
                        usability.total_meaningful_characters
                    ),
                    "pages_requiring_ocr": [
                        page.page_number for page in pages if page.requires_ocr
                    ],
                },
            )
        finally:
            document.close()
